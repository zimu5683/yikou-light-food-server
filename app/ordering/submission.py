"""闪时送下单提交、重登、余额检查与至少一次+对账收敛循环。"""

from __future__ import annotations

import json
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from threading import Lock, local
from typing import Any, Callable

from app.integrations.api_client import (
    ApiError, SssApiClient, SssTransportError,
    auth_error_message, is_auth_expired_payload,
)
from app.order.common import _emit
from app.ordering.common import _trace
from app.ordering.fingerprint import _task_fingerprint
from app.ordering.constants import (
    _ACCOUNT_PATH,
    _BALANCE_KEYWORDS,
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
    """平台明确拒绝且给出 message/code：有充分依据认为该单未落单。"""


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
    """把下单响应分成：明确成功 / 明确拒绝 / 结果不确定三类。

    只有明确 success 才放行；``success=false`` 且有 message/code 才视为
    “有充分依据确认未落单”的显式拒绝。空对象、空数组、缺 success 字段、
    code-only、畸形/未知格式都视为已发送未知，保留 prewrite，交给只读对账，
    绝不能 discard 后自动重发。
    """
    if not isinstance(resp, dict):
        raise _SubmissionUncertain(
            "平台 2xx 响应格式未知（无法确认下单结果）："
            + json.dumps(resp, ensure_ascii=False, default=str)[:200])
    if is_auth_expired_payload(resp):
        raise _AuthExpired(auth_error_message(resp, fallback="平台 2xx 响应表示登录态失效"))
    success = resp.get("success")
    if success is True or (isinstance(success, int) and not isinstance(success, bool)
                           and success == 1):
        return
    message = str(resp.get("message") or resp.get("msg") or "")
    lowered = message.lower()
    if any(kw.lower() in lowered or (kw in message) for kw in _BALANCE_KEYWORDS):
        raise _BalanceDepleted(message or json.dumps(resp, ensure_ascii=False)[:200])
    if success is False:
        code = resp.get("code")
        if code in (None, ""):
            code = resp.get("errorCode") or resp.get("error_code")
        has_code = code not in (None, "", 0, "0")
        if message.strip() or has_code:
            detail = message.strip() or f"平台明确拒绝（code={code}）"
            raise _ExplicitRejection(detail)
        # success=false 但没有任何 message/code：证据不足，不能确定未落单。
        raise _SubmissionUncertain(
            "平台返回 success=false 但缺少 message/code，无法确认是否落单")
    raise _SubmissionUncertain(
        "平台 2xx 响应缺少明确 success 字段，无法确认下单结果："
        + json.dumps(resp, ensure_ascii=False, default=str)[:200]
    )


def _is_auth_expired(exc: BaseException) -> bool:
    """判断是否为登录态过期，过期才值得打断整批去重登。

    闪时送实际使用 HTTP 200 + code=10000 + “token失效，请重新登陆”，
    因此不能只看 401。
    """
    text = str(exc)
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
        if _is_auth_expired(exc):
            raise _AuthExpired(str(exc)) from exc
        raise _SubmissionUncertain(str(exc)) from exc
    except Exception as exc:
        detail = str(exc) or type(exc).__name__
        raise _SubmissionUncertain(detail) from exc


def _submit_tasks_concurrent(tasks: list[dict[str, Any]],
                             submit: Callable[[dict[str, Any]], dict[str, Any]],
                             stop_event: Any,
                             callback: Callable[[str], Any] | None,
                             max_workers: int = 4,
                             on_stop: Callable[[], None] | None = None) -> _SubmitResult:
    """有界并发提交，停止、401、余额不足后不再派发新任务。

    同一轮最多保留 ``max_workers`` 个在途 POST。请求超时或断链只记录为
    ``uncertain``，由调用方查询订单列表确认，绝不在此处自动重发。
    """
    result = _SubmitResult()
    workers = max(1, int(max_workers))
    iterator = iter(tasks)
    in_flight: dict[Any, dict[str, Any]] = {}
    stop_closed = False

    def halted() -> bool:
        return bool(stop_event.is_set() or result.auth_error or result.balance_error)

    def run_one(task: dict[str, Any]) -> tuple[str, str, str]:
        if stop_event.is_set():
            return task["identifier"], "stopped", ""
        started = time.perf_counter()
        try:
            _check_success(submit(task["payload"]))
        except _AuthExpired as exc:
            state, detail = "auth", str(exc)
        except _BalanceDepleted as exc:
            state, detail = "balance", str(exc)
        except _ExplicitRejection as exc:
            state, detail = "failure", str(exc)
        except (_SubmissionUncertain, SssTransportError) as exc:
            state, detail = "uncertain", str(exc)
        except Exception as exc:
            # 非幂等 POST 一旦调用出去，非显式拒绝的异常一律是“已发送未知”；
            # 不能落入 failure/discard，否则平台已落单但响应异常时会重复 POST。
            state, detail = "uncertain", str(exc)
        else:
            state, detail = "success", ""
        _trace(f"POST {task['identifier']}: {time.perf_counter() - started:.2f}s -> {state}")
        return task["identifier"], state, detail

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
                if state == "success":
                    result.succeeded.add(identifier)
                    if len(result.succeeded) % _PROGRESS_EVERY_N == 0:
                        _emit(callback, f"本轮收到成功响应 {len(result.succeeded)}/{len(tasks)} 单")
                elif state == "failure":
                    result.failures.append((identifier, detail))
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
    原有两元组返回契约。
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

    def _absorb(round_result: _SubmitResult) -> None:
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
        _sync_outcome()
        return None, False
    if initial.duplicate_count:
        _emit(callback, "站内记录数超过 Excel 目标，已停止且不会自动取消或补发")
        stop_event.set()
        _sync_outcome()
        return initial, True
    if not initial.missing:
        _emit(callback, "Excel 订单均已在站内确认，本次不发送任何下单请求")
        _sync_outcome()
        return initial, True

    preconfirmed = set(initial.confirmed)
    current = list(initial.missing)
    balance_error = ""

    def run_round(round_tasks: list[dict[str, Any]], workers: int) -> _SubmitResult:
        result = _SubmitResult()
        if not _prepare_round_journal(round_tasks):
            result.stopped = True
            return result
        submit, close = submit_factory()
        try:
            result = _submit_tasks_concurrent(round_tasks, submit, stop_event,
                                              callback, workers, on_stop=close)
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
            _sync_outcome()
            return None, False
        if after_login.duplicate_count:
            _emit(callback, "重登后发现站内重复，已停止且不会自动取消或补发")
            stop_event.set()
            _sync_outcome()
            return _merge_reconciliation(preconfirmed, after_login), True
        # 重登后对账确认的订单也要计入最终 confirmed，否则 created 数会少算。
        preconfirmed.update(after_login.confirmed)
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
        _sync_outcome()
        return None, False
    final = _merge_reconciliation(preconfirmed, final_current)
    if final.duplicate_count:
        _emit(callback, "收尾对账发现站内重复，已停止且不会自动取消")
        stop_event.set()
        _sync_outcome()
        return final, True
    _resolve_confirmed_uncertain(final)
    if stop_event.is_set() or balance_error or state.get("journal_error"):
        _sync_outcome()
        return final, True

    # 对账后仍缺失的单才允许用户决策。超时/已返回成功的单绝不重发，
    # 只有显式失败或确认从未派发的任务才允许在重试前对账后串行重试一次。
    for task in list(final.missing):
        identifier = str(task.get("identifier"))
        if identifier in uncertain_ids:
            _emit(callback, f"订单未确认：{identifier}：POST 发送结果未知；"
                            "下一步只做只读核对，禁止重试或重跑")
            continue
        if identifier in success_ids:
            _emit(callback, f"订单未确认：{identifier}：POST 已返回成功但站内尚未查到；"
                            "下一步只做只读核对，禁止重发")
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
            _sync_outcome()
            return None, False
        if before_retry.duplicate_count:
            _emit(callback, "重试前发现站内重复，已停止且不会自动取消")
            stop_event.set()
            _sync_outcome()
            return _merge_reconciliation(preconfirmed, before_retry), True
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
            _sync_outcome()
            return None, False
        if final_current.duplicate_count:
            _emit(callback, "重试后发现站内重复，已停止且不会自动取消")
            stop_event.set()
            _sync_outcome()
            return _merge_reconciliation(preconfirmed, final_current), True
        final = _merge_reconciliation(preconfirmed, final_current)
        _resolve_confirmed_uncertain(final)
        if stop_event.is_set():
            break
    _sync_outcome()
    return final, True
