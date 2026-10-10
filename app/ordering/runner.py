"""闪时送任务入口：从云端/本地读取订单，走完提交与对账全流程。"""

from __future__ import annotations

import datetime as _dt
import time
import uuid
from functools import wraps
from pathlib import Path
from typing import Any, Callable

from app.core.config import (
    _DEFAULT_SSS_SUBMIT_MIN_INTERVAL_S,
    _SSS_MAX_WORKERS_CEILING,
    _SSS_SUBMIT_MIN_INTERVAL_CEILING_S,
)
from app.integrations.api_client import SssApiClient
from app.integrations.sss_url import canonical_sss_origin
from app.order.common import _emit
from app.ordering.cloud_import import prepare_day_orders
from app.ordering.constants import (
    DEFAULT_SSS_URL,
    _CLIENT_IDEMPOTENCY_FIELD,
    _SSS_RUN_LOCK,
)
from app.ordering.models import _AuthExpired
from app.ordering.payload import (
    _cached_store_id,
    _collect_tasks,
    _fixed_address_from_config,
    _prepare_store_and_address,
    _run_dry_run,
)
from app.ordering.submission import (
    _balance_precheck,
    _format_balance,
    _make_api_submitter,
    _preflight_tasks,
    _run_reconciled_submission,
    _with_auth_relogin,
    query_balance,
)
from app.ordering.uncertain import (
    UncertainJournalError,
    append_uncertain_records,
    authoritative_uncertain_path,
    authority_location_gate,
    batch_key,
    batch_submission_lock,
    cross_scope_unresolved_records,
    describe_authority_location_conflicts,
    describe_cross_scope_conflicts,
    discard_uncertain_records,
    legacy_uncertain_paths,
    merge_journals,
    mirror_journal,
    mirror_uncertain_paths,
    normalise_account,
    platform_origin,
    resolve_pending_records,
    resolve_uncertain_records,
)
from app.ordering.workbook import (
    _validate_sss_orders,
    expected_delivery_date,
    load_sss_orders,
)

#: 配置对象缺 ``sss_max_workers`` 字段（或取值无法解析）时的保守回退：串行。
#: 正常路径上 ``AppConfig`` 始终带该字段（默认 4 路），因此这条只保护传了残缺
#: config 的调用方——宁可慢，也不要在拿不准并发语义时同时发多个非幂等 POST。
_FALLBACK_SSS_CREATE_WORKERS = 1


def resolve_create_workers(config: Any) -> int:
    """本批创建订单的并发路数：取配置值并夹到 ``[1, _SSS_MAX_WORKERS_CEILING]``。

    1 = 串行（平台限流或对账压力大时的回退值）。
    """
    try:
        workers = int(getattr(config, "sss_max_workers",
                              _FALLBACK_SSS_CREATE_WORKERS))
    except (TypeError, ValueError):
        workers = _FALLBACK_SSS_CREATE_WORKERS
    return max(1, min(_SSS_MAX_WORKERS_CEILING, workers))


def resolve_submit_min_interval_s(config: Any) -> float:
    """本批提交的最小间隔（秒）：取配置值并夹到 ``[0, 60]``；0 = 关闭节流。

    间隔**只来自配置**且运行期固定：平台返回的建单内部异常
    （``IndexOutOfBoundsException`` 等）既不是“受理超速”的证据，也不能被当成
    “没有落单”，因此不据此自动放宽或收紧（当晚实测已推翻“受理上限约 1 单 /
    2.1 秒”的推断，见 docs/SSS-下单失败与重试排查.md 的修正段）。拿不到配置
    （缺字段或取值不可解析）时按出厂默认（2.5 秒）保守节流——它只错开发送起点、
    把快速失败的兜圈速度限制在可预期范围内，不改变结果分类与重发语义。
    """
    try:
        interval = float(getattr(config, "sss_submit_min_interval_s",
                                 _DEFAULT_SSS_SUBMIT_MIN_INTERVAL_S))
    except (TypeError, ValueError):
        interval = _DEFAULT_SSS_SUBMIT_MIN_INTERVAL_S
    if interval != interval:  # NaN
        interval = _DEFAULT_SSS_SUBMIT_MIN_INTERVAL_S
    return max(0.0, min(_SSS_SUBMIT_MIN_INTERVAL_CEILING_S, interval))


def _count_text(value: Any, unit: str = "") -> str:
    """计数的显示文本；``None``（没读取到/对账未完成）写「待核对」，绝不写成 0。"""
    if value is None:
        return "待核对"
    try:
        number = int(value)
    except (TypeError, ValueError):
        return "待核对"
    return f"{number} {unit}".strip()


def _format_submission_stats(stats: dict[str, Any]) -> str:
    """把 ``summary.submission`` 渲染成一行精确计数（结束日志与 Bridge 消息共用）。

    只呈现真实计数：响应成功/技术异常/明确拒绝/未发送/本轮新增站内确认都与
    「最终确认」分开列，站内数量未读取到时写「待核对」，不写成 0。``created``
    只代表站内确认总数，**不说成“新建”**：总确认 = 提交前已有 + 本轮新增。
    """
    parts = [
        f"目标 {_count_text(stats.get('target_total'), '单')}",
        f"提交前已有 {_count_text(stats.get('preconfirmed'), '单')}",
        f"本轮提交 {_count_text(stats.get('submitted'), '单')}"
        f"（{_count_text(stats.get('attempts'), '次')} POST）",
        f"成功响应 {_count_text(stats.get('success_responses'), '次')}",
        f"技术异常（结果未知）{_count_text(stats.get('technical_errors'), '次')}",
        f"明确拒绝 {_count_text(stats.get('explicit_rejections'), '次')}",
    ]
    for key, label in (("auth_rejections", "登录失效"),
                       ("balance_rejections", "余额不足")):
        value = stats.get(key)
        if isinstance(value, int) and not isinstance(value, bool) and value > 0:
            parts.append(f"{label} {value} 次")
    parts.extend([
        f"未发送 {_count_text(stats.get('not_sent'), '单')}",
        f"本轮新增站内确认 {_count_text(stats.get('newly_confirmed'), '单')}",
        f"总确认 {_count_text(stats.get('confirmed'))}"
        f"/{_count_text(stats.get('target_total'))}",
        f"未确认 {_count_text(stats.get('unconfirmed'), '单')}",
    ])
    return "；".join(parts)


def _confirmed_clause(stats: dict[str, Any] | None, legacy: str) -> str:
    """确认口径短句：``站内确认 51/99（提交前已有 26，本轮新增 25）``。

    未知（对账未完成）时写「站内确认 待核对/N（未完成对账）」，不退化成 0。
    """
    if stats is None:
        return legacy
    confirmed = stats.get("confirmed")
    if confirmed is None:
        return (f"站内确认 待核对/{_count_text(stats.get('target_total'))}"
                "（未完成对账）")
    text = (f"站内确认 {_count_text(confirmed)}"
            f"/{_count_text(stats.get('target_total'))}")
    preconfirmed = stats.get("preconfirmed")
    newly = stats.get("newly_confirmed")
    if preconfirmed is not None or newly is not None:
        text += (f"（提交前已有 {_count_text(preconfirmed)}，"
                 f"本轮新增 {_count_text(newly)}）")
    return text


def _pending_clause(stats: dict[str, Any] | None, legacy_missing: int) -> str:
    """未确认数量短句；对账没完成时写「未确认数量待核对」，不按目标总数冒充。"""
    if stats is None:
        return f"{legacy_missing} 单未确认"
    value = stats.get("unconfirmed")
    return ("未确认数量待核对" if value is None
            else f"{_count_text(value)} 单未确认")


def _submission_stats_skeleton(target_total: int, **overrides: Any) -> dict[str, Any]:
    """没进入「提交 + 对账」流程的运行（无单/干跑/预检/余额闸门/并发锁）的计数。

    一条 POST 都没发：提交与响应计数诚实为 0；站内数量没有读取到，因此
    ``preconfirmed`` / ``newly_confirmed`` / ``confirmed`` / ``unconfirmed`` 用
    ``None``（未知），不冒充 0。``not_sent`` 与提交层同口径：初始对账未知时按
    全部目标任务计（这些任务始终没有 POST）。
    """
    stats: dict[str, Any] = {
        "target_total": int(target_total),
        "preconfirmed": None,
        "submitted": 0,
        "attempts": 0,
        "success_responses": 0,
        "technical_errors": 0,
        "explicit_rejections": 0,
        "auth_rejections": 0,
        "balance_rejections": 0,
        "not_sent": int(target_total),
        "newly_confirmed": None,
        "confirmed": None,
        "unconfirmed": None,
        "reconciled": False,
    }
    stats.update(overrides)
    return stats


def _exclusive_sss_job(func: Callable[..., Any]) -> Callable[..., Any]:
    """同一进程内拒绝两个闪时送下单任务并发提交。"""
    @wraps(func)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        if not _SSS_RUN_LOCK.acquire(blocking=False):
            raise RuntimeError("已有闪时送下单任务正在运行，拒绝并发执行")
        try:
            return func(*args, **kwargs)
        finally:
            _SSS_RUN_LOCK.release()
    return wrapper


@_exclusive_sss_job
def run_sss_job(config: Any, stop_event: Any,
                progress_callback: Callable[[str], Any] | None = None,
                password: str | None = None,
                decision_callback: Callable[[str, str], str] | None = None,
                captcha_callback: Callable[[bytes], str] | None = None,
                store_cache_callback: Callable[[str, int], None] | None = None) -> dict[str, Any]:
    """按配置的名单来源读取当天订单，并通过接口批量创建预约单。

    名单来源（``config.sss_order_source``）：

    - ``wps``（默认）：下单前从 WPS 云端的「东湖中餐 / 东湖晚餐」读取当天
      （运行时刻 20:00 之后识别次日，其余时刻识别运行日；见
      ``sss_import.ordering_target_date``）标 ``1`` 的人，地址是「大西 / 小」的不下单；
      名单直接用内存数据下单，同时写一份到《闪时送.xlsx》留档。云端读不到、
      数据不完整或「识别日期 ≠ 本次送达日期」时**一律拒绝下单**（不登录、不提交）。
    - ``excel``：旧行为，读《闪时送.xlsx》（人工准备名单时的兜底）。

    ``decision_callback(identifier, error)`` 返回 ``retry``/``skip``/``stop``，
    用于单个订单创建失败时的交互决策（与订单处理的 order_decision 一致）。
    ``config.sss_dry_run`` 为真时只组装并打印报文，不真实提交（且跳过
    登录与门店/地址查询，无需验证码）。
    ``captcha_callback`` 在纯接口模式接收验证码 PNG 字节，返回用户输入的验证码。
    下单阶段并发提交（``config.sss_max_workers``，默认 4），提交前后均会
    查询站内订单列表；状态不确定时绝不直接重复 POST。
    """
    load_start = time.perf_counter()
    now = _dt.datetime.now()
    source = str(getattr(config, "sss_order_source", "wps") or "wps").strip().lower()
    if source != "excel":
        source = "wps"
    configured_excel = getattr(config, "sss_excel_path", None)
    excel_path = Path(configured_excel) if configured_excel else None
    import_summary: dict[str, Any] | None = None

    if source == "wps":
        # 云端当天名单：读云端 → 地址过滤 → 校验 → 留档；任何问题都在这里
        # 抛 ImportRefused，此时还没有登录、没有提交任何订单。
        day = prepare_day_orders(config, now=now,
                                 delivery_date=expected_delivery_date(now),
                                 log=progress_callback)
        orders_by_sheet = day.orders_by_sheet
        import_summary = day.as_summary()
    else:
        if excel_path is None:
            raise FileNotFoundError("尚未选择闪时送 Excel 文件")
        if not excel_path.is_file():
            raise FileNotFoundError(f"Excel 文件不存在: {excel_path}")
        if excel_path.suffix.lower() not in {".xlsx", ".xlsm"}:
            raise ValueError("仅支持 .xlsx 和 .xlsm Excel 文件")
        orders_by_sheet = load_sss_orders(excel_path)

    def _result(payload: dict[str, Any]) -> dict[str, Any]:
        """给结果补上名单来源与云端导入摘要（界面/日志用）。"""
        payload["source"] = source
        if import_summary is not None:
            payload["import"] = import_summary
        return payload

    def _finish(payload: dict[str, Any], *, status: str, confirmed: int = 0,
                unconfirmed: int = 0, stopped: bool = False, failed: int = 0,
                next_action: str = "", **summary_extra: Any) -> dict[str, Any]:
        """统一给结果补 status/next_action/summary，保留旧键与旧入口签名。"""
        summary = {
            "status": status,
            "confirmed": int(confirmed),
            "unconfirmed": int(unconfirmed),
            "stopped": bool(stopped),
            "failed": int(failed),
            "next_action": next_action,
        }
        summary.update(summary_extra)
        payload = dict(payload)
        payload["status"] = status
        payload["next_action"] = next_action
        payload["summary"] = summary
        return _result(payload)

    _validate_sss_orders(orders_by_sheet)
    total = sum(len(orders) for orders in orders_by_sheet.values())
    if total == 0:
        if source == "wps":
            _emit(progress_callback,
                  "当天云端名单为空（没有当天日期列，或标 1 的人都是「大西/小」），本次不下单")
        else:
            _emit(progress_callback, "闪时送 Excel 中没有任何订单")
        return _finish({"processed": 0, "created": 0}, status="no_orders",
                       next_action="没有需要下单的订单，无需操作",
                       submission=_submission_stats_skeleton(0))
    _emit(progress_callback,
          f"读取订单表耗时 {(time.perf_counter() - load_start):.1f} 秒，共 {total} 单")
    if password is None:
        password = ""
    dry_run = bool(getattr(config, "sss_dry_run", False))
    store_name = str(getattr(config, "sss_store_name", "") or "一口轻食")
    common_address = str(getattr(config, "sss_common_address", "") or "")
    use_fixed_address = bool(getattr(config, "sss_use_fixed_address", False))
    goods_name = str(getattr(config, "sss_product_name", "") or "轻食")
    # 出厂并发 1（v3 起，串行）；旧配置里等于历史出厂默认的 8 / 4 由 AppConfig 的
    # 逐代迁移搬过来，用户显式改过的其它取值原样生效。
    max_workers = resolve_create_workers(config)
    submit_min_interval_s = resolve_submit_min_interval_s(config)
    batch_id = uuid.uuid4().hex[:12]
    idempotency_field = str(getattr(config, "sss_idempotency_field", "") or _CLIENT_IDEMPOTENCY_FIELD).strip()

    # 干跑短路：组装报文即返回，不取验证码、不登录、不查门店地址。
    try:
        read_timeout_s = float(getattr(config, "sss_read_timeout_s", 20.0))
    except (TypeError, ValueError):
        read_timeout_s = 20.0
    read_timeout_s = max(1.0, min(120.0, read_timeout_s))
    url = str(getattr(config, "sss_url", "") or "").strip() or DEFAULT_SSS_URL
    if dry_run:
        store_id = _cached_store_id(config, store_name) or 0
        if store_id:
            _emit(progress_callback, f"门店「{store_name}」命中缓存（id={store_id}）")
        if use_fixed_address:
            address = _fixed_address_from_config(config)
        else:
            _emit(progress_callback, "干跑模式：跳过常用地址查询，用占位地址组装报文")
            address = {"lnt": 0.0, "lat": 0.0, "areaCode": "",
                       "addressDetail": common_address or "干跑占位"}
        tasks = _collect_tasks(
            orders_by_sheet, store_id, address, goods_name,
            account=str(getattr(config, "sss_account", "") or ""),
            batch_id=batch_id, idempotency_field=idempotency_field, now=now)
        preview = _run_dry_run(tasks, progress_callback)
        return _finish(preview, status="dry_run", confirmed=0,
                       unconfirmed=len(tasks), next_action="干跑未发送任何 POST；确认报文后再正式运行",
                       submission=_submission_stats_skeleton(len(tasks)))

    account = str(getattr(config, "sss_account", "") or "")
    if not account:
        raise ValueError("尚未填写闪时送账号")

    # R8-S1：非规范/不支持的网址在产生任何闪时送请求之前就以明确的配置错误
    # 终止（与配置保存入口、SssApiClient 共用 app.integrations.sss_url 规则）。
    canonical_sss_origin(url)
    # 校验通过后按**冻结的配置字符串**（不是实时 config）算 origin：既保持
    # “只有一套 origin 规范化”，又保证执行期配置被改写不会漂移作用域。
    origin = platform_origin(config, url=url)

    job_start = time.perf_counter()
    _emit(progress_callback, "正在获取闪时送验证码…")
    client = SssApiClient(origin, account, password or "",
                          timeout=(5.0, read_timeout_s),
                          pool_size=max_workers)
    _batch_guard: Any = None
    try:
        captcha = client.fetch_captcha()
        if captcha_callback is None:
            raise RuntimeError("纯接口模式需要验证码输入回调，当前界面未提供 captcha 弹窗")
        code = captcha_callback(captcha)
        _emit(progress_callback, "正在登录闪时送…")
        client.login(code)

        def relogin() -> None:
            _emit(progress_callback, "登录态过期，重新获取验证码并登录…")
            img = client.fetch_captcha()
            if captcha_callback is None:
                raise RuntimeError("纯接口模式需要验证码输入回调")
            client.login(captcha_callback(img))
            _emit(progress_callback, "重新登录成功")

        def prepare_session_data() -> tuple[int, dict[str, Any], tuple[float | None, float | None]]:
            _emit(progress_callback, "登录成功，读取门店与常用地址…")
            prep_start = time.perf_counter()
            # 接口模式也串行：requests.Session 不保证线程安全，门店/地址两个
            # GET 并发共用同一 Session 会出现偶发失败。
            store_id, address = _prepare_store_and_address(
                client.get_json, config, store_name, common_address,
                use_fixed_address, progress_callback, parallel=False,
                store_cache_callback=store_cache_callback)
            _emit(progress_callback,
                  f"门店与地址准备耗时 {(time.perf_counter() - prep_start):.1f} 秒")
            balance = query_balance(client.get_json)
            return store_id, address, balance

        store_id, address, (balance_total, balance_frozen) = _with_auth_relogin(
            prepare_session_data, relogin, progress_callback, "读取门店/地址/余额")
        _emit(progress_callback, _format_balance(balance_total, balance_frozen))

        tasks = _collect_tasks(
            orders_by_sheet, store_id, address, goods_name,
            account=account, batch_id=batch_id, idempotency_field=idempotency_field,
            now=now)
        if idempotency_field:
            _emit(progress_callback, f"已启用客户端幂等字段「{idempotency_field}」")
        else:
            _emit(progress_callback, "平台接口未探测到客户端幂等字段：采用“至少一次提交 + 对账确认”语义，不承诺 exactly-once")

        # 权威共享不确定状态：所有实例必须读写同一个权威 path，不能随
        # sss_uncertain_path 改变；旧路径只做迁移/镜像，避免切路径绕过未决订单。
        # R8-S1：这里显式传入**启动时已验证并冻结**的 origin/account，之后
        # journal 元数据与跨作用域核对全部复用同一份身份；即使执行期配置被
        # 改写，也不会把未决记录写到另一个 scope。
        authoritative_journal = authoritative_uncertain_path(
            config, origin=origin, account=account)
        # 旧哈希/旧默认/旧全局文件只作为只读迁移来源；只有显式配置的
        # sss_uncertain_path / YIKOU_SSS_UNCERTAIN_PATH 才允许镜像写回。
        migration_sources = legacy_uncertain_paths(config, authoritative_journal)
        mirror_journals = mirror_uncertain_paths(config, authoritative_journal)

        def _sync_journal_mirrors() -> None:
            for mirror_path in mirror_journals:
                mirror_journal(authoritative_journal, mirror_path)

        journal_key = batch_key(expected_delivery_date(now), source, account)
        # SN-C1：业务批次级跨进程锁必须覆盖“读 unresolved → 对账 → POST → 结果写盘”
        # 全过程。锁内重新读取权威 journal，不使用锁外快照；获取失败直接拒绝提交。
        _batch_guard = batch_submission_lock(authoritative_journal, journal_key)
        try:
            _batch_guard.acquire()
        except UncertainJournalError as exc:
            _emit(progress_callback,
                  f"批次级跨进程锁不可用：{exc}；另一个进程可能正在处理该批次，"
                  "本进程不会提交任何 POST")
            stop_event.set()
            return _finish({
                "processed": len(tasks), "created": 0, "submitted": 0,
                "previewed": 0, "stopped": True, "partial": False,
                "reconciled": False, "uncertain": True,
                "semantics": "batch-submission-lock",
            }, status="blocked_concurrent", unconfirmed=len(tasks), stopped=True,
               next_action="另一进程正在处理同一批次或跨进程锁不可用；未发送任何 POST，"
                           "请等待锁释放后重试，严禁并行重跑本批",
               submission=_submission_stats_skeleton(len(tasks)))
        # 持锁后先把旧/镜像 journal 的 unresolved 合并进权威共享状态，再做
        # 只读对账；迁移或镜像失败一律 fail-closed。
        #
        # R8-S1：历史 URL 写法（尾点/IDN 等）会把未决记录拆到另一个 authority
        # scope。持锁后、迁移/对账/POST 之前，先按规范化账号只读扫描本机权威根；
        # 发现**无法安全归属**给当前账号的活跃 unresolved（旧写法不再受支持、
        # 平台/账号字段缺失）一律保守阻断；不同规范 origin 的平台不误伤。
        # 该扫描只读：不迁移、不改写、不删除旧记录，也不依据 DNS 等价合并身份。
        # 3.6.19 起扫描结果只提示、不再阻断本批（「人工核对后才能重跑」的流程已下线；
        # 再运行会先做站内对账、只补仍缺失项，但平台列表延迟时仍可能重复下单）。
        try:
            cross_scope = cross_scope_unresolved_records(
                authoritative_journal, config=config, account=account,
                origin=origin)
        except UncertainJournalError as exc:
            _emit(progress_callback,
                  f"跨作用域未决记录只读核对失败（仅提示，不阻断本批）：{exc}")
            cross_scope = []
        if cross_scope:
            _emit(progress_callback,
                  f"提示：发现 {len(cross_scope)} 条属于当前账号但无法安全归属的活跃"
                  f"未决记录：{describe_cross_scope_conflicts(cross_scope)}；"
                  "本批继续提交，记录保留在原处供排查（不会再要求人工核对后才能重跑）。")
        # R8-S3：显式权威位置（config.sss_authoritative_uncertain_path /
        # YIKOU_SSS_AUTHORITATIVE_PATH）启用、切换或取消时，旧位置的活跃
        # unresolved 会变得不可见。这里在锁内、迁移/对账/POST 之前做一次
        # 「位置切换检查」：核对其他已知位置（默认权威根 + 部署级登记位置）里
        # 属于当前账号的活跃未决记录；3.6.19 起有冲突也只提示、不阻断本批
        # （否则重跑会被历史记录卡住）；该检查只读 journal、只写登记文件。
        try:
            location_gate = authority_location_gate(
                authoritative_journal, config=config, account=account,
                origin=origin)
        except UncertainJournalError as exc:
            _emit(progress_callback,
                  f"权威位置登记/切换检查失败（仅提示，不阻断本批）：{exc}")
            location_gate = None
        if location_gate is not None:
            if location_gate["pruned"]:
                _emit(progress_callback,
                      "权威位置登记：已移除不存在的旧位置 "
                      + "、".join(location_gate["pruned"]))
            if location_gate["conflicts"]:
                detail = describe_authority_location_conflicts(
                    location_gate["conflicts"])
                _emit(progress_callback,
                      f"提示：其他权威位置仍有当前账号的 "
                      f"{len(location_gate['conflicts'])} 条活跃未决记录：{detail}；"
                      "本批继续提交，记录保留在原处供排查。")
        try:
            migration = merge_journals(
                authoritative_journal, migration_sources,
                scope={"platform": origin,
                       "account": normalise_account(account)})
            if migration["merged"]:
                _emit(progress_callback,
                      f"已把旧 journal 的 {migration['merged']} 条 unresolved "
                      f"合并到权威共享状态：{authoritative_journal}")
            _sync_journal_mirrors()
            pending_journal, _journal_recon, journal_resolved = resolve_pending_records(
                authoritative_journal, journal_key, client.get_json, progress_callback)
            _sync_journal_mirrors()
        except UncertainJournalError as exc:
            # 3.6.19：读取/迁移失败不再阻断本批（历史记录只是供排查的本地账本，
            # 不再有任何“必须先人工处理”的闸门）；写盘失败仍会在运行时停止提交。
            _emit(progress_callback,
                  f"权威/旧 journal 迁移或读取失败（仅提示，不阻断本批）：{exc}；"
                  "记录保留在原处，本批继续提交")
            pending_journal, journal_resolved = [], 0
        if journal_resolved:
            _emit(progress_callback,
                  f"跨运行不确定记录：只读对账已确认并清理 {journal_resolved} 条")
        if pending_journal:
            _emit(progress_callback,
                  f"提示：{len(pending_journal)} 条历史“已发送未知”记录仍未在站内确认；"
                  "本批继续提交（提交前的站内对账会避免重复提交已在站内的订单）。")

        try:
            unit_price = float(getattr(config, "sss_unit_price", 0) or 0)
        except (TypeError, ValueError):
            unit_price = 0
        if unit_price > 0:
            estimate = round(unit_price * len(tasks), 2)
            _emit(progress_callback,
                  f"本批 {len(tasks)} 单预计送完结算约 {estimate} 元"
                  + (f"，当前可用 {balance_total}"
                     if balance_total is not None else "，当前余额未知"))

        balance_status, estimate = _balance_precheck(
            tasks, balance_total, unit_price, progress_callback)
        if balance_status != "ok":
            if balance_status == "insufficient_balance":
                next_action = "余额不足，本批未发送任何 POST；充值后先只读核对再运行"
            else:
                next_action = "余额未知，本批未发送任何 POST；确认余额后先只读核对再运行"
            return _finish({
                "processed": len(tasks), "created": 0, "submitted": 0,
                "previewed": 0, "stopped": True, "partial": False,
                "reconciled": False, "uncertain": balance_status == "balance_unknown",
                "estimate": estimate,
                "semantics": "pre-submit-balance-guard",
            }, status=balance_status, unconfirmed=len(tasks), stopped=True,
               next_action=next_action,
               submission=_submission_stats_skeleton(len(tasks)))

        if bool(getattr(config, "sss_preflight", False)):
            preflight_status, preflight = _preflight_tasks(
                tasks, client.get_json, progress_callback)
            preflight_confirmed = len(preflight.confirmed) if preflight else 0
            # 预检是只读对账、一条 POST 都没发：站内数量在对账成功时来自真实
            # 匹配（本轮新增站内确认 = 0，因为没有任何提交），对账失败则一律
            # 未知，不按 0 或目标总数冒充。
            preflight_stats = _submission_stats_skeleton(
                len(tasks),
                preconfirmed=preflight_confirmed if preflight is not None else None,
                not_sent=(len(tasks) - preflight_confirmed
                          if preflight is not None else len(tasks)),
                newly_confirmed=0 if preflight is not None else None,
                confirmed=preflight_confirmed if preflight is not None else None,
                unconfirmed=(len(tasks) - preflight_confirmed
                             if preflight is not None else None),
                reconciled=preflight is not None,
            )
            return _finish({
                "processed": len(tasks),
                "created": preflight_confirmed,
                "submitted": 0, "previewed": 0,
                "stopped": preflight_status != "preflight_ok",
                "partial": False,
                "reconciled": preflight is not None,
                "uncertain": preflight is None,
                "balance_total": balance_total,
                "estimate": estimate,
                "semantics": "preflight-only",
            }, status=preflight_status, confirmed=preflight_confirmed,
               unconfirmed=len(tasks) - preflight_confirmed,
               stopped=preflight_status != "preflight_ok",
               failed=0 if preflight is not None else len(tasks),
               next_action=("预检只读模式，未提交新订单；确认后再正式运行"
                            if preflight_status == "preflight_ok"
                            else "预检未通过或对账不确定，未提交任何 POST；先处理日志风险"),
               submission=preflight_stats)

        _emit(progress_callback,
              f"开始下单：共 {len(tasks)} 单，并发 {max_workers} 路，读取超时 {read_timeout_s:g}s"
              + (f"，提交最小间隔 {submit_min_interval_s:g}s"
                 if submit_min_interval_s > 0 else ""))
        journal_meta = {
            "delivery_date": expected_delivery_date(now).isoformat(),
            "source": source,
            "account": account,
            "platform": origin,
            "batch_started_at": time.time(),
        }

        def _journal_sink(entries: list[dict[str, Any]],
                          meta: dict[str, Any]) -> None:
            append_uncertain_records(authoritative_journal, journal_key, entries, meta=meta)
            _sync_journal_mirrors()

        def _journal_clear(identifiers: set[str]) -> None:
            resolve_uncertain_records(authoritative_journal, journal_key, identifiers)
            _sync_journal_mirrors()

        def _journal_discard(identifiers: set[str]) -> None:
            discard_uncertain_records(authoritative_journal, journal_key, identifiers)
            _sync_journal_mirrors()

        outcome: dict[str, Any] = {}
        submit_start = time.perf_counter()
        final, reconciled = _run_reconciled_submission(
            tasks,
            lambda: _make_api_submitter(client),
            client.get_json,
            stop_event,
            progress_callback,
            decision_callback,
            max_workers,
            relogin=relogin,
            read_timeout_s=read_timeout_s,
            submit_min_interval_s=submit_min_interval_s,
            uncertain_sink=_journal_sink,
            uncertain_clear=_journal_clear,
            uncertain_discard=_journal_discard,
            journal_meta=journal_meta,
            outcome=outcome,
        )
        created = len(final.confirmed) if final is not None else 0
        processed = len(tasks)
        # 本轮提交计数只认提交层从**真实调用与对账集合**得到的 submission_stats；
        # runner 只补 target_total（本批目标单数）。绝不从进度里程碑/日志推算。
        raw_stats = outcome.get("submission_stats")
        submission_stats = ({**raw_stats, "target_total": processed}
                            if isinstance(raw_stats, dict) else None)
        if submission_stats is not None:
            _emit(progress_callback,
                  "本轮提交统计：" + _format_submission_stats(submission_stats))
        confirmed_clause = _confirmed_clause(submission_stats,
                                             f"确认 {created}/{processed}")
        _emit(progress_callback,
              f"下单与对账耗时 {(time.perf_counter() - submit_start):.1f} 秒，"
              f"{confirmed_clause}")
        try:
            end_total, end_frozen = query_balance(client.get_json)
        except _AuthExpired:
            _emit(progress_callback, "余额查询时登录态失效，正在重新登录…")
            relogin()
            try:
                end_total, end_frozen = query_balance(client.get_json)
            except _AuthExpired as exc:
                # 进度回调只接收一个参数（Bridge 传的是 lambda msg: self.log(msg)），
                # 旧代码多传了一个 "WARN"，会在这里抛 TypeError 把整批任务打成 error。
                _emit(progress_callback, f"重新登录后余额查询仍失败：{exc}")
                end_total, end_frozen = None, None
        _emit(progress_callback, "结束" + _format_balance(end_total, end_frozen))

        uncertain_ids = [str(item) for item in (outcome.get("uncertain") or [])]
        failure_ids = [str(item) for item in
                       (outcome.get("failure_ids") or outcome.get("failures") or [])]
        not_sent_ids = [str(item) for item in (outcome.get("not_sent") or [])]
        success_response_ids = [str(item) for item in
                                (outcome.get("success_responses") or [])]
        journal_error = str(outcome.get("journal_error") or "")
        stopped = bool(stop_event.is_set())
        partial = bool(reconciled and final is not None and final.missing and not stopped)
        reconcile_failed = bool(outcome.get("reconcile_failed")) or not reconciled
        if journal_error:
            status = "failed"
            next_action = (f"{journal_error}；本批已停止提交。修复后可直接再运行一次"
                           "核对并补单：先站内对账、只补仍缺失项；"
                           "平台列表延迟时仍可能重复下单")
        elif reconcile_failed or final is None:
            status = "failed"
            next_action = ("站内对账失败，无法确认创建数量；可直接再运行一次核对并补单："
                           "先站内对账、只补仍缺失项；平台列表延迟时仍可能重复下单")
        elif stopped:
            status = "stopped"
            next_action = ("任务已停止；未确认单可直接再运行一次核对并补单："
                           "先站内对账、只补仍缺失项；平台列表延迟时仍可能重复下单")
        elif uncertain_ids or (final is not None and final.missing):
            status = "unconfirmed"
            next_action = ("存在未确认或“已发送未知”的订单；可直接再运行一次核对并补单："
                           "先站内对账、只补仍缺失项；平台列表延迟时仍可能重复下单；"
                           "本批不会自动重发")
        else:
            status = "confirmed"
            next_action = "已全部对账确认，无需重复提交"
        failed_count = len(failure_ids) + (1 if journal_error else 0)

        if status == "failed":
            _emit(progress_callback,
                  f"闪时送任务失败：{confirmed_clause}，"
                  f"{next_action}")
        elif status == "stopped":
            _emit(progress_callback,
                  f"闪时送任务已停止：{'已完成站内对账' if reconciled else '站内对账失败'}，"
                  f"{confirmed_clause}")
        elif status == "confirmed":
            _emit(progress_callback,
                  f"闪时送下单完成：{confirmed_clause}，"
                  f"总耗时 {(time.perf_counter() - job_start):.1f} 秒")
        else:
            _emit(progress_callback,
                  f"闪时送任务未确认：{confirmed_clause}，"
                  f"{_pending_clause(submission_stats, processed - created)}；{next_action}")

        result: dict[str, Any] = {
            "processed": processed,
            "created": created,
            "stopped": stopped,
            "partial": partial,
            "reconciled": reconciled,
            "uncertain": not reconciled,
            "semantics": ("idempotency-key+reconciliation"
                          if idempotency_field else "at-least-once+reconciliation"),
            "next_action": next_action,
        }
        if balance_total is not None:
            result["balance_total"] = balance_total
        if end_total is not None:
            result["balance_end"] = end_total
        return _finish(
            result, status=status, confirmed=created, unconfirmed=processed - created,
            stopped=stopped, failed=failed_count, next_action=next_action,
            uncertain=len(uncertain_ids), explicit_failures=len(failure_ids),
            not_sent=len(not_sent_ids), success_responses=len(success_response_ids),
            reconciled=reconciled,
            **({"submission": submission_stats} if submission_stats is not None else {}),
        )
    finally:
        if _batch_guard is not None:
            _batch_guard.release()
        client.close()
