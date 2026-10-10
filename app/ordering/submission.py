"""闪时送下单提交、重登、余额检查与至少一次+对账收敛循环。"""

from __future__ import annotations

import json
import re
import time
import uuid
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from threading import Lock, local
from typing import Any, Callable

from app.integrations.api_client import (
    ApiError, SssApiClient, SssTransportError,
    auth_error_message, is_auth_expired_payload, is_internal_error_payload,
    is_server_error_payload,
)
from app.order.common import _emit
from app.ordering.common import _trace
from app.ordering.diagnostics import (
    diagnostic_log_path, payload_digest, submission_diagnostic,
    write_submission_diagnostic,
)
from app.ordering.fingerprint import _task_fingerprint
from app.ordering.constants import (
    _ACCOUNT_PATH,
    _BATCH_CLOCK_SKEW_S,
    _CREATE_ORDER_PATH,
    _PREFILTER_ZERO_RETRY_DELAY_S,
    _PROGRESS_EVERY_N,
    _RECONCILE_POLL_ATTEMPTS,
)
from app.ordering.models import (
    _AuthExpired, _BalanceDepleted, _Reconciliation,
    _SubmissionUncertain, _SubmitResult,
)
from app.ordering.reconcile import _merge_reconciliation, _safe_reconcile
class _ExplicitRejection(LookupError):
    """平台返回可识别的参数校验拒绝，允许在只读对账后决定是否重试。"""


_VALIDATION_REJECTION_RE = re.compile(
    r"^(?:(?:操作失败|下单失败|创建失败)[!！,，:：\s]*)?"
    r"(?:地址无效|(?:收货|收件)?地址(?:不能为空|不完整|格式错误)"
    r"|(?:手机号|手机号码|电话|收件电话)(?:无效|不合法|格式错误|不能为空)"
    r"|(?:姓名|收件姓名|门牌号|商品名称|商品数量|预约时间|送达时间|门店)"
    r"(?:不能为空|未填写|必填))[!！。.,，\s]*$"
)
_BALANCE_REJECTION_RE = re.compile(
    r"^(?:(?:账户|帐户|账号|当前|可用)\s*)?余额不足"
    r"(?:[，,。.!！\s]*(?:请)?充值)?[。.!！\s]*$"
    r"|^欠费(?:停服)?[。.!！\s]*$"
    r"|^(?:insufficient\s+balance|balance\s+(?:is\s+)?insufficient)[.!\s]*$",
    re.IGNORECASE,
)


def _balance_precheck(tasks: list[dict[str, Any]], total: float | None,
                     unit_price: float, callback: Callable[[str], Any] | None
                     ) -> tuple[str, float]:
    """阻止余额未知或不足的真实批次，且保证尚未发送任何 POST。"""
    estimate = round(max(0.0, unit_price) * len(tasks), 2)
    if total is None:
        _emit(callback, "余额未知，为避免产生无法完成的订单，本批不会提交")
        return "balance_unknown", estimate
    if unit_price > 0 and total < estimate:
        shortage = round(estimate - total, 2)
        _emit(callback, f"余额不足：当前可用 {total:.2f}，本批预计 {estimate:.2f}，"
                        f"还差 {shortage:.2f}，本批不会提交")
        return "insufficient_balance", estimate
    return "ok", estimate


def _preflight_tasks(tasks: list[dict[str, Any]],
                     fetch_json: Callable[[str], dict[str, Any]],
                     callback: Callable[[str], Any] | None
                     ) -> tuple[str, _Reconciliation | None]:
    """只读检查订单列表；预检失败时绝不进入 POST 阶段。"""
    reconciliation = _safe_reconcile(tasks, fetch_json, callback, "预检站内对账", attempts=1)
    if reconciliation is None:
        return "preflight_uncertain", None
    if reconciliation.duplicate_count:
        _emit(callback, "预检发现站内重复订单，停止且不会提交")
        return "duplicate_detected", reconciliation
    if reconciliation.confirmed:
        _emit(callback, f"预检发现已有 {len(reconciliation.confirmed)} 单匹配，"
                        "这些订单不会重复提交")
    return "preflight_ok", reconciliation


def _check_success(resp: Any) -> None:
    """只有明确成功、登录拒绝或可识别的校验拒绝才能离开未知状态。

    success=false、错误码、Java 异常都不能证明事务已回滚；不认识的失败
    保留前置记录，仅做只读对账，不能据此重发非幂等 POST。
    """
    if not isinstance(resp, dict):
        raise _SubmissionUncertain(
            "平台响应格式未知，无法确认下单结果")
    message = str(resp.get("message") or resp.get("msg") or "").strip()
    if is_internal_error_payload(resp):
        exception = re.search(r"\b(?:java|javax|org|com)\.[\w.]*(?:Exception|Error)\b", message)
        detail = f"：{exception.group(0)}" if exception else ""
        raise _SubmissionUncertain(f"平台内部异常，无法确认是否落单{detail}")
    if is_server_error_payload(resp):
        raise _SubmissionUncertain("平台返回服务端错误，无法确认是否落单")
    if is_auth_expired_payload(resp):
        raise _AuthExpired(auth_error_message(resp, fallback="平台响应表示登录态失效"))
    success = resp.get("success")
    if success is True or (isinstance(success, int) and not isinstance(success, bool)
                           and success == 1):
        return
    if success is False:
        if _BALANCE_REJECTION_RE.fullmatch(message):
            raise _BalanceDepleted(message)
        if _VALIDATION_REJECTION_RE.fullmatch(message):
            raise _ExplicitRejection(message)
        code = resp.get("code")
        if code in (None, ""):
            code = resp.get("errorCode") or resp.get("error_code")
        detail = f"（code={code}）" if re.fullmatch(r"[0-9]{1,6}", str(code)) else ""
        raise _SubmissionUncertain(f"平台返回失败，但无法证明未落单{detail}")
    raise _SubmissionUncertain(
        "平台响应缺少明确 success 字段，无法确认下单结果")


def _is_auth_expired(exc: BaseException) -> bool:
    """判断是否为登录态过期，过期才值得打断整批去重登。

    闪时送实际使用 HTTP 200 + code=10000 + “token失效，请重新登陆”，
    因此不能只看 401。
    """
    text = str(exc)
    if is_internal_error_payload({"message": text}):
        return False
    lowered = text.lower()
    keywords = (
        "401", "code=10000", "code: 10000", "code:10000",
        "token失效", "token 失效", "token invalid", "invalid token",
        "登录态失效", "登录已失效", "登录过期", "登录已过期",
        "请重新登陆", "请重新登录", "unauthorized", "login expired",
    )
    return any(keyword in text or keyword in lowered for keyword in keywords)


def _with_auth_relogin(operation: Callable[[], Any], relogin: Callable[[], None],
                       callback: Callable[[str], Any] | None, label: str) -> Any:
    """执行只读/准备操作；确认登录态失效时重登一次并重试。"""
    try:
        return operation()
    except Exception as exc:
        if not _is_auth_expired(exc):
            raise
        _emit(callback, f"{label}时登录态失效，正在重新登录…")
        relogin()
        return operation()


def _post_one(client: Any, payload: dict[str, Any]) -> dict[str, Any]:
    """单次下单 POST；任何非确认响应均改为待对账状态。

    非幂等 POST 一旦调用出去，任何异常（超时、连接中断、非 JSON、甚至
    客户端适配层直接抛 ``TimeoutError``）都无法证明服务端没有落单，因此
    除明确的登录态失效外，一律归类为“已发送未知”，交给只读对账确认。
    """
    try:
        return client.post_json(_CREATE_ORDER_PATH, payload)
    except _AuthExpired:
        raise
    except ApiError as exc:
        if not isinstance(exc, SssTransportError) and _is_auth_expired(exc):
            wrapped = _AuthExpired(str(exc))
        else:
            wrapped = _SubmissionUncertain(str(exc))
        wrapped.diagnostics = dict(getattr(exc, "diagnostics", {}) or {})
        raise wrapped from exc
    except Exception as exc:
        detail = str(exc) or type(exc).__name__
        raise _SubmissionUncertain(detail) from exc


def _emit_round_summary(callback: Callable[[str], Any] | None,
                        latencies: list[float],
                        started_at: float,
                        read_timeout_s: float | None,
                        min_interval_s: float | None = None) -> None:
    """本轮提交的实测汇总：并发到底有没有用，全看这一行。

    吞吐 = 单数 / 墙钟，每单均时 = 服务端建单耗时。并发调高后若平台在建单上
    排队，每单均时会被推高而吞吐不变——这时应当把并发调低（或放宽读取超时），
    而不是继续加路数。启用提交节流时补一句当前生效的最小间隔（该值只来自配置，
    不随平台内部异常变化），便于和日志里的发送节奏对照。
    """
    if not latencies:
        return
    wall = max(0.0, time.perf_counter() - started_at)
    average = sum(latencies) / len(latencies)
    throughput = (len(latencies) / wall) if wall > 0 else 0.0
    message = (f"本轮提交 {len(latencies)} 单，耗时 {wall:.1f} 秒，"
               f"每单平均 {average:.1f} 秒（吞吐 {throughput:.2f} 单/秒）")
    try:
        pacing = float(min_interval_s) if min_interval_s else 0.0
    except (TypeError, ValueError):
        pacing = 0.0
    if pacing > 0:
        message += f"；提交最小间隔 {pacing:g} 秒"
    try:
        limit = float(read_timeout_s) if read_timeout_s else 0.0
    except (TypeError, ValueError):
        limit = 0.0
    if limit > 0 and average > limit * 0.6:
        message += (f"；已接近读取超时 {limit:g} 秒，平台可能在建单上排队，"
                    "必要时把并发调低或把读取超时调高")
    _emit(callback, message)


#: 每轮精简结果的单字符标记：✔ 成功、✖ 技术异常/结果未知、⚠ 平台明确拒绝、
#: A 登录失效、B 余额不足。只有真正发出过 POST 的尝试才会进入这个列表。
_SEND_MARKERS = {
    "success": "✔", "uncertain": "✖", "failure": "⚠", "auth": "A", "balance": "B",
}
_SEND_MARKER_LEGEND = ("✔ 成功响应；✖ 技术异常/结果待对账；⚠ 平台明确拒绝；"
                       "A 登录失效；B 余额不足")


class _SubmitPacer:
    """提交节流：令相邻两次 POST 的起点至少间隔配置的 ``interval`` 秒。

    间隔**只来自配置**（``sss_submit_min_interval_s``，出厂 2.5 秒，0 = 关闭节流），
    运行期不因任何响应特征自动放宽或收紧：平台的建单内部异常（如
    ``IndexOutOfBoundsException``）只代表“结果待对账”，既不是受理超速的证据，
    也不能被当成“没有落单”，节奏因此只由配置固定。节流只错开发送起点，不改变
    任何结果分类与重发语义。
    """

    def __init__(self, interval_s: float) -> None:
        self._lock = Lock()
        self._interval = max(0.0, float(interval_s or 0.0))
        self._next_allowed = 0.0  # time.perf_counter 时间基准

    def wait(self, stop_event: Any = None,
             halted: Callable[[], bool] | None = None) -> bool:
        """阻塞到可以发送下一次 POST；返回 False = 等待期间停止/中止（未发送）。

        节流关闭（间隔为 0）时不等待也不中止，行为与未引入节流前完全一致；
        等待路径上出现停止、401 或余额不足时返回 False，调用方应把该任务记为
        「未发送」（它确实没有发出，重登后按未发送语义允许补发）。
        """
        if self._interval <= 0.0:
            return True

        def aborted() -> bool:
            if halted is not None and halted():
                return True
            return stop_event is not None and bool(stop_event.is_set())

        while True:
            if aborted():
                return False
            with self._lock:
                now = time.perf_counter()
                if now >= self._next_allowed:
                    self._next_allowed = now + self._interval
                    return True
                delay = min(self._next_allowed - now, 0.25)
            time.sleep(delay)

    @property
    def interval(self) -> float:
        """当前生效的最小间隔（秒）。"""
        with self._lock:
            return self._interval


def _round_counts_message(result: _SubmitResult) -> str:
    """本轮提交的精确计数：实际发起、成功响应、技术异常、明确拒绝、未发送。"""
    return (f"本轮统计：实际发起 {result.attempts} 次 POST"
            f"（{len(result.dispatched)} 单）；成功响应 {len(result.succeeded)}，"
            f"技术异常 {len(result.uncertain)}，明确拒绝 {result.explicit_rejections}，"
            f"登录失效 {len(result.auth)}，余额不足 {len(result.balance)}，"
            f"未发送 {len(result.not_sent)}")


def _submit_tasks_concurrent(tasks: list[dict[str, Any]],
                             submit: Callable[[dict[str, Any]], dict[str, Any]],
                             stop_event: Any,
                             callback: Callable[[str], Any] | None,
                             max_workers: int = 4,
                             on_stop: Callable[[], None] | None = None,
                             read_timeout_s: float | None = None,
                             min_interval_s: float = 0.0,
                             round_no: int = 1) -> _SubmitResult:
    """有界并发提交，停止、401、余额不足后不再派发新任务。

    同一轮最多保留 ``max_workers`` 个在途 POST。请求超时或断链只记录为
    ``uncertain``，由调用方查询订单列表确认，绝不在此处自动重发。

    ``min_interval_s`` > 0 时启用全局提交节流（见 `_SubmitPacer`）：相邻两次
    POST 的起点至少间隔该值，0 = 关闭节流。间隔只来自配置且在运行期固定，不会
    因为平台返回内部异常（``IndexOutOfBoundsException`` 等）自动放宽。

    ``round_no`` 是批次内的提交轮次（独立调用默认 1），只进入诊断记录。
    ``send_seq`` 是**本地派发起点**序号（同一轮内从 1 开始，由派发临界区内的
    计数决定）：它不代表响应完成顺序，也不代表服务端到达顺序；停止等待与未派发
    的任务不占序号。``attempts`` / ``dispatched`` 同样只统计真实调用 ``submit``
    的起点，未派发与本地准备失败都不计——派发临界区内不含任何网络请求。

    停止 / 401 / 余额不足在全部准备完成、进入派发临界区之前做最终确认：已经中止
    时按「未发送」处理，不发出 POST、不占序号、不计调用次数；本地准备失败同理，
    并只按异常类型提示，不输出报文或异常正文。

    结束后输出一行本轮汇总（`_emit_round_summary`）、一行精确计数和一行按
    ``send_seq`` 排列的成功/异常标记。
    """
    result = _SubmitResult()
    workers = max(1, int(max_workers))
    try:
        pacing = max(0.0, float(min_interval_s or 0.0))
    except (TypeError, ValueError):
        pacing = 0.0
    pacer = _SubmitPacer(pacing)
    if isinstance(round_no, bool) or not isinstance(round_no, int) or round_no < 1:
        round_no = 1
    iterator = iter(tasks)
    in_flight: dict[Any, dict[str, Any]] = {}
    stop_closed = False
    latencies: list[float] = []
    diagnostic_records: dict[str, dict[str, Any]] = {}
    # 派发序号/计数只在这一小段加锁（微秒级），网络请求绝不进入锁内，
    # 并发路数仍然有界且互不串行。
    dispatch_lock = Lock()
    dispatch_state: dict[str, Any] = {"seq": 0, "previous_start": None}
    seq_by_identifier: dict[str, int] = {}
    round_marks: dict[int, str] = {}

    def halted() -> bool:
        return bool(stop_event.is_set() or result.auth_error or result.balance_error)

    def run_one(task: dict[str, Any]) -> tuple[str, str, str]:
        identifier = str(task["identifier"])
        if stop_event.is_set():
            return identifier, "stopped", ""
        # 纯准备（可能耗时或抛异常）放在节流等待之前：它不占发送槽位（否则准备
        # 阻塞会让相邻两次 POST 的实际起点挤在一起），失败时也只是一次「未发送」。
        try:
            request_digest = payload_digest(task["payload"])
            attempt_id = uuid.uuid4().hex[:16]
        except Exception as exc:
            # 本地准备失败：请求没有发出，绝不带出报文或异常正文（可能含凭据）。
            _emit(callback, f"提交准备失败（{type(exc).__name__}）：{identifier} 未发送，"
                            "站内对账后可按未发送任务补发；不记为已发送未知")
            return identifier, "stopped", ""
        if not pacer.wait(stop_event, halted):
            # 节流等待期间收到停止/中止：请求确实没有发出，按「未发送」处理
            # （重登后仍属于可补发的明确未发送任务），也不占用派发序号。
            return identifier, "stopped", ""
        interval_at_send = pacer.interval
        # 所有准备完成、进入派发临界区之前的最终中止确认（stop / 401 / 余额不足）。
        if halted():
            return identifier, "stopped", ""
        with dispatch_lock:
            started = time.perf_counter()
            started_at = time.time()
            dispatch_state["seq"] += 1
            send_seq = dispatch_state["seq"]
            previous_start = dispatch_state["previous_start"]
            dispatch_state["previous_start"] = started
            seq_by_identifier[identifier] = send_seq
            result.attempts += 1
            result.dispatched.add(identifier)
            # 序号与起点同区取得，间隔直接作差（不夹紧，避免掩盖异常值）。
            actual_interval_s = (None if previous_start is None
                                 else started - previous_start)
        response: Any = None
        transport: dict[str, Any] = {}
        try:
            response = submit(task["payload"])
            _check_success(response)
        except _AuthExpired as exc:
            transport = dict(getattr(exc, "diagnostics", {}) or {})
            state, detail = "auth", str(exc)
        except _BalanceDepleted as exc:
            state, detail = "balance", str(exc)
        except _ExplicitRejection as exc:
            state, detail = "failure", str(exc)
        except (_SubmissionUncertain, SssTransportError) as exc:
            transport = dict(getattr(exc, "diagnostics", {}) or {})
            state, detail = "uncertain", str(exc)
        except Exception as exc:
            # 非幂等 POST 一旦调用出去，非显式拒绝的异常一律是“已发送未知”；
            # 不能落入 failure/discard，否则平台已落单但响应异常时会重复 POST。
            state, detail = "uncertain", str(exc)
        else:
            state, detail = "success", ""
        elapsed = time.perf_counter() - started
        # list.append 在 CPython 下是原子的；这里只做统计，不参与任何判定。
        latencies.append(elapsed)
        try:
            record: dict[str, Any] | None = submission_diagnostic(
                task, response, started_at=started_at, elapsed_s=elapsed, state=state,
                payload_hash=request_digest, transport=transport, send_seq=send_seq,
                round_no=round_no, attempt_id=attempt_id,
                actual_interval_s=actual_interval_s)
            record["min_interval_s"] = round(interval_at_send, 3)
        except Exception as exc:
            # 诊断只是旁路：构造失败不得改变已经取得的分类，也不触发任何重发。
            record = None
            _emit(callback, f"提交诊断构造失败（{type(exc).__name__}）："
                            "该次提交仍按已取得的响应分类，不会因此重发")
        if record is not None:
            diagnostic_records[identifier] = record
        _trace(f"POST {identifier}: {elapsed:.2f}s -> {state}")
        return identifier, state, detail

    started_at = time.perf_counter()
    with ThreadPoolExecutor(max_workers=workers) as executor:
        exhausted = False
        while in_flight or not exhausted:
            while not exhausted and not halted() and len(in_flight) < workers:
                try:
                    task = next(iterator)
                except StopIteration:
                    exhausted = True
                    break
                in_flight[executor.submit(run_one, task)] = task
            if stop_event.is_set() and not stop_closed:
                stop_closed = True
                if on_stop is not None:
                    on_stop()
            if not in_flight:
                if halted():
                    result.stopped = bool(stop_event.is_set())
                    break
                continue
            done, _ = wait(in_flight, timeout=0.1, return_when=FIRST_COMPLETED)
            for future in done:
                task = in_flight.pop(future)
                try:
                    identifier, state, detail = future.result()
                except Exception as exc:  # pragma: no cover - run_one already contains errors
                    identifier, state, detail = task["identifier"], "uncertain", str(exc)
                seq = seq_by_identifier.get(str(identifier))
                if seq is not None:
                    # 完成顺序可能倒置；精简结果一律按派发时的 send_seq 排列。
                    round_marks[seq] = state
                diagnostic = diagnostic_records.pop(identifier, None)
                if diagnostic is not None:
                    diagnostic["workers"] = workers
                    try:
                        write_submission_diagnostic(diagnostic)
                    except Exception as exc:
                        _emit(callback, f"提交诊断记录写入失败（{type(exc).__name__}），"
                                        "订单仍按前置记录与只读对账处理")
                    if state != "success":
                        _emit(callback, f"提交诊断：{json.dumps(diagnostic, ensure_ascii=False, sort_keys=True)}")
                if state == "success":
                    result.succeeded.add(identifier)
                    if len(result.succeeded) % _PROGRESS_EVERY_N == 0:
                        _emit(callback, f"本轮收到成功响应 {len(result.succeeded)}/{len(tasks)} 单")
                elif state == "failure":
                    result.failures.append((identifier, detail))
                    result.explicit_rejections += 1
                elif state == "uncertain":
                    result.uncertain.append((identifier, detail))
                elif state == "stopped":
                    result.not_sent.add(identifier)
                elif state == "auth":
                    result.auth.add(identifier)
                    if not result.auth_error:
                        result.auth_error = detail
                elif state == "balance":
                    result.balance.add(identifier)
                    if not result.balance_error:
                        result.balance_error = detail
        # 中途 halt（401/余额/取消）时，迭代器里尚未派发的任务一定是未发送；
        # 单独记录，后续恢复只允许从这些明确未发送的任务继续。
        if not exhausted:
            for task in iterator:
                result.not_sent.add(task["identifier"])
        result.stopped = bool(result.stopped or stop_event.is_set())
    _emit_round_summary(callback, latencies, started_at, read_timeout_s,
                        pacer.interval)
    if result.attempts or result.not_sent:
        _emit(callback, _round_counts_message(result))
    if round_marks:
        marks = " ".join(f"{seq}{_SEND_MARKERS.get(round_marks[seq], '?')}"
                         for seq in sorted(round_marks))
        _emit(callback, f"本轮发送顺序（send_seq:结果）：{marks}（{_SEND_MARKER_LEGEND}）")
    if result.uncertain:
        _emit(callback, f"本轮 {len(result.uncertain)} 次请求未获可确认结果"
                        "（例如平台建单内部异常）：结果待站内对账确认，"
                        "不代表订单未创建，本批不会自动重发")
    if latencies:
        _emit(callback, f"脱敏提交诊断日志：{diagnostic_log_path()}")
    return result


def query_balance(fetch_json: Callable[[str], dict[str, Any]]) -> tuple[float | None, float | None]:
    """查询账户余额，返回 ``(可用余额, 冻结金额)``。

    登录态失效必须向上抛出 ``_AuthExpired``，不能静默返回未知余额；普通
    接口异常仍按旧策略返回 ``(None, None)`` 以便继续展示日志。
    """
    try:
        payload = fetch_json(_ACCOUNT_PATH)
    except Exception as exc:
        if _is_auth_expired(exc):
            raise _AuthExpired(str(exc)) from exc
        return None, None
    if is_auth_expired_payload(payload):
        raise _AuthExpired(auth_error_message(payload))
    result = payload.get("result") if isinstance(payload, dict) else None
    if not isinstance(result, dict):
        return None, None

    def _num(value: Any) -> float | None:
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    return (_num(result.get("totalAmount")), _num(result.get("freezeAmount")))


def _format_balance(total: float | None, frozen: float | None) -> str:
    """余额日志一行：查不到时如实说明，不编造数字。"""
    if total is None:
        return "账户余额查询失败（接口异常），本批将停止提交"
    if frozen is None:
        return f"账户可用余额：{total}"
    return f"账户可用余额：{total}（冻结 {frozen}）"


def _make_api_submitter(client: SssApiClient) -> tuple[
        Callable[[dict[str, Any]], dict[str, Any]], Callable[[], None]]:
    """返回每线程独立 Session 的提交函数，以及幂等的关闭回调。"""
    per_thread = local()
    clients: list[SssApiClient] = []
    lock = Lock()
    closed = False

    def submit(payload: dict[str, Any]) -> dict[str, Any]:
        worker = getattr(per_thread, "client", None)
        if worker is None:
            worker = client.fork()
            per_thread.client = worker
            with lock:
                clients.append(worker)
        return _post_one(worker, payload)

    def close() -> None:
        nonlocal closed
        with lock:
            if closed:
                return
            closed = True
            to_close = list(clients)
        for worker in to_close:
            worker.close()

    return submit, close


def _journal_entry(task: dict[str, Any], error: str,
                   status: str = "unresolved") -> dict[str, Any]:
    """把一条任务转成可持久化的本地记录（不包含密码/凭据）。

    POST 前置记录用 ``inflight``；收到超时/断线后更新为 ``unresolved``；
    站内对账确认后才由 journal 层改成 ``resolved``。
    """
    fingerprint = _task_fingerprint(task)
    return {
        "identifier": str(task.get("identifier") or ""),
        "client_request_id": str(task.get("client_request_id") or ""),
        "sheet": str(task.get("sheet") or ""),
        "batch_id": str(task.get("batch_id") or ""),
        "account": str(task.get("account") or ""),
        "fingerprint": fingerprint.as_dict(),
        "error": str(error or ""),
        "status": str(status or "unresolved"),
    }


def _run_reconciled_submission(
        tasks: list[dict[str, Any]],
        submit_factory: Callable[[], tuple[Callable[[dict[str, Any]], dict[str, Any]], Callable[[], None]]],
        fetch_json: Callable[[str], dict[str, Any]],
        stop_event: Any,
        callback: Callable[[str], Any] | None,
        decision_callback: Callable[[str, str], str] | None,
        max_workers: int,
        relogin: Callable[[], None] | None = None,
        *,
        read_timeout_s: float | None = None,
        submit_min_interval_s: float = 0.0,
        uncertain_sink: Callable[[list[dict[str, Any]], dict[str, Any]], None] | None = None,
        uncertain_clear: Callable[[set[str]], None] | None = None,
        uncertain_discard: Callable[[set[str]], None] | None = None,
        journal_meta: dict[str, Any] | None = None,
        outcome: dict[str, Any] | None = None,
        ) -> tuple[_Reconciliation | None, bool]:
    """执行“先对账、提交、再对账”的至少一次语义。

    返回 ``(最终对账结果, 是否完成了最终对账)``。平台接口没有客户端幂等
    键，因此这里不承诺 exactly-once：只保证非幂等 POST 不自动重试，提交后
    通过列表对账确认；对账延迟时只读轮询，绝不立即重复 POST。

    ``uncertain_sink`` 在每次出现“已发送未知”时落一条本地记录；写入失败会
    立即停止后续 POST。``uncertain_clear`` 在只读对账确认相关订单后删除记录。
    ``outcome`` 为可选增量结果容器，供调用方生成 status/next_action，不影响
    原有两元组返回契约；其中 ``outcome["submission_stats"]`` 是提交层给 runner
    的计数契约（``target_total`` 由 runner 按本批目标总数补齐）。

    计数与“当前最终状态”严格分开：站内对账确认只会清理当前状态集合，不会清掉
    ``success_responses`` / ``technical_errors`` 等历史响应次数；初始或最终对账
    未知时 ``preconfirmed`` / ``newly_confirmed`` / ``confirmed`` / ``unconfirmed``
    用 ``None``，不用 0 冒充未知。
    """
    batch_started_at = time.time()
    batch_window_start = batch_started_at - _BATCH_CLOCK_SKEW_S
    task_by_id = {str(task.get("identifier")): task for task in tasks}
    state: dict[str, Any] = outcome if outcome is not None else {}
    state.setdefault("uncertain", [])
    state.setdefault("failures", {})
    state.setdefault("auth", [])
    state.setdefault("not_sent", [])
    state.setdefault("success_responses", [])
    state.setdefault("errors", {})
    state.setdefault("journal_error", "")
    state.setdefault("reconcile_failed", False)
    state["batch_started_at"] = batch_started_at
    uncertain_ids: set[str] = set()
    failure_ids: set[str] = set()
    not_sent_ids: set[str] = set()
    auth_ids: set[str] = set()
    balance_ids: set[str] = set()
    success_ids: set[str] = set()
    journal_ids: set[str] = set()
    #: 真实调用计数。与“当前最终状态”分离：站内对账确认只清当前状态，
    #: 绝不清这些历史响应次数（成功但暂不可见、随后被确认的依然计入）。
    stats_counts: dict[str, int] = {
        "attempts": 0, "success_responses": 0, "technical_errors": 0,
        "explicit_rejections": 0, "auth_rejections": 0, "balance_rejections": 0,
    }
    #: 真实发起过 POST 的任务（含重登补发与人工重试），不随对账清理。
    attempted_ids: set[str] = set()
    target_ids: set[str] = set(task_by_id)
    #: 初始对账给出的“缺失任务”集合 = not_sent 的分母；初始对账未知时保持
    #: None，此时按全部目标任务计（不拿未知当已有）。
    initial_missing_ids: set[str] | None = None
    preconfirmed_count: int | None = None
    final_counts: dict[str, int | None] = {
        "confirmed": None, "unconfirmed": None, "newly_confirmed": None,
    }
    final_reconciled = False

    def _sync_stats() -> None:
        """写入 ``outcome["submission_stats"]``：只有真实调用/集合推导的计数。

        ``target_total`` 不在其中（由 runner 补齐）；所有计数都来自实际 POST 的
        起点与对账集合，绝不从进度里程碑推测。
        """
        scope = target_ids if initial_missing_ids is None else initial_missing_ids
        state["submission_stats"] = {
            "preconfirmed": preconfirmed_count,
            "submitted": len(attempted_ids),
            "attempts": stats_counts["attempts"],
            "success_responses": stats_counts["success_responses"],
            "technical_errors": stats_counts["technical_errors"],
            "explicit_rejections": stats_counts["explicit_rejections"],
            "auth_rejections": stats_counts["auth_rejections"],
            "balance_rejections": stats_counts["balance_rejections"],
            "not_sent": len(scope - attempted_ids),
            "newly_confirmed": final_counts["newly_confirmed"],
            "confirmed": final_counts["confirmed"],
            "unconfirmed": final_counts["unconfirmed"],
            "reconciled": final_reconciled,
        }

    def _sync_outcome() -> None:
        state["uncertain"] = sorted(uncertain_ids)
        state["failure_ids"] = sorted(failure_ids)
        state["failures"] = {identifier: state["errors"][identifier]
                             for identifier in failure_ids
                             if identifier in state["errors"]}
        state["auth"] = sorted(auth_ids)
        state["not_sent"] = sorted(not_sent_ids)
        state["success_responses"] = sorted(success_ids)
        state["balance_ids"] = sorted(balance_ids)
        state["journal_pending"] = sorted(journal_ids)
        state["failure_count"] = len(failure_ids)
        _sync_stats()

    def _record_final(reconciliation: _Reconciliation | None,
                      reconciled: bool) -> None:
        """记录最终对账结论并同步 outcome；对账未完成时数量字段一律为 None。"""
        nonlocal final_reconciled
        final_reconciled = bool(reconciled and reconciliation is not None)
        if not final_reconciled:
            final_counts.update({"confirmed": None, "unconfirmed": None,
                                 "newly_confirmed": None})
        else:
            confirmed = len(reconciliation.confirmed)
            final_counts["confirmed"] = confirmed
            final_counts["unconfirmed"] = len(reconciliation.missing)
            final_counts["newly_confirmed"] = (
                None if preconfirmed_count is None
                else max(0, confirmed - preconfirmed_count))
        _sync_outcome()

    def _absorb(round_result: _SubmitResult) -> None:
        # 先累加真实调用计数：后面即使被站内对账确认清掉当前状态，
        # 这些历史响应次数也保持不变。
        stats_counts["attempts"] += round_result.attempts
        stats_counts["success_responses"] += len(round_result.succeeded)
        stats_counts["technical_errors"] += len(round_result.uncertain)
        stats_counts["explicit_rejections"] += round_result.explicit_rejections
        stats_counts["auth_rejections"] += len(round_result.auth)
        stats_counts["balance_rejections"] += len(round_result.balance)
        attempted_ids.update(round_result.dispatched)
        for identifier, detail in round_result.failures:
            failure_ids.add(identifier)
            state["errors"][identifier] = detail
        for identifier, detail in round_result.uncertain:
            failure_ids.discard(identifier)
            uncertain_ids.add(identifier)
            state["errors"][identifier] = detail
        not_sent_ids.update(round_result.not_sent)
        auth_ids.update(round_result.auth)
        balance_ids.update(round_result.balance)
        success_ids.update(round_result.succeeded)
        for identifier in round_result.succeeded:
            failure_ids.discard(identifier)
            not_sent_ids.discard(identifier)
            auth_ids.discard(identifier)
            balance_ids.discard(identifier)
            state["errors"].pop(identifier, None)
        _sync_outcome()

    def _mark_confirmed(reconciliation: _Reconciliation) -> None:
        confirmed = reconciliation.confirmed
        for identifiers in (uncertain_ids, failure_ids, not_sent_ids, auth_ids, balance_ids):
            identifiers.difference_update(confirmed)
        for identifier in confirmed:
            state["errors"].pop(identifier, None)
        _sync_outcome()

    def _persist_uncertain(round_result: _SubmitResult) -> None:
        if not round_result.uncertain or uncertain_sink is None:
            return
        persisted = set(state.get("persisted_uncertain") or set())
        entries: list[dict[str, Any]] = []
        for identifier, detail in round_result.uncertain:
            if identifier in persisted:
                continue
            task = task_by_id.get(identifier)
            if task is None:
                continue
            entries.append(_journal_entry(task, detail))
        if not entries:
            return
        try:
            uncertain_sink(entries, dict(journal_meta or {}))
        except Exception as exc:
            # 记录失败意味着不确定状态无法跨运行保留；立即停止后续 POST，
            # 绝不冒险继续发送或重发。
            state["journal_error"] = f"本地不确定记录写入失败：{exc}"
            _sync_outcome()
            _emit(callback, state["journal_error"] + "；已停止后续 POST，只允许只读对账")
            stop_event.set()
        else:
            persisted.update(entry["identifier"] for entry in entries)
            state["persisted_uncertain"] = persisted
            journal_ids.update(entry["identifier"] for entry in entries)
            _sync_outcome()

    def _resolve_confirmed_uncertain(reconciliation: _Reconciliation) -> None:
        _mark_confirmed(reconciliation)
        if uncertain_clear is None or not journal_ids:
            return
        confirmed = {identifier for identifier in journal_ids
                     if identifier in reconciliation.confirmed}
        if not confirmed:
            return
        try:
            uncertain_clear(confirmed)
        except Exception as exc:
            state["journal_error"] = f"本地不确定记录清理失败：{exc}"
            _sync_outcome()
            _emit(callback, state["journal_error"] + "；为避免重复提交，已停止恢复流程")
            stop_event.set()
        else:
            journal_ids.difference_update(confirmed)
            uncertain_ids.difference_update(confirmed)
            _sync_outcome()

    def _journal_failure(message: str) -> None:
        state["journal_error"] = message
        _sync_outcome()
        _emit(callback, message + "；已停止后续 POST，只允许只读对账")
        stop_event.set()

    def _prepare_round_journal(round_tasks: list[dict[str, Any]]) -> bool:
        """POST 前置写：先把本轮任务写成 inflight，进程在中途崩溃也能跨运行阻断。

        这是 SN-C4 的关键：不确定记录不能等 round 结束才写，否则“请求已发出、
        客户端断线、进程随后崩溃”会使记录完全丢失。写盘失败时不派发任何 POST。
        """
        if uncertain_sink is None or not round_tasks:
            return True
        entries = [
            _journal_entry(task, "提交前置记录：POST 即将发出，等待响应/对账",
                           status="inflight")
            for task in round_tasks
        ]
        try:
            uncertain_sink(entries, dict(journal_meta or {}))
        except Exception as exc:
            _journal_failure(f"本地不确定记录写入失败：{exc}")
            return False
        journal_ids.update(str(task.get("identifier")) for task in round_tasks)
        _sync_outcome()
        return True

    def _finalize_round_journal(round_tasks: list[dict[str, Any]],
                                round_result: _SubmitResult) -> None:
        """本轮结束后修正 journal 状态：明确未发送的 discard，已发出的保持 active。"""
        if uncertain_sink is None:
            return
        round_ids = {str(task.get("identifier")) for task in round_tasks}
        active = journal_ids & round_ids
        if not active:
            return
        unsent = active & (
            set(round_result.auth) | set(round_result.balance)
            | set(round_result.not_sent) | {identifier for identifier, _ in round_result.failures}
        )
        sent_unknown = active - unsent
        # success 响应也必须按“已发送未知”保留到站内对账确认，不能因为
        # POST 返回 success 就删除记录。
        entries = [
            _journal_entry(task_by_id[identifier],
                           "POST 已发出且返回 success，等待站内只读对账",
                           status="unresolved")
            for identifier in sorted(sent_unknown)
            if identifier not in uncertain_ids and identifier in task_by_id
        ]
        if entries:
            try:
                uncertain_sink(entries, dict(journal_meta or {}))
            except Exception as exc:
                _journal_failure(f"本地不确定记录写入失败：{exc}")
                return
        if unsent:
            if uncertain_discard is None:
                # 没有 discard 回调时宁可保留记录（更保守），也不能假装未发送。
                _emit(callback, f"{len(unsent)} 个明确未发送任务未获 journal 清理回调，"
                                "记录保持 active 以等待人工核对")
                return
            try:
                uncertain_discard(unsent)
            except Exception as exc:
                _journal_failure(f"本地不确定记录清理失败：{exc}")
                return
            journal_ids.difference_update(unsent)
            _sync_outcome()

    initial = _safe_reconcile(tasks, fetch_json, callback, "下单前站内对账", attempts=1)
    if initial is None:
        stop_event.set()
        state["reconcile_failed"] = True
        _record_final(None, False)
        return None, False
    # 初始对账成功：已有数量与“待提交的缺失任务”范围从此已知；对账失败时保持
    # None，submission_stats 里的对应字段也随之诚实为 null。
    preconfirmed_count = len(initial.confirmed)
    initial_missing_ids = {str(task.get("identifier")) for task in initial.missing}
    if initial.duplicate_count:
        _emit(callback, "站内记录数超过 Excel 目标，已停止且不会自动取消或补发")
        stop_event.set()
        _record_final(initial, True)
        return initial, True
    if not initial.missing:
        _emit(callback, "Excel 订单均已在站内确认，本次不发送任何下单请求")
        _record_final(initial, True)
        return initial, True

    preconfirmed = set(initial.confirmed)
    current = list(initial.missing)
    balance_error = ""
    round_no = 0

    def run_round(round_tasks: list[dict[str, Any]], workers: int) -> _SubmitResult:
        nonlocal round_no
        round_no += 1
        result = _SubmitResult()
        if not _prepare_round_journal(round_tasks):
            result.stopped = True
            return result
        submit, close = submit_factory()
        try:
            result = _submit_tasks_concurrent(round_tasks, submit, stop_event,
                                              callback, workers, on_stop=close,
                                              read_timeout_s=read_timeout_s,
                                              min_interval_s=submit_min_interval_s,
                                              round_no=round_no)
        finally:
            close()
        _absorb(result)
        _persist_uncertain(result)
        _finalize_round_journal(round_tasks, result)
        return result

    first = run_round(current, max_workers)
    if first.balance_error:
        balance_error = first.balance_error
        _emit(callback, f"余额不足，已停止后续下单：{first.balance_error}")
        stop_event.set()
    if first.uncertain:
        _emit(callback, f"{len(first.uncertain)} 单请求未获确认，已记录为“已发送未知”，"
                        "正在查站，绝不会直接重发")

    # 401 只能统一重登一次；重登后先只读对账，再仅补发“明确 401 / 从未派发 /
    # 显式失败”的任务。POST 超时或已收到成功响应的任务一律禁止自动补发。
    if (first.auth_error and not stop_event.is_set() and relogin is not None
            and not state.get("journal_error")):
        _emit(callback, "登录态过期，正在重新登录并对账…")
        relogin()
        after_login = _safe_reconcile(
            current, fetch_json, callback, "重登后站内对账",
            created_after=batch_window_start, attempts=_RECONCILE_POLL_ATTEMPTS)
        if after_login is None:
            stop_event.set()
            state["reconcile_failed"] = True
            _record_final(None, False)
            return None, False
        if after_login.duplicate_count:
            _emit(callback, "重登后发现站内重复，已停止且不会自动取消或补发")
            stop_event.set()
            merged = _merge_reconciliation(preconfirmed, after_login)
            _record_final(merged, True)
            return merged, True
        # 重登后对账确认的订单也要计入最终 confirmed，否则 created 数会少算。
        preconfirmed.update(after_login.confirmed)
        _resolve_confirmed_uncertain(after_login)
        current = list(after_login.missing)
        allowed = auth_ids | not_sent_ids | failure_ids
        resend = [
            task for task in current
            if str(task.get("identifier")) in allowed
            and str(task.get("identifier")) not in uncertain_ids
            and str(task.get("identifier")) not in success_ids
        ]
        if state.get("journal_error"):
            stop_event.set()
        if resend and not stop_event.is_set():
            _emit(callback, f"重登后仅补发 {len(resend)} 单明确未发送/显式失败的任务；"
                            "超时与已返回成功的单只做只读对账")
            second = run_round(resend, max_workers)
            if second.balance_error:
                balance_error = balance_error or second.balance_error
                _emit(callback, f"余额不足，已停止后续下单：{second.balance_error}")
                stop_event.set()
            if second.auth_error:
                _emit(callback, "重新登录后仍遇到 401，停止自动提交并以站内对账为准")
                for task in resend:
                    state["errors"].setdefault(str(task.get("identifier")),
                                                "重新登录后仍遇到 401")
                stop_event.set()
        else:
            _emit(callback, "重登后没有可自动补发的任务；"
                            "超时/已返回成功单仅通过只读对账确认")
    elif first.auth_error and not stop_event.is_set():
        _emit(callback, "登录态过期但当前模式无法重登，停止自动提交并以站内对账为准")
        stop_event.set()

    poll_attempts = (
        _RECONCILE_POLL_ATTEMPTS
        if not (stop_event.is_set() or balance_error or state.get("journal_error")) else 1
    )
    final_current = _safe_reconcile(
        current, fetch_json, callback, "收尾站内对账",
        created_after=batch_window_start, attempts=poll_attempts,
        zero_retry_delay=_PREFILTER_ZERO_RETRY_DELAY_S)
    if final_current is None:
        stop_event.set()
        state["reconcile_failed"] = True
        _record_final(None, False)
        return None, False
    final = _merge_reconciliation(preconfirmed, final_current)
    if final.duplicate_count:
        _emit(callback, "收尾对账发现站内重复，已停止且不会自动取消")
        stop_event.set()
        _record_final(final, True)
        return final, True
    _resolve_confirmed_uncertain(final)
    if stop_event.is_set() or balance_error or state.get("journal_error"):
        _record_final(final, True)
        return final, True

    # 对账后仍缺失的单才允许用户决策。超时/已返回成功的单绝不重发，
    # 只有显式失败或确认从未派发的任务才允许在重试前对账后串行重试一次。
    for task in list(final.missing):
        identifier = str(task.get("identifier"))
        if identifier in uncertain_ids:
            _emit(callback, f"订单未确认：{identifier}：POST 发送结果未知；"
                            "本批不自动重发，继续只读复查站内列表；"
                            "若复查后仍缺失，可由用户主动再运行一次"
                            "（再运行会先做站内对账，但列表延迟时仍可能重复下单）")
            continue
        if identifier in success_ids:
            _emit(callback, f"订单未确认：{identifier}：POST 已返回成功但站内尚未查到；"
                            "先按列表延迟处理：继续只读复查，本批不自动重发；"
                            "必要时可由用户主动再运行（已返回成功的单重发有重复风险）")
            continue
        error = state["errors"].get(identifier, "站内未查询到对应订单")
        _emit(callback, f"订单未确认：{identifier}：{error}")
        if decision_callback is None:
            continue
        decision = decision_callback(identifier, error).lower()
        if decision == "stop":
            stop_event.set()
            break
        if decision != "retry":
            continue
        if identifier not in failure_ids and identifier not in not_sent_ids:
            _emit(callback, f"订单 {identifier} 不属于显式失败/未发送，拒绝重试；仅做只读核对")
            continue
        before_retry = _safe_reconcile(
            current, fetch_json, callback, "重试前站内对账",
            created_after=batch_window_start, attempts=_RECONCILE_POLL_ATTEMPTS,
            zero_retry_delay=_PREFILTER_ZERO_RETRY_DELAY_S)
        if before_retry is None:
            stop_event.set()
            state["reconcile_failed"] = True
            _record_final(None, False)
            return None, False
        if before_retry.duplicate_count:
            _emit(callback, "重试前发现站内重复，已停止且不会自动取消")
            stop_event.set()
            merged = _merge_reconciliation(preconfirmed, before_retry)
            _record_final(merged, True)
            return merged, True
        if identifier not in {str(item.get("identifier")) for item in before_retry.missing}:
            _emit(callback, f"{identifier} 已在站内确认，跳过重试")
            final = _merge_reconciliation(preconfirmed, before_retry)
            _resolve_confirmed_uncertain(final)
            continue
        _emit(callback, f"重试 {identifier}（显式失败/未发送，已完成重试前只读对账）")
        retry = run_round([task], 1)
        if retry.balance_error:
            _emit(callback, f"余额不足，已停止后续下单：{retry.balance_error}")
            stop_event.set()
        if retry.auth_error:
            _emit(callback, "重试遇到 401，不再自动重登")
            stop_event.set()
        if state.get("journal_error"):
            stop_event.set()
        final_current = _safe_reconcile(
            current, fetch_json, callback, "重试后站内对账",
            created_after=batch_window_start,
            attempts=1 if stop_event.is_set() else _RECONCILE_POLL_ATTEMPTS,
            zero_retry_delay=_PREFILTER_ZERO_RETRY_DELAY_S)
        if final_current is None:
            stop_event.set()
            state["reconcile_failed"] = True
            _record_final(None, False)
            return None, False
        if final_current.duplicate_count:
            _emit(callback, "重试后发现站内重复，已停止且不会自动取消")
            stop_event.set()
            merged = _merge_reconciliation(preconfirmed, final_current)
            _record_final(merged, True)
            return merged, True
        final = _merge_reconciliation(preconfirmed, final_current)
        _resolve_confirmed_uncertain(final)
        if stop_event.is_set():
            break
    _record_final(final, True)
    return final, True
