"""闪时送任务入口：从云端/本地读取订单，走完提交与对账全流程。"""

from __future__ import annotations

import datetime as _dt
import time
import uuid
from functools import wraps
from pathlib import Path
from typing import Any, Callable

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
                       next_action="没有需要下单的订单，无需操作")
    _emit(progress_callback,
          f"读取订单表耗时 {(time.perf_counter() - load_start):.1f} 秒，共 {total} 单")
    if password is None:
        password = ""
    dry_run = bool(getattr(config, "sss_dry_run", False))
    store_name = str(getattr(config, "sss_store_name", "") or "一口轻食")
    common_address = str(getattr(config, "sss_common_address", "") or "")
    use_fixed_address = bool(getattr(config, "sss_use_fixed_address", False))
    goods_name = str(getattr(config, "sss_product_name", "") or "轻食")
    try:
        max_workers = int(getattr(config, "sss_max_workers", 4))
    except (TypeError, ValueError):
        max_workers = 4
    max_workers = max(1, min(20, max_workers))
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
                       unconfirmed=len(tasks), next_action="干跑未发送任何 POST；确认报文后再正式运行")

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
                           "请等待锁释放后重试，严禁并行重跑本批")
        # 持锁后先把旧/镜像 journal 的 unresolved 合并进权威共享状态，再做
        # 只读对账；迁移或镜像失败一律 fail-closed。
        #
        # R8-S1：历史 URL 写法（尾点/IDN 等）会把未决记录拆到另一个 authority
        # scope。持锁后、迁移/对账/POST 之前，先按规范化账号只读扫描本机权威根；
        # 发现**无法安全归属**给当前账号的活跃 unresolved（旧写法不再受支持、
        # 平台/账号字段缺失）一律保守阻断；不同规范 origin 的平台不误伤。
        # 该扫描只读：不迁移、不改写、不删除旧记录，也不依据 DNS 等价合并身份。
        try:
            cross_scope = cross_scope_unresolved_records(
                authoritative_journal, config=config, account=account,
                origin=origin)
        except UncertainJournalError as exc:
            _emit(progress_callback, f"跨作用域未决记录只读核对失败：{exc}")
            stop_event.set()
            return _finish({
                "processed": len(tasks), "created": 0, "submitted": 0,
                "previewed": 0, "stopped": True, "partial": False,
                "reconciled": False, "uncertain": True,
                "semantics": "cross-scope-authority-guard",
                "authoritative_journal": str(authoritative_journal),
            }, status="failed", unconfirmed=len(tasks), stopped=True,
               failed=len(tasks),
               next_action=f"{exc}；请人工只读核对后再运行，不要删除权威文件，"
                           "也不要并行重跑")
        if cross_scope:
            detail = describe_cross_scope_conflicts(cross_scope)
            _emit(progress_callback,
                  f"发现 {len(cross_scope)} 条属于当前账号但无法安全归属的活跃未决记录："
                  f"{detail}；本批拒绝任何 POST")
            stop_event.set()
            return _finish({
                "processed": len(tasks), "created": 0, "submitted": 0,
                "previewed": 0, "stopped": True, "partial": False,
                "reconciled": False, "uncertain": True,
                "semantics": "cross-scope-authority-guard",
                "authoritative_journal": str(authoritative_journal),
                "cross_scope_records": len(cross_scope),
                "cross_scope_journals": sorted(
                    {str(item.get("journal") or "") for item in cross_scope}),
            }, status="blocked_uncertain", unconfirmed=len(tasks), stopped=True,
               uncertain_count=len(cross_scope),
               cross_scope_records=len(cross_scope),
               next_action="换 URL 写法会让未决记录落在另一个 authority scope，"
                           "无法安全判定它们是否属于同一平台同一账号；请先只读核对站内"
                           "订单，并按上述文件人工整理这些记录（不要删除或改写权威文件，"
                           "也不要并行重跑）。影响范围：本机同一账号在已不受支持的旧网址"
                           "写法（尾点、中文域名等）或归属信息缺失时，本批都会被阻断，"
                           "直到人工核对完成；其他账号与不同规范 origin 的平台不受影响")
        # R8-S3：显式权威位置（config.sss_authoritative_uncertain_path /
        # YIKOU_SSS_AUTHORITATIVE_PATH）启用、切换或取消时，旧位置的活跃
        # unresolved 会变得不可见。这里在锁内、迁移/对账/POST 之前做一次
        # 「位置切换闸门」：核对其他已知位置（默认权威根 + 部署级登记位置）里
        # 属于当前账号的活跃未决记录；有就阻断，全部干净后先把当前位置登记
        # 落盘（POST 之前），再继续既有流程。该闸门只读 journal、只写登记文件：
        # 不迁移、不改写、不删除任何旧记录。
        try:
            location_gate = authority_location_gate(
                authoritative_journal, config=config, account=account,
                origin=origin)
        except UncertainJournalError as exc:
            _emit(progress_callback, f"权威位置登记/切换闸门失败：{exc}")
            stop_event.set()
            return _finish({
                "processed": len(tasks), "created": 0, "submitted": 0,
                "previewed": 0, "stopped": True, "partial": False,
                "reconciled": False, "uncertain": True,
                "semantics": "authority-location-guard",
                "authoritative_journal": str(authoritative_journal),
            }, status="failed", unconfirmed=len(tasks), stopped=True,
               failed=len(tasks),
               next_action=f"{exc}；请人工核对权威位置登记文件与各权威位置后再运行，"
                           "不要删除权威记录文件，也不要并行重跑")
        if location_gate["pruned"]:
            _emit(progress_callback,
                  "权威位置登记：已移除不存在的旧位置 "
                  + "、".join(location_gate["pruned"]))
        if location_gate["conflicts"]:
            detail = describe_authority_location_conflicts(
                location_gate["conflicts"])
            _emit(progress_callback,
                  f"权威位置切换闸门：其他权威位置仍有当前账号的 "
                  f"{len(location_gate['conflicts'])} 条活跃未决记录：{detail}；"
                  "本批拒绝任何 POST")
            stop_event.set()
            return _finish({
                "processed": len(tasks), "created": 0, "submitted": 0,
                "previewed": 0, "stopped": True, "partial": False,
                "reconciled": False, "uncertain": True,
                "semantics": "authority-location-guard",
                "authoritative_journal": str(authoritative_journal),
                "authority_locations_registry": location_gate["registry"],
                "location_conflicts": len(location_gate["conflicts"]),
                "location_conflict_locations": sorted(
                    {str(item.get("location") or "")
                     for item in location_gate["conflicts"]}),
            }, status="blocked_uncertain", unconfirmed=len(tasks), stopped=True,
               uncertain_count=len(location_gate["conflicts"]),
               location_conflicts=len(location_gate["conflicts"]),
               next_action="检测到另一个权威状态位置仍有当前账号的活跃未确认记录"
                           "（例如启用/切换/取消显式权威路径后，旧位置不再被读取）。"
                           "为避免重复下单，本次不会提交任何 POST。请先只读核对这些"
                           "位置的站内订单与本地记录，确认后人工整理对应记录；"
                           "不要删除权威记录文件，也不要并行重跑。影响范围：同一账号在"
                           "任一已登记或默认权威位置存在活跃未决记录时，本批都会被阻断"
                           f"（本次检查的位置：{'、'.join(location_gate['checked']) or '无'}）；"
                           "其他账号不受影响。")
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
            pending_journal, journal_recon, journal_resolved = resolve_pending_records(
                authoritative_journal, journal_key, client.get_json, progress_callback)
            _sync_journal_mirrors()
        except UncertainJournalError as exc:
            _emit(progress_callback,
                  f"权威/旧 journal 迁移或读取失败：{exc}；已停止，不会提交任何 POST")
            stop_event.set()
            return _finish({
                "processed": len(tasks), "created": 0, "submitted": 0,
                "previewed": 0, "stopped": True, "partial": False,
                "reconciled": False, "uncertain": True,
                "semantics": "authoritative-shared-journal",
                "authoritative_journal": str(authoritative_journal),
            }, status="failed", unconfirmed=len(tasks), stopped=True,
               failed=len(tasks),
               next_action=f"{exc}；请人工核对旧 journal 与权威 journal "
                           f"{authoritative_journal} 后再运行；严禁直接删除或并行重跑")
        if journal_resolved:
            _emit(progress_callback,
                  f"跨运行不确定记录：只读对账已确认并清理 {journal_resolved} 条")
        if pending_journal:
            _emit(progress_callback,
                  f"发现 {len(pending_journal)} 条未解决的“已发送未知”记录，"
                  "只读对账仍未确认：本批拒绝任何 POST，下一步仅可人工核对站内订单")
            stop_event.set()
            return _finish({
                "processed": len(tasks), "created": 0, "submitted": 0,
                "previewed": 0, "stopped": True, "partial": False,
                "reconciled": journal_recon is not None, "uncertain": True,
                "semantics": "uncertain-journal-guard",
                "authoritative_journal": str(authoritative_journal),
                "uncertain_records": len(pending_journal),
            }, status="blocked_uncertain", unconfirmed=len(tasks), stopped=True,
               uncertain_count=len(pending_journal),
               next_action="先只读核对站内订单与本地不确定记录；未确认前不要重跑或补发")

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
               next_action=next_action)

        if bool(getattr(config, "sss_preflight", False)):
            preflight_status, preflight = _preflight_tasks(
                tasks, client.get_json, progress_callback)
            preflight_confirmed = len(preflight.confirmed) if preflight else 0
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
                            else "预检未通过或对账不确定，未提交任何 POST；先处理日志风险"))

        _emit(progress_callback,
              f"开始下单：共 {len(tasks)} 单，并发 {max_workers} 路，读取超时 {read_timeout_s:g}s")
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
            uncertain_sink=_journal_sink,
            uncertain_clear=_journal_clear,
            uncertain_discard=_journal_discard,
            journal_meta=journal_meta,
            outcome=outcome,
        )
        created = len(final.confirmed) if final is not None else 0
        processed = len(tasks)
        _emit(progress_callback,
              f"下单与对账耗时 {(time.perf_counter() - submit_start):.1f} 秒，"
              f"确认 {created}/{processed}")
        try:
            end_total, end_frozen = query_balance(client.get_json)
        except _AuthExpired:
            _emit(progress_callback, "余额查询时登录态失效，正在重新登录…")
            relogin()
            try:
                end_total, end_frozen = query_balance(client.get_json)
            except _AuthExpired as exc:
                _emit(progress_callback, f"重新登录后余额查询仍失败：{exc}", "WARN")
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
            next_action = (f"{journal_error}；先人工只读核对站内订单和本地记录，"
                           "严禁重跑本批")
        elif reconcile_failed or final is None:
            status = "failed"
            next_action = ("站内对账失败，无法确认创建数量；请仅做只读核对，"
                           "严禁重跑或补发本批")
        elif stopped:
            status = "stopped"
            next_action = ("任务已停止；未确认单只做只读核对，禁止重试或重跑")
        elif uncertain_ids or (final is not None and final.missing):
            status = "unconfirmed"
            next_action = ("存在未确认或“已发送未知”的订单；下一步仅做只读核对，"
                           "禁止人工重试或重跑本批")
        else:
            status = "confirmed"
            next_action = "已全部对账确认，无需重复提交"
        failed_count = len(failure_ids) + (1 if journal_error else 0)

        if status == "failed":
            _emit(progress_callback,
                  f"闪时送任务失败：确认 {created}/{processed}，"
                  f"{next_action}")
        elif status == "stopped":
            _emit(progress_callback,
                  f"闪时送任务已停止：{'已完成站内对账' if reconciled else '站内对账失败'}，"
                  f"确认 {created}/{processed}")
        elif status == "confirmed":
            _emit(progress_callback,
                  f"闪时送下单完成：确认 {created}/{processed}，"
                  f"总耗时 {(time.perf_counter() - job_start):.1f} 秒")
        else:
            _emit(progress_callback,
                  f"闪时送任务未确认：确认 {created}/{processed}，"
                  f"{processed - created} 单未确认；{next_action}")

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
        )
    finally:
        if _batch_guard is not None:
            _batch_guard.release()
        client.close()
