"""业务逻辑与网页前端之间的桥接层（HTTP + 事件推送）。

职责边界：本模块只做「应用 API / UI 协议」，业务规则全部留在领域模块
（``app.order`` / ``app.ordering`` / ``app.wps`` / ``app.core``）。
服务端不依赖任何桌面窗口框架，文件选择由 ``app.web`` 的文件浏览器完成。

事件协议（Python → JS，经 ``drain_events(last_sequence, ack_sequence)``）：
每条事件都是 ``{event, payload, event_id, sequence, created_at, timestamp, droppable}``；
前端只在成功应用后推进 cursor，ACK 会推动已确认事件删除；未 ACK 的仍可重放，
sequence 中间缺口和关键事件超限都会返回 ``events:dropped`` 告警。
- log                {ts, level, msg}          结构化日志行
- status             {state}                   ready/running/stopping/success/partial/stopped/error/updating
- task:done          {message, stopped, partial}
- task:error         {message}
- update:available         {tag, current, body, html_url, can_install, asset_name, size}
                            App 自己的 Release；APK 模式下带安装包信息
- update:latest            {manual, current}
- update:progress          {phase, percent, downloaded, total, message}
- update:permission_required {message}                      需要允许“安装未知应用”
- update:cancelled         {message}
- update:error             {code, message}
- decision           {id, kind, title, message, choices}
- captcha            {id, image}
- address_input      {id, title, message, items[{raw_address, order_numbers,
                     campus, reason, suggested_point}]}
- events:dropped     {dropped_count, first_available_sequence, message}
"""
from __future__ import annotations

import contextvars
import copy
import datetime as _dt
import json
import os
import re
import secrets
import threading
import time
import logging
import webbrowser
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path as _Path
from typing import Any

from app import __version__
from app.order.runner import parse_target_date, run_job
from app.core.config import AppConfig, clamp_split_ratio, default_wps_address_order
from app.integrations.sss_url import SssUrlConfigError, canonical_sss_origin
from app.core.credentials import (delete_password, delete_sss_password, get_password,
                          get_sss_password, set_password, set_sss_password)
from app.order.templates import write_order_template, write_sss_template
from app.ordering.sss import (
    _REVIEW_WIDE_WINDOW_DAYS,
    _SSS_REVIEW_TTL_S,
    UncertainJournalError,
    authoritative_uncertain_path,
    batch_key,
    batch_submission_lock,
    discard_uncertain_records,
    expected_delivery_date,
    journal_fingerprint,
    mask_contact,
    pending_record_views,
    platform_origin,
    resolve_uncertain_records,
    run_sss_job,
    run_sss_review_job,
)
from app.ordering.cloud_import import ImportRefused, prepare_day_orders
from app.api.operation import OperationCoordinator
from app.api.preview import (PREVIEW_TTL_SECONDS, PreviewStore,
                             canonical_plan, fingerprint_payload,
                             plan_fingerprint, sha256_file)
from app.core.update import (
    ReleaseCheckError,
    UpdateCancelled,
    check_for_update,
    download_asset,
    parse_sha256,
    read_asset_text,
    select_android_apk,
    select_sha256_asset,
)
from app.wps.sync import (KdocsCli, SyncLedger, WpsCloudError, apply_plan,
                        build_plan, effective_tables, format_plan,
                        read_local_orders, summarize_plan, target_date_for,
                        recovery_status as _wps_recovery_status_contract,
                        resolve_pending_operation as _wps_resolve_pending_operation)
from app.wps import android_runtime

logger = logging.getLogger(__name__)

#: HTTP 请求线程的当前角色；None 表示回退到 Bridge 实例默认角色。
_REQUEST_IS_ADMIN: contextvars.ContextVar[bool | None] = contextvars.ContextVar(
    "yikou_bridge_request_is_admin", default=None)
#: HTTP 请求线程的当前用户名；用于 pending interaction 的归属过滤。
_REQUEST_IDENTITY: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "yikou_bridge_request_identity", default=None)

EXCEL_EXTS = {".xlsx", ".xlsm"}
# 文件对话框过滤器沿用 "描述 (*.a;*.b)" 写法（历史格式，保持配置兼容）。
FILE_DIALOG_FILTERS = ["Excel 工作簿 (*.xlsx)", "Excel 启用宏的工作簿 (*.xlsm)", "所有文件 (*)"]

MAX_ORDER_COUNT = 9999

# 事件重放窗口：仅限制可丢失日志的内存占用，关键事件不按固定容量淘汰。
EVENT_HISTORY_LIMIT = 2000
EVENT_DRAIN_LIMIT = 500
# ACK 即代表前端已成功应用并持久化 cursor；确认后即可删除，未 ACK 的仍可重放。
EVENT_ACK_RETAIN = 0
# 关键事件不参与普通淘汰，但必须有上限，否则前端长期不轮询会无限增长。
CRITICAL_EVENT_LIMIT = 500
DROPPABLE_EVENTS = frozenset({"log"})
# 交互请求（decision/captcha）等待上限；到点后 worker 必须能退出，而不是永久阻塞。
DEFAULT_INTERACTION_TIMEOUT_S = 300.0

# 「核对副本」的并发度：每张子表要读 2 张云表，6 张子表串行时最多 12 次往返。
# 并发只改变往返的重叠方式：**正常路径（含全部 5 种 status 与 WpsCloudError）
# 的调用次数与参数完全不变**，因此不额外消耗云端每日额度；只有出现
# ``WpsCloudError`` 之外的意外异常时，最坏会多发 workers-1 次调用（见下方分批
# 提交的注释），串行版本则是 0 次。
#
# 为什么是 2 而不是 4：金山接口的 429002 表示「短时间频繁触发」，而
# ``_TRANSIENT_HINTS`` 并不包含限流提示，`_run` 不会重试它 —— 一旦突发触发，
# 该表就会从「已核对」变成「读不了」，属于可观测的行为偏差。取 2 只把瞬时速率
# 翻倍（串行约 5 次/秒 → 并发约 10 次/秒），在明显提速与不制造突发之间取平衡；
# 这也是项目里最保守的既有取值（app/ordering/sss.py 的验证码/登录轮询同样用 2）。
WPS_COPY_CHECK_WORKERS = 2

RETRY_CHOICES = [{"value": "retry", "label": "重试", "style": "primary"},
                 {"value": "skip", "label": "跳过", "style": "neutral"},
                 {"value": "stop", "label": "停止", "style": "danger"}]

_MODE_LABELS = {
    "order": "订单处理任务",
    "sss": "闪时送下单任务",
    "wps_upload": "云文档上传",
    "wps_authorize": "WPS 授权",
    "wps_logout": "WPS 退出授权",
    "check_update": "检查更新",
    "install_update": "安装更新",
    # W7：只读云入口也占同一个槽位，冲突提示要说清是谁在跑。
    "wps_preview": "云文档预览",
    "wps_check_copies": "云文档副本核对",
    "sss_day_orders": "云端当天名单读取",
    "wps_recovery_resolve": "旧任务恢复/退场",
}


class _InteractionCancelled(RuntimeError):
    """交互请求被取消/超时，worker 必须按取消路径退出。"""


@dataclass
class _PendingInteraction:
    event: threading.Event
    holder: list[Any] = field(default_factory=list)
    kind: str = ""
    created_at: float = 0.0
    expires_at: float = 0.0
    operation_id: str = ""
    owner: str = ""
    owner_is_admin: bool = False
    request: dict[str, Any] = field(default_factory=dict)


class Bridge:
    """js_api 对象。公开方法（无下划线）均可被前端 Promise 调用。"""

    def __init__(self, config_path: os.PathLike[str] | str | None = None, *,
                 is_admin: bool = False) -> None:
        """``is_admin`` 决定这个实例的权限级别。

        **默认 False（非管理员）是刻意的**：任何忘记显式指定的地方都退化为「受限」，
        而不是「全权」。网页版在每次请求时按会话账号重设（见 web_server），
        桌面版与测试则以管理员身份构造。
        """
        self._window: Any = None
        # config_path 供测试注入临时配置文件；生产环境沿用默认用户配置目录。
        self._config = AppConfig.load(config_path) if config_path else AppConfig.load()
        self._stop_event = threading.Event()
        self._worker: threading.Thread | None = None
        self._closing = False
        self._status = "ready"
        # 事件使用「保留 + cursor 重放」而不是「取出即删除」；producer_id 用于
        # 前端识别 Python 进程重启后 sequence 归零，避免误推进旧 cursor。
        self._event_log: deque[dict[str, Any]] = deque()
        self._event_seq = 0
        self._event_producer_id = secrets.token_hex(8)
        self._event_ack_sequence = 0
        self._event_dropped_count = 0
        self._critical_dropped_count = 0
        # 被淘汰事件的 sequence 区间 (start, end, critical_count)，用于精确告警。
        self._event_dropped_ranges: deque[tuple[int, int, int]] = deque()
        self._push_lock = threading.Lock()
        self._worker_lock = threading.Lock()
        self._update_checking = False
        #: APK 更新：同一时间只允许一个下载/安装任务。
        self._update_installing = False
        self._update_cancel = threading.Event()
        self._update_prev_status = "ready"
        self._update_phase = ""
        self._decision_seq = 0
        self._decisions: dict[str, _PendingInteraction] = {}
        self._interaction_timeout_s = DEFAULT_INTERACTION_TIMEOUT_S
        #: 当前任务归属：HTTP 会话身份 + 是否管理员；只用于交互只读过滤。
        self._task_owner = ""
        self._task_owner_is_admin = False
        #: 成功清除过的凭据，用于同一 Bridge 进程内重复 clear 的幂等判定。
        self._password_clear_lock = threading.Lock()
        self._cleared_password_accounts: set[tuple[str, str]] = set()
        #: 实例默认角色；每个 HTTP 请求的会话角色由 ContextVar 临时覆盖。
        self._is_admin = bool(is_admin)
        #: 统一互斥：订单 / 闪时送 / 云上传 / WPS 授权退出 / 更新。
        self._operations = OperationCoordinator()
        #: 一次性 WPS 上传预览令牌（只读哈希与指纹）。
        self._previews = PreviewStore(ttl_seconds=PREVIEW_TTL_SECONDS)
        #: 各后台 worker 对应的 operation_id；finally 据此释放互斥槽位。
        self._task_operation_id = ""
        #: 任务完成事件幂等：同一 worker 生命周期只接受第一次 _finish_task/_task_error。
        self._task_finish_lock = threading.Lock()
        self._task_finished = False
        #: 只读核对证据快照（仅内存）：管理员据“站内查不到”解除阻断前必须先有
        #: 一次新鲜核对，快照就是那条证据；进程重启即失效，必须重新核对。
        self._sss_review_snapshot: dict[str, Any] = {}
        self._sss_review_lock = threading.Lock()
        self._authorize_operation_id = ""
        self._update_check_operation_id = ""
        self._update_install_operation_id = ""

    # -- 请求局部权限上下文 --------------------------------------------
    @property
    def is_admin(self) -> bool:
        """当前请求的角色。

        网页版是共享 Bridge：请求线程调用 ``set_request_is_admin`` 写入
        ContextVar，读到这里优先取请求局部值；没有请求上下文（单元测试、
        桌面直调、后台 worker）时回退实例构造时的 ``_is_admin``。
        这避免了“A 请求把共享实例改成管理员、B 请求又改回普通用户”的
        串权限窗口，同时保留 ``Bridge(is_admin=True)`` 的直接可用性。
        """
        request_value = _REQUEST_IS_ADMIN.get()
        if request_value is None:
            return self._is_admin
        return bool(request_value)

    @is_admin.setter
    def is_admin(self, value: bool) -> None:
        self._is_admin = bool(value)

    def set_request_is_admin(self, value: bool | None) -> None:
        """网页版每个请求调用一次；``None`` 表示清回实例默认角色。"""
        _REQUEST_IS_ADMIN.set(None if value is None else bool(value))

    def set_request_identity(self, username: str | None) -> None:
        """网页版每个请求写入当前登录用户名；``None`` 表示清空。"""
        _REQUEST_IDENTITY.set((str(username).strip() or None)
                              if username is not None else None)

    def _request_identity(self) -> str:
        return str(_REQUEST_IDENTITY.get() or "").strip()

    def _clear_request_is_admin(self) -> None:
        _REQUEST_IS_ADMIN.set(None)
        _REQUEST_IDENTITY.set(None)

    # ------------------------------------------------------------------
    # 事件通道：保留窗口 + 前端 cursor 拉取
    #
    # 不用 evaluate_js 推送——它在 WebKitGTK 上并发调用会静默丢结果
    # （症状：日志行成对丢失）。Python 只追加事件、保留重放窗口，前端用
    # ``last_sequence`` 拉取并在成功应用后推进 cursor；事件不会“取出即删”。
    # 关键事件（decision/captcha/task:*/update:*）不参与固定容量淘汰，只有
    # 普通日志/进度会被丢弃，并通过 ``events:dropped`` 明确告警。
    # ------------------------------------------------------------------
    def attach(self, window: Any) -> None:
        """挂接服务端运行时的窗口适配层（``destroy`` 用于停止服务）。

        网页版这里收到的是 :class:`app.web.server._WebWindow`，不是原生窗口。
        """
        self._window = window

    @staticmethod
    def _is_droppable_event(event: str) -> bool:
        return event in DROPPABLE_EVENTS

    def _record_dropped_locked(self, sequence: int, *, critical: bool = False) -> None:
        critical_count = 1 if critical else 0
        entries = list(self._event_dropped_ranges)
        entries.append((sequence, sequence, critical_count))
        entries.sort(key=lambda item: item[0])
        merged: list[tuple[int, int, int]] = []
        for start, end, critical_in_range in entries:
            if merged and start <= merged[-1][1] + 1:
                prev_start, prev_end, prev_critical = merged[-1]
                merged[-1] = (prev_start, max(prev_end, end), prev_critical + critical_in_range)
            else:
                merged.append((start, end, critical_in_range))
        self._event_dropped_ranges = deque(merged)
        self._event_dropped_count += 1
        if critical:
            self._critical_dropped_count += 1

    def _prune_event_log_locked(self) -> None:
        """先按 ACK 清理，再限制总量，最后对关键事件做显式上限告警。"""
        # 1) ACK 推动删除；只保留少量已确认事件，保证页面刷新/重连仍可重放最近一段。
        while (len(self._event_log) > EVENT_ACK_RETAIN
               and int(self._event_log[0].get("sequence") or 0) <= self._event_ack_sequence):
            self._event_log.popleft()
        while self._event_dropped_ranges and self._event_dropped_ranges[0][1] <= self._event_ack_sequence:
            self._event_dropped_ranges.popleft()

        # 2) 总量超限时优先淘汰普通日志/进度；关键事件保留。
        while len(self._event_log) > EVENT_HISTORY_LIMIT:
            dropped = False
            for index, item in enumerate(self._event_log):
                if item.get("droppable"):
                    sequence = int(item.get("sequence") or 0)
                    del self._event_log[index]
                    self._record_dropped_locked(sequence, critical=False)
                    dropped = True
                    break
            if not dropped:
                break

        # 3) 关键事件上限：超过时淘汰最旧的关键事件，并记录 critical 丢失区间。
        while True:
            critical_items = [item for item in self._event_log if not item.get("droppable")]
            if len(critical_items) <= CRITICAL_EVENT_LIMIT:
                break
            oldest = critical_items[0]
            sequence = int(oldest.get("sequence") or 0)
            try:
                self._event_log.remove(oldest)
            except ValueError:  # pragma: no cover - 单线程锁内不应发生
                break
            self._record_dropped_locked(sequence, critical=True)

    def _emit_event(self, event: str, payload: Any = None) -> None:
        with self._push_lock:
            self._event_seq += 1
            sequence = self._event_seq
            now = time.time()
            envelope = {
                "event": event,
                # event_type 与 event 同义，兼容审计报告建议的事件协议字段名。
                "event_type": event,
                "payload": payload,
                "event_id": f"{self._event_producer_id}:{sequence}",
                "sequence": sequence,
                "created_at": now,
                # timestamp 与 created_at 同义，兼容审计报告建议字段名。
                "timestamp": now,
                "droppable": self._is_droppable_event(event),
            }
            self._event_log.append(envelope)
            self._prune_event_log_locked()

    def _dropped_notices_locked(self, cursor: int) -> list[dict[str, Any]]:
        """把丢失区间转换为明确的事件；sequence 取区间末尾，避免同一缺口重复告警。"""
        notices: list[dict[str, Any]] = []
        for start, end, critical_count in list(self._event_dropped_ranges):
            effective_start = max(start, cursor + 1)
            if effective_start > end:
                continue
            dropped_count = end - effective_start + 1
            critical_dropped = min(critical_count, dropped_count)
            now = time.time()
            notices.append({
                "event": "events:dropped",
                "event_type": "events:dropped",
                "payload": {
                    "dropped_count": dropped_count,
                    "critical_dropped_count": critical_dropped,
                    "first_sequence": effective_start,
                    "last_sequence": end,
                    "total_dropped": self._event_dropped_count,
                    "total_critical_dropped": self._critical_dropped_count,
                    "message": (
                        f"事件队列繁忙，已丢弃 sequence {effective_start}-{end} 的 "
                        f"{dropped_count} 条事件"
                        + (f"（其中关键事件 {critical_dropped} 条）" if critical_dropped else "")
                    ),
                },
                # 取区间末尾：前端应用后 cursor 直接越过整个缺口。
                "sequence": end,
                "event_id": f"{self._event_producer_id}:dropped:{start}-{end}",
                "created_at": now,
                "timestamp": now,
                "droppable": False,
                "synthetic": True,
            })
        return notices

    def drain_events(self, last_sequence: int = 0, ack_sequence: int | None = None,
                     producer_id: str = "") -> dict[str, Any]:
        """按 cursor 拉取事件；前端应用成功后用 ``ack_sequence`` 确认。

        ACK 会推动已确认事件删除（保留少量尾部用于重连重放）；sequence
        中间的缺口也会生成明确的 ``events:dropped`` 告警，而不是只跳号。
        """
        try:
            cursor = int(last_sequence or 0)
        except (TypeError, ValueError):
            cursor = 0
        with self._push_lock:
            # 前端可能仍持有上一进程的 producer_id/ACK；只接受当前生产者的 ACK，
            # 且 ACK 不得超过当前已分配的最大 sequence，避免误删新进程事件。
            ack_producer_ok = not producer_id or str(producer_id) == self._event_producer_id
            if ack_sequence is not None and ack_producer_ok:
                try:
                    requested_ack = int(ack_sequence)
                    if 0 <= requested_ack <= self._event_seq:
                        self._event_ack_sequence = max(self._event_ack_sequence, requested_ack)
                except (TypeError, ValueError):
                    pass
            self._prune_event_log_locked()
            available = [item for item in self._event_log
                         if int(item.get("sequence") or 0) > cursor]
            notices = self._dropped_notices_locked(cursor)
            combined = available + notices
            combined.sort(key=lambda item: int(item.get("sequence") or 0))
            events = combined[:EVENT_DRAIN_LIMIT]
            return {
                "events": events,
                "producer_id": self._event_producer_id,
                "latest_sequence": self._event_seq,
                "acked_sequence": self._event_ack_sequence,
                "dropped_count": self._event_dropped_count,
                "critical_dropped_count": self._critical_dropped_count,
                "first_available_sequence": int(self._event_log[0].get("sequence") or 0) if self._event_log else self._event_seq + 1,
            }

    def log(self, message: str, level: str = "INFO") -> None:
        """向前端推一条日志行（``event="log"``，含 ``ts``/``level``/``msg``）。"""
        self._emit_event("log", {"ts": time.strftime("%H:%M:%S"), "level": level, "msg": message.rstrip()})

    def _set_status(self, state: str) -> None:
        self._status = state
        self._emit_event("status", {"state": state})

    @property
    def status(self) -> str:
        """当前任务状态（``ready``/``running``/``stopping``/``success``/``partial``/``stopped``/``error``/``updating``）。"""
        return self._status

    #: 非管理员不可见（但任务仍按这些值运行）的配置字段。
    #: 判据是「泄漏后能被用来做什么」：管理网址 + 手机号可用来接管下单目标、
    #: Excel 路径暴露服务器目录结构、WPS 表格 ID 可直接改云端文档。
    _ADMIN_ONLY_CONFIG = (
        "target_url", "phone_number", "excel_path",
        "sss_url", "sss_account", "sss_excel_path",
        "wps_cli_path", "wps_drive_id", "wps_tables", "wps_test_tables",
        "wps_test_file_id", "wps_test_drive_id",
    )

    def _visible_config(self, fields: dict[str, Any]) -> dict[str, Any]:
        """按角色过滤配置字段。

        非管理员这些字段**整体清空**（而不是保留真值）：界面隐藏挡不住任何人 ——
        接口是公开可调的，真值一旦发出去就等于公开。
        """
        if self.is_admin:
            return fields
        for key in self._ADMIN_ONLY_CONFIG:
            if key in fields:
                current = fields[key]
                fields[key] = type(current)() if isinstance(current, (dict, list)) else ""
        return fields

    def _forced_order_payload(self, payload: dict[str, Any]) -> dict[str, Any]:
        """非管理员：下单目标一律取服务端预设，客户端传什么都不作数。

        为什么必须在后端做：否则普通用户只要把 url/phone 指向自己的管理后台，
        再点「开始处理」，就能让这台手机登录**别人的**平台并按其数据下单 ——
        参数被隐藏毫无意义。

        口令同样取自**服务端已保存的那份**（非管理员看不到密码输入框，
        让他填既没意义也拦不住）——否则会卡在校验「请输入登录密码」而无法下单。
        密钥环里没存过口令时仍会如实回报该错误，由管理员去「订单处理」里补一次，
        这与原本的行为一致。``remember`` 强制 False：非管理员不该借下单
        往密钥环写入或替换平台口令。
        """
        forced = dict(payload)
        config = self._config
        forced["url"] = config.target_url
        forced["phone"] = config.phone_number
        forced["excel"] = str(config.excel_path) if config.excel_path else ""
        forced["password"] = (get_password(config.phone_number) or "") if config.phone_number else ""
        forced["remember"] = False
        return forced

    def _forced_sss_payload(self, payload: dict[str, Any]) -> dict[str, Any]:
        """非管理员：闪时送的账号/地址/Excel 同样强制取预设。

        ``dry_run`` / ``preflight`` / ``product_name`` 保留 —— 它们是「怎么跑」，
        不是「拿谁的账号、跑到哪个地址去」，属于受权范围内的操作选择。
        """
        forced = dict(payload)
        config = self._config
        forced["url"] = config.sss_url
        forced["account"] = config.sss_account
        forced["excel"] = str(config.sss_excel_path) if config.sss_excel_path else ""
        forced["password"] = (get_sss_password(config.sss_account) or "") if config.sss_account else ""
        forced["remember"] = False
        for key in ("use_fixed_address", "fixed_lnt", "fixed_lat",
                    "fixed_area_code", "fixed_address_detail", "common_address"):
            forced.pop(key, None)
        return forced

    # ------------------------------------------------------------------
    # 操作互斥与断线可查状态
    # ------------------------------------------------------------------
    def operation_status(self, operation_id: str = "") -> dict[str, Any]:
        """查询当前/最近操作；不依赖前端是否收到过事件。

        查询只短暂持有协调器内部锁，不等待云端/worker，因此浏览器断线后重连、
        或上传过程中调用都能立即返回。
        """
        return self._operations.status(operation_id)

    def _operation_conflict_payload(self, conflict: Any = None, *,
                                    action: str = "") -> dict[str, Any]:
        """把互斥冲突转成统一结果；保留旧 ``reason="busy"`` 字段。"""
        if hasattr(conflict, "as_dict"):
            status = conflict.as_dict(active=True)
        elif isinstance(conflict, dict):
            status = dict(conflict)
        else:
            status = self.operation_status()
        if not status or not status.get("active"):
            status = self.operation_status()
        mode = str(status.get("mode") or "")
        label = _MODE_LABELS.get(mode, mode or "其他操作")
        operation_id = str(status.get("operation_id") or "")
        message = (f"已有{label}进行中，已拒绝{action or '本次操作'}"
                   f"（operation_id={operation_id or 'unknown'}）")
        return {
            "ok": False,
            "status": "rejected",
            "reason": "busy",
            "code": "operation_conflict",
            "next_action": str(status.get("next_action")
                               or f"等待{label}结束后重试"),
            "summary": {"conflicting_operation": status},
            "operation_id": operation_id,
            "message": message,
        }

    def _reject_if_operation_active(self, action: str) -> dict[str, Any] | None:
        """所有会改配置/模板的入口共用；只读查询和 resolve_* 不走这里。"""
        status = self.operation_status()
        if status.get("active"):
            return self._operation_conflict_payload(status, action=action)
        return None

    def _busy_result_from_worker(self, message: str = "") -> dict[str, Any]:
        """旧测试/外部直接设置 ``_worker`` 时的安全拒绝。

        若协调器里确实有活动操作，返回带 operation_id 的统一冲突结果；
        只有老测试那种“伪造 _worker 但无操作记录”才退回纯 legacy 形状。
        """
        status = self.operation_status()
        if status.get("active"):
            payload = self._operation_conflict_payload(status, action="启动任务")
            if message:
                payload["message"] = message
            return payload
        return {
            "ok": False,
            "status": "rejected",
            "reason": "busy",
            "code": "worker_busy",
            "next_action": "等待当前任务结束后重试",
            "summary": {},
            "message": message or "已有任务正在运行，请先停止后再启动",
            "fields": {},
        }

    def bridge_ready(self) -> dict[str, Any]:
        """前端装载完成后的握手。返回初始状态并冲积未发送事件。"""
        config = self._config
        android_mode = android_runtime.is_android()
        state: dict[str, Any] = {
            "version": __version__,
            "status": self._status,
            # 前端据此识别 Python 进程重启，避免旧 cursor 与新的 sequence 冲突。
            "event_producer_id": self._event_producer_id,
            #: App 运行平台：android = APK 自带 WebView；web = 纯浏览器访问。
            "platform": "android" if android_mode else "web",
            #: 是否支持应用内下载安装（当前只有 APK 模式支持）。
            "can_self_update": android_mode,
            #: 前端据此决定显示哪些表单字段。真正的拦截在后端（见 _ADMIN_ONLY_CONFIG
            #: 与 web_server 的方法白名单），前端只是不显示。
            "is_admin": bool(self.is_admin),
            "config": {
                "target_url": config.target_url,
                "phone_number": config.phone_number,
                "excel_path": str(config.excel_path) if config.excel_path else "",
                "order_date": config.order_date,
                "order_count": config.order_count,
                "split_ratio": config.split_ratio,
                "sss_url": config.sss_url,
                "sss_account": config.sss_account,
                "sss_excel_path": str(config.sss_excel_path) if config.sss_excel_path else "",
                "sss_order_source": config.sss_order_source,
                "sss_product_name": config.sss_product_name,
                "sss_common_address": config.sss_common_address,
                "sss_use_fixed_address": config.sss_use_fixed_address,
                "sss_fixed_lnt": config.sss_fixed_lnt,
                "sss_fixed_lat": config.sss_fixed_lat,
                "sss_fixed_area_code": config.sss_fixed_area_code,
                "sss_fixed_address_detail": config.sss_fixed_address_detail,
                "sss_dry_run": config.sss_dry_run,
                "sss_preflight": config.sss_preflight,
                "sss_idempotency_field": config.sss_idempotency_field,
                "wps_enabled": config.wps_enabled,
                "wps_test_mode": config.wps_test_mode,
                "wps_test_file_id": config.wps_test_file_id,
                "wps_test_drive_id": config.wps_test_drive_id,
                "wps_test_tables": dict(config.wps_test_tables),
                "wps_drive_id": config.wps_drive_id,
                "wps_cli_path": config.wps_cli_path,
                "wps_tables": dict(config.wps_tables),
                "wps_target_hour_start": config.wps_target_hour_start,
                "wps_target_hour_end": config.wps_target_hour_end,
                "wps_marker_enabled": config.wps_marker_enabled,
            },
            # 密码只回给管理员：这是平台登录口令，拿到即可冒充管理员操作。
            # 非管理员仍能下单 —— 服务端在 start_order/start_sss 里自己从密钥环取，
            # 所以清空它不影响授权范围内的功能。
            "passwords": {
                "order": (get_password(config.phone_number) if config.phone_number else "")
                if self.is_admin else "",
                "sss": (get_sss_password(config.sss_account) if config.sss_account else "")
                if self.is_admin else "",
            },
        }
        try:
            operation = self.operation_status()
        except Exception as exc:  # noqa: BLE001 - 握手不能因状态查询失败而挂掉
            logger.exception("bridge_ready operation_status failed")
            operation = {
                "ok": False, "active": False, "operation_id": "",
                "mode": "", "status": "error", "phase": "",
                "reason": str(exc), "next_action": "刷新页面后重试",
                "summary": {}, "started_at": None, "finished_at": None,
                "operations": [],
            }
        state["operation"] = dict(operation)
        state["operations"] = list(operation.get("operations") or [])
        state["config"] = self._visible_config(state["config"])
        return state

    # ------------------------------------------------------------------
    # js_api：任务启动/停止（校验逻辑移植自旧 _validate_form/_validate_sss_form）
    # ------------------------------------------------------------------
    def start_order(self, payload: dict[str, Any]) -> dict[str, Any]:
        """校验订单表单并启动「订单处理」任务。

        统一互斥：与闪时送/云上传/WPS 授权退出/更新共用同一个槽位；运行线程拿到
        **启动时的配置深拷贝快照**，后续配置写入不会影响在跑任务。原子占位后无论
        校验失败、启动失败还是异常都会释放；正常运行时由 ``_finish_task``/异常路径释放。
        """
        if self.worker_alive():
            return self._busy_result_from_worker()
        reservation = self._operations.try_reserve(
            "order", summary={"title": "订单处理任务"},
            next_action="等待订单处理结束或查询 operation_status")
        if not reservation.granted:
            return self._operation_conflict_payload(reservation.conflict,
                                                    action="启动订单处理")
        operation = reservation.operation
        assert operation is not None
        try:
            if not self.is_admin:
                payload = self._forced_order_payload(payload)
            fields: dict[str, dict[str, str]] = {}
            url = str(payload.get("url", "")).strip()
            phone = str(payload.get("phone", "")).strip()
            password = str(payload.get("password", ""))
            excel = str(payload.get("excel", "")).strip()
            date_text = str(payload.get("date", "")).strip()
            count_text = str(payload.get("count", "")).strip()
            count: int | None = None
            if count_text:
                try:
                    count = int(count_text)
                except (TypeError, ValueError):
                    count = 0
                if not 1 <= count <= MAX_ORDER_COUNT:
                    fields["count"] = {
                        "message": f"请输入 1～{MAX_ORDER_COUNT} 的整数，或留空处理全部"}
            if not url:
                fields["url"] = {"message": "请输入管理网址"}
            if not phone:
                fields["phone"] = {"message": "请输入手机号或账号"}
            if not password:
                fields["password"] = {"message": "请输入登录密码"}
            excel_error = _excel_field_error(excel)
            if excel_error:
                fields["excel"] = {"message": excel_error}
            try:
                parse_target_date(date_text)
            except ValueError as exc:
                fields["date"] = {"message": str(exc)}
            if fields:
                self._set_status("error")
                self._operations.finish(
                    operation, status="rejected", reason="validation_failed",
                    summary={"fields": fields},
                    next_action="修正表单后重试")
                return {
                    "ok": False,
                    "status": "rejected",
                    "reason": "validation_failed",
                    "next_action": "修正表单后重试",
                    "summary": {"fields": fields},
                    "operation_id": operation.operation_id,
                    "fields": fields,
                }

            # 校验通过才写配置；任务线程始终拿深拷贝快照，避免运行中被防抖保存改写。
            _apply_order_payload(self._config, {
                "url": url, "phone": phone, "excel": excel,
                "date": date_text, "count": count,
            })
            self._config.save()
            if payload.get("remember", True):
                set_password(phone, password)
                with self._password_clear_lock:
                    self._cleared_password_accounts.discard(("order", phone))
            snapshot = copy.deepcopy(self._config)
            self._task_operation_id = operation.operation_id
            self._task_owner = self._request_identity()
            self._task_owner_is_admin = bool(self.is_admin)
            launched = self._launch("order", snapshot, count, password)
            if launched is False:
                if self._task_operation_id == operation.operation_id:
                    self._task_operation_id = ""
                    self._task_owner = ""
                    self._task_owner_is_admin = False
                self._operations.finish(
                    operation, status="rejected", reason="operation_conflict",
                    summary={"message": "启动时发现已有任务正在运行"},
                    next_action="等待当前操作结束后重试")
                return self._busy_result_from_worker("已有任务正在运行，请先停止后再启动")
            if not self.worker_alive() and self._operations.is_active(operation):
                # 测试替身或线程瞬时结束：不能让互斥槽位永久占住。
                self._operations.finish(
                    operation, status="success",
                    reason="worker_finished_before_status_check",
                    summary={"message": "订单处理任务已启动"},
                    next_action="")
                if self._task_operation_id == operation.operation_id:
                    self._task_operation_id = ""
                    self._task_owner = ""
                    self._task_owner_is_admin = False
            active = self._operations.is_active(operation)
            return {
                "ok": True,
                "status": "running" if active else "success",
                "reason": "",
                "next_action": ("使用 bridge_ready/operation_status 或事件跟踪进度"
                                if active else ""),
                "summary": {"message": "订单处理任务已启动"},
                "operation_id": operation.operation_id,
            }
        except Exception:
            if self._task_operation_id == operation.operation_id:
                self._task_operation_id = ""
                self._task_owner = ""
                self._task_owner_is_admin = False
            self._operations.finish_if_active(
                operation, status="error", reason="start_failed",
                next_action="查看日志后重试")
            raise

    def start_sss(self, payload: dict[str, Any]) -> dict[str, Any]:
        """校验闪时送表单并启动「闪时送下单」任务。

        与 :meth:`start_order` 同一套原子占位/快照/finally 释放规则。
        """
        if self.worker_alive():
            return self._busy_result_from_worker()
        reservation = self._operations.try_reserve(
            "sss", summary={"title": "闪时送下单任务"},
            next_action="等待闪时送任务结束或查询 operation_status")
        if not reservation.granted:
            return self._operation_conflict_payload(reservation.conflict,
                                                    action="启动闪时送下单")
        operation = reservation.operation
        assert operation is not None
        try:
            if not self.is_admin:
                payload = self._forced_sss_payload(payload)
            fields = {}
            url = str(payload.get("url", "")).strip()
            account = str(payload.get("account", "")).strip()
            password = str(payload.get("password", ""))
            excel = str(payload.get("excel", "")).strip()
            if not url:
                fields["url"] = {"message": "请输入闪时送网址"}
            else:
                # R8-S1：非规范/不支持的写法（尾点、中文域名、缺协议、非法端口等）
                # 在启动入口就拒绝，避免换写法后另建 authority scope 重复下单。
                try:
                    canonical_sss_origin(url)
                except SssUrlConfigError as exc:
                    fields["url"] = {"message": str(exc)}
            if not account:
                fields["account"] = {"message": "请输入闪时送账号"}
            if not password:
                fields["password"] = {"message": "请输入登录密码"}
            excel_error = _excel_field_error(excel)
            order_source = str(payload.get("order_source", self._config.sss_order_source) or "").strip().lower()
            order_source = "excel" if order_source == "excel" else "wps"
            # 云端模式下《闪时送.xlsx》只是留档目标：没选文件、文件不在也能下单，
            # 只在下单日志里提示“跳过留档”。本地 Excel 模式仍需严格校验。
            if order_source == "excel" and excel_error:
                fields["excel"] = {"message": excel_error}
            use_fixed_address = bool(payload.get("use_fixed_address", False))
            fixed_lnt = str(payload.get("fixed_lnt", "")).strip()
            fixed_lat = str(payload.get("fixed_lat", "")).strip()
            fixed_area_code = str(payload.get("fixed_area_code", "")).strip()
            fixed_address_detail = str(payload.get("fixed_address_detail", "")).strip()
            if use_fixed_address:
                try:
                    float(fixed_lnt)
                except (TypeError, ValueError):
                    fields["fixed_lnt"] = {"message": "请输入有效的经度"}
                try:
                    float(fixed_lat)
                except (TypeError, ValueError):
                    fields["fixed_lat"] = {"message": "请输入有效的纬度"}
                if not fixed_area_code:
                    fields["fixed_area_code"] = {"message": "请输入地区编码"}
                if not fixed_address_detail:
                    fields["fixed_address_detail"] = {"message": "请输入详细地址"}
            if fields:
                self._set_status("error")
                self._operations.finish(
                    operation, status="rejected", reason="validation_failed",
                    summary={"fields": fields},
                    next_action="修正表单后重试")
                return {
                    "ok": False,
                    "status": "rejected",
                    "reason": "validation_failed",
                    "next_action": "修正表单后重试",
                    "summary": {"fields": fields},
                    "operation_id": operation.operation_id,
                    "fields": fields,
                }

            # 就地更新并保存：避免全新 AppConfig 把订单处理侧配置重置成默认。
            _apply_sss_payload(self._config, {
                "url": url, "account": account, "excel": excel,
                "order_source": order_source,
                "product_name": str(payload.get("product_name", "轻食")).strip() or "轻食",
                "common_address": str(payload.get("common_address", "")).strip(),
                "use_fixed_address": use_fixed_address,
                "fixed_lnt": fixed_lnt, "fixed_lat": fixed_lat,
                "fixed_area_code": fixed_area_code,
                "fixed_address_detail": fixed_address_detail,
                # dry_run：只组装并打印下单报文，不真实提交（联调/验收用）。
                "dry_run": bool(payload.get("dry_run", self._config.sss_dry_run)),
                "preflight": bool(payload.get("preflight", self._config.sss_preflight)),
            })
            self._merge_sss_store_cache_from_disk()
            self._config.save()
            if payload.get("remember", True):
                set_sss_password(account, password)
                with self._password_clear_lock:
                    self._cleared_password_accounts.discard(("sss", account))
            snapshot = copy.deepcopy(self._config)
            self._task_operation_id = operation.operation_id
            self._task_owner = self._request_identity()
            self._task_owner_is_admin = bool(self.is_admin)
            launched = self._launch("sss", snapshot, None, password)
            if launched is False:
                if self._task_operation_id == operation.operation_id:
                    self._task_operation_id = ""
                    self._task_owner = ""
                    self._task_owner_is_admin = False
                self._operations.finish(
                    operation, status="rejected", reason="operation_conflict",
                    summary={"message": "启动时发现已有任务正在运行"},
                    next_action="等待当前操作结束后重试")
                return self._busy_result_from_worker("已有任务正在运行，请先停止后再启动")
            if not self.worker_alive() and self._operations.is_active(operation):
                self._operations.finish(
                    operation, status="success",
                    reason="worker_finished_before_status_check",
                    summary={"message": "闪时送下单任务已启动"},
                    next_action="")
                if self._task_operation_id == operation.operation_id:
                    self._task_operation_id = ""
                    self._task_owner = ""
                    self._task_owner_is_admin = False
            active = self._operations.is_active(operation)
            return {
                "ok": True,
                "status": "running" if active else "success",
                "reason": "",
                "next_action": ("使用 bridge_ready/operation_status 或事件跟踪进度"
                                if active else ""),
                "summary": {"message": "闪时送下单任务已启动"},
                "operation_id": operation.operation_id,
            }
        except Exception:
            if self._task_operation_id == operation.operation_id:
                self._task_operation_id = ""
                self._task_owner = ""
                self._task_owner_is_admin = False
            self._operations.finish_if_active(
                operation, status="error", reason="start_failed",
                next_action="查看日志后重试")
            raise

    def start_sss_review(self, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        """启动「只读核对未决记录与站内订单」worker（仅管理员，绝不发 POST）。

        为什么单独开一个入口：未决记录闸门在阻断时要求用户“只读核对站内订单与
        本地记录”，但正式运行与预检都会在闸门处提前返回，用户拿不到核对结果。
        本入口只做读操作，因此**允许在被阻断时使用**；它不会解除阻断 —— 解除必须
        走 :meth:`sss_uncertain_resolve` 的人工确认入口。

        请求：``POST /api/start_sss_review``，JSON 数组单对象::

            {"password": "闪时送登录密码"}

        本入口**不写任何凭据**（``remember`` 被忽略）：核对是只读动作，不该有
        配置/密钥副作用。
        """
        data: dict[str, Any] = dict(payload) if isinstance(payload, dict) else {}
        if not self.is_admin:
            # 双保险：HTTP 白名单已经 403；直接调用（脚本/旧客户端）也必须拒绝，
            # 否则受限角色能借这个入口用调用方提供的密码登录平台。
            return {
                "ok": False, "status": "forbidden", "code": "forbidden",
                "reason": "只读核对入口仅管理员可用",
                "next_action": "联系管理员处理",
                "summary": {"message": "只读核对入口仅管理员可用"},
            }
        if self.worker_alive():
            return self._busy_result_from_worker()
        reservation = self._operations.try_reserve(
            "sss_review", summary={"title": "闪时送只读核对"},
            next_action="等待只读核对结束或查询 operation_status")
        if not reservation.granted:
            return self._operation_conflict_payload(reservation.conflict,
                                                    action="启动闪时送只读核对")
        operation = reservation.operation
        assert operation is not None
        try:
            url = str(getattr(self._config, "sss_url", "") or "").strip()
            account = str(getattr(self._config, "sss_account", "") or "").strip()
            password = str(data.get("password", "") or "")
            fields: dict[str, Any] = {}
            if not url:
                fields["url"] = {"message": "请先保存闪时送网址"}
            else:
                try:
                    canonical_sss_origin(url)
                except SssUrlConfigError as exc:
                    fields["url"] = {"message": str(exc)}
            if not account:
                fields["account"] = {"message": "请先保存闪时送账号"}
            if not password:
                fields["password"] = {"message": "请输入登录密码"}
            if fields:
                self._operations.finish(
                    operation, status="rejected", reason="validation_failed",
                    summary={"fields": fields}, next_action="修正表单后重试")
                return {
                    "ok": False, "status": "rejected", "reason": "validation_failed",
                    "next_action": "修正表单后重试", "summary": {"fields": fields},
                    "operation_id": operation.operation_id, "fields": fields,
                }
            # 冻结配置快照：核对期间用户改配置不会改变本次作用域（与 start_sss 同）。
            snapshot = copy.deepcopy(self._config)
            self._task_operation_id = operation.operation_id
            self._task_owner = self._request_identity()
            self._task_owner_is_admin = bool(self.is_admin)
            launched = self._launch("sss_review", snapshot, None, password)
            if launched is False:
                if self._task_operation_id == operation.operation_id:
                    self._task_operation_id = ""
                    self._task_owner = ""
                    self._task_owner_is_admin = False
                self._operations.finish(
                    operation, status="rejected", reason="operation_conflict",
                    summary={"message": "启动时发现已有任务正在运行"},
                    next_action="等待当前操作结束后重试")
                return self._busy_result_from_worker("已有任务正在运行，请先停止后再启动")
            active = self._operations.is_active(operation)
            return {
                "ok": True,
                "status": "running" if active else "success",
                "reason": "",
                "next_action": ("只读核对进行中；可用 bridge_ready/operation_status 跟踪进度"
                                if active else ""),
                "summary": {"message": "闪时送只读核对已启动"},
                "operation_id": operation.operation_id,
            }
        except Exception:
            if self._task_operation_id == operation.operation_id:
                self._task_operation_id = ""
                self._task_owner = ""
                self._task_owner_is_admin = False
            self._operations.finish_if_active(
                operation, status="error", reason="start_failed",
                next_action="查看日志后重试")
            raise

    # 表单防抖即时保存：只落盘本次改动，不做启动校验、不触发任务。
    def save_order_config(self, payload: dict[str, Any]) -> dict[str, Any]:
        """只更新订单处理侧的配置字段并落盘（不触碰闪时送侧）。写盘失败回 ``write_failed``。"""
        conflict = self._reject_if_operation_active("保存订单配置")
        if conflict is not None:
            return conflict
        _apply_order_payload(self._config, payload)
        try:
            self._config.save()
        except OSError:
            return {"ok": False, "reason": "write_failed"}
        return {"ok": True, "saved": {
            "order_date": self._config.order_date,
            "order_count": self._config.order_count,
        }}

    def save_sss_config(self, payload: dict[str, Any]) -> dict[str, Any]:
        """只更新闪时送侧的配置字段并落盘（不触碰订单处理侧）。写盘失败回 ``write_failed``。

        R8-S1：本次请求带 ``url`` 且非空时，必须先通过闪时送网址校验；不合法
        直接回 ``invalid_sss_url``（含操作建议），不写盘、不静默保存成另一种
        authority scope 的写法。
        """
        conflict = self._reject_if_operation_active("保存闪时送配置")
        if conflict is not None:
            return conflict
        try:
            _apply_sss_payload(self._config, payload)
        except SssUrlConfigError as exc:
            return {
                "ok": False,
                "status": "rejected",
                "reason": "invalid_sss_url",
                "next_action": f"{exc}；修正后再保存或启动任务",
                "fields": {"url": {"message": str(exc)}},
            }
        try:
            self._config.save()
        except OSError:
            return {"ok": False, "reason": "write_failed"}
        return {"ok": True}

    def sss_day_orders(self) -> dict[str, Any]:
        """读取云端当天名单（东湖午餐/东湖晚餐）并留档，但不下单。

        「闪时送下单」页签的「读取云端当天名单」按钮用它：只读取 + 写留档
        Excel + 汇报人数，方便下单前先核对日期与名单。任何失败都只返回原因，
        不会触碰下单流程。

        W7：它读的是**同一批云表**，并且会把名单留档进《闪时送.xlsx》
        （可能是闪时送下单来源）。因此必须与云上传/订单/闪时送任务统一互斥，
        否则上传写到一半时读到的“半份名单”会被留档成下单来源文件。
        """
        if self.worker_alive():
            return {"ok": False, "reason": "已有任务正在运行，请先停止后再读取当天名单"}
        operation, conflict = self._begin_cloud_read(
            "sss_day_orders", "读取云端当天名单", "读取云端当天名单")
        if conflict is not None:
            return {
                "ok": False,
                "reason": str(conflict.get("message")
                              or "已有其他操作正在进行，请等待结束后再读取当天名单"),
                "status": str(conflict.get("status") or "rejected"),
                "code": str(conflict.get("code") or "operation_conflict"),
                "operation_id": str(conflict.get("operation_id") or ""),
                "next_action": str(conflict.get("next_action") or ""),
            }
        result: dict[str, Any] = {"ok": False, "reason": "read_aborted"}
        try:
            result = self._sss_day_orders_impl()
        except Exception as exc:  # noqa: BLE001 - 兜底也不把异常抛给前端
            self.log("读取云端当天名单失败：" + str(exc), "ERROR")
            result = {"ok": False, "reason": str(exc)}
        self._finish_cloud_read(operation, result, title="读取云端当天名单")
        return result

    def _sss_day_orders_impl(self) -> dict[str, Any]:
        try:
            day = prepare_day_orders(self._config,
                                     delivery_date=expected_delivery_date(),
                                     log=lambda message: self.log(message))
        except ImportRefused as exc:
            self.log("读取云端当天名单失败：" + str(exc), "ERROR")
            return {"ok": False, "reason": str(exc)}
        except Exception as exc:  # noqa: BLE001 - 界面需要原样原因
            self.log("读取云端当天名单失败：" + str(exc), "ERROR")
            return {"ok": False, "reason": str(exc)}

        summary = day.as_summary()
        parts = []
        for name, info in (summary.get("meals") or {}).items():
            if info.get("skipped"):
                parts.append(f"{name}不下单（{info.get('reason') or '没有当天列'}）")
            else:
                parts.append(
                    f"{name} {info.get('orders', 0)} 人"
                    f"（标 1 共 {info.get('marked', 0)} 人，"
                    f"其中 {info.get('skipped_address', 0)} 人地址是大西/小不下单）")
        self.log(f"云端当天名单 {summary.get('target_date')} "
                 f"{summary.get('date_text')}：" + ("；".join(parts) or "没有数据"), "OK")
        if summary.get("archive_error"):
            self.log("留档 Excel 写入失败：" + str(summary["archive_error"]), "WARN")
        return {"ok": True, **summary}

    # ------------------------------------------------------------------
    # 闪时送未决记录：只读核对 + 管理员带审计解除
    #
    # 背景：闪时送下单 POST 非幂等，POST 结果未知的记录会一直阻断后续运行。
    # 闸门只把“站内确实查到的”记录自动 resolved；“站内查不到”不等于“没落单”，
    # 所以必须由人核对后再决定。这里补上缺失的两个能力：
    # * sss_uncertain_records：只读列出阻断记录（脱敏，仅管理员，无网络）；
    # * sss_uncertain_resolve：管理员带确认/备注/证据地处置（永不发 POST、永不联网）。
    # ------------------------------------------------------------------
    #: 允许的处置决策。
    _SSS_RESOLVE_DECISIONS = ("station_absent", "station_present", "keep")
    #: 审计备注最短长度：强制写下“凭什么可以解除阻断”。
    _SSS_RESOLVE_MIN_NOTE = 4

    def _sss_batch_context(self) -> dict[str, Any]:
        """当前账号/批次的权威未决记录上下文（origin 规则与正式运行完全一致）。"""
        config = self._config
        url = str(getattr(config, "sss_url", "") or "").strip()
        if not url:
            raise UncertainJournalError("尚未配置闪时送网址，无法定位未决记录")
        canonical_sss_origin(url)
        origin = platform_origin(config, url=url)
        account = str(getattr(config, "sss_account", "") or "")
        if not account:
            raise UncertainJournalError("尚未配置闪时送账号，无法定位未决记录")
        source = str(getattr(config, "sss_order_source", "wps") or "wps").strip().lower()
        if source != "excel":
            source = "wps"
        delivery_date = expected_delivery_date()
        return {
            "origin": origin,
            "account": account,
            "source": source,
            "delivery_date": str(delivery_date),
            "batch_key": batch_key(delivery_date, source, account),
            "journal": authoritative_uncertain_path(
                config, origin=origin, account=account),
        }

    def _sss_review_view(self, view: dict[str, Any]) -> dict[str, Any]:
        """把内存里的只读核对证据整理成前端可读字段（含是否仍然有效）。"""
        empty = {
            "available": False, "stale": False, "journal_matches": False,
            "covers_active": False, "checked_at": "", "age_s": None,
            "wide_window_days": _REVIEW_WIDE_WINDOW_DAYS,
            "journal_fingerprint": "", "counts": {}, "classifications": {},
            "cross_scope_conflicts": 0, "cross_scope_error": "",
        }
        with self._sss_review_lock:
            snapshot = dict(self._sss_review_snapshot or {})
        if not snapshot:
            return empty
        try:
            current = journal_fingerprint(view["path"])
        except UncertainJournalError:
            return empty
        checked_at = str(snapshot.get("checked_at") or "")
        try:
            age: float | None = max(0.0, (
                _dt.datetime.now()
                - _dt.datetime.fromisoformat(checked_at)).total_seconds())
        except ValueError:
            age = None
        classifications = snapshot.get("classifications") if isinstance(
            snapshot.get("classifications"), dict) else {}
        active_ids: set[str] = set()
        for record in view["records"]:
            for identifier in (record.get("journal_id"), record.get("identifier")):
                text = str(identifier or "")
                if text:
                    active_ids.add(text)
        covers = bool(active_ids) and all(
            identifier in classifications for identifier in active_ids)
        return {
            "available": True,
            "stale": bool(age is None or age > _SSS_REVIEW_TTL_S),
            "journal_matches": current == str(snapshot.get("journal_fingerprint") or ""),
            "covers_active": covers,
            "checked_at": checked_at,
            "age_s": None if age is None else round(age, 1),
            "wide_window_days": int(snapshot.get("wide_window_days")
                                    or _REVIEW_WIDE_WINDOW_DAYS),
            "journal_fingerprint": str(snapshot.get("journal_fingerprint") or ""),
            "counts": dict(snapshot.get("counts") or {}),
            "classifications": {str(key): str(value)
                                for key, value in classifications.items()},
            "cross_scope_conflicts": int(snapshot.get("cross_scope_conflicts") or 0),
            "cross_scope_error": str(snapshot.get("cross_scope_error") or ""),
        }

    def sss_uncertain_records(self) -> dict[str, Any]:
        """只读列出当前批次仍阻断的未决记录（仅管理员，无网络、不改文件）。

        记录带客户姓名与掩码手机号，因此只给管理员：普通用户在 HTTP 白名单层
        就被 403 拦掉，直接调用也拿到 forbidden。手机号已脱敏，保留核对所需的
        最小信息（姓名 + 送达时间 + 门牌）。
        """
        if not self.is_admin:
            return {
                "ok": False, "status": "forbidden", "code": "forbidden",
                "reason": "未决记录含客户信息，仅管理员可查看",
                "next_action": "联系管理员处理", "read_only": True,
                "contract_version": 1, "records": [], "counts": {}, "review": {},
            }
        try:
            ctx = self._sss_batch_context()
            view = pending_record_views(ctx["journal"], ctx["batch_key"])
        except (UncertainJournalError, SssUrlConfigError) as exc:
            return {
                "ok": False, "status": "failed", "code": "journal_unreadable",
                "reason": str(exc), "next_action": "人工核对本地记录后再运行",
                "read_only": True, "contract_version": 1,
                "records": [], "counts": {}, "review": {},
            }
        records = list(view["records"])
        return {
            "ok": True, "status": "ok", "code": "", "reason": "",
            "read_only": True, "contract_version": 1,
            "origin": ctx["origin"],
            "account": mask_contact(ctx["account"]),
            "journal": str(ctx["journal"]),
            "batch_key": ctx["batch_key"],
            "delivery_date": ctx["delivery_date"],
            "counts": dict(view["counts"]),
            "records": records,
            "review": self._sss_review_view(view),
            "next_action": ("先只读核对站内订单；确认后由管理员在未决记录面板解除阻断"
                            if records else "当前批次没有活跃未决记录，无需处置"),
        }

    def _sss_uncertain_failure(self, code: str, reason: str, next_action: str, *,
                               status: str = "rejected",
                               extra: dict[str, Any] | None = None) -> dict[str, Any]:
        """统一失败形状：永不发 POST、永不写云端，changed 恒为 False。"""
        payload: dict[str, Any] = {
            "ok": False,
            "status": str(status),
            "code": str(code),
            "reason": str(reason or code),
            "next_action": str(next_action or ""),
            "contract_version": 1,
            "read_only": False,
            "cloud_write": False,
            "post_sent": False,
            "changed": False,
            "decision": "",
            "record_ids": [],
            "affected": 0,
            "remaining": 0,
            "audit": {},
            "allowed_decisions": list(self._SSS_RESOLVE_DECISIONS),
        }
        if extra:
            payload.update(extra)
        return payload

    def _sss_uncertain_success(self, *, status: str, decision: str,
                               targets: list[str], affected: int, remaining: int,
                               audit: dict[str, Any],
                               next_action: str) -> dict[str, Any]:
        """统一成功形状：只写本地 journal 审计，post_sent 恒为 False。"""
        return {
            "ok": True,
            "status": str(status),
            "code": "",
            "reason": "",
            "next_action": str(next_action),
            "contract_version": 1,
            "read_only": False,
            "cloud_write": False,
            "post_sent": False,
            "changed": True,
            "decision": str(decision),
            "record_ids": list(targets),
            "affected": int(affected),
            "remaining": int(remaining),
            "audit": dict(audit),
            "allowed_decisions": list(self._SSS_RESOLVE_DECISIONS),
        }

    def sss_uncertain_resolve(self, payload: dict[str, Any] | None = None,
                              **options: Any) -> dict[str, Any]:
        """管理员专用：带确认、证据与审计地处置未决记录（永不发 POST、永不联网）。

        请求（HTTP ``POST /api/sss_uncertain_resolve``，JSON 数组单对象或对象键值均可）::

            {"decision": "station_absent" | "station_present" | "keep",
             "confirm":  "<与 decision 完全相同的字符串>",
             "note":     "人工核对说明（至少 4 个字符，写进审计）",
             "record_ids": ["<journal_id>", ...]}

        语义边界：

        * ``station_present``（站内确实有这些订单）→ 只把记录标成 resolved，本批不再
          重发；这是安全方向，即使判断错也不会因此多下单（提交前还会独立对账）。
        * ``station_absent``（站内确实没有）→ 把记录标成 discarded 以解除阻断。它会让
          下一轮真的重发，因此**必须**有一次新鲜的只读核对证据：该次核对要覆盖全部
          所选记录、每条都判成 ``station_missing``，且 journal 指纹未变；宽窗内查到
          订单、证据过期、journal 变过一律拒绝。
        * ``keep`` → 什么都不改，保持阻断。

        权限：仅管理员。普通用户 HTTP 403 ``admin_only``；绕过 HTTP 直接调用也会得到
        ``ok=false, status=forbidden``。
        """
        data: dict[str, Any] = dict(payload) if isinstance(payload, dict) else {}
        for key, value in options.items():
            if value is not None:
                data[key] = value
        actor = self._request_identity() or "local-admin"
        decision = str(data.get("decision") or "").strip().lower()
        confirm = str(data.get("confirm") or "").strip()
        note = str(data.get("note") or "").strip()
        raw_ids = data.get("record_ids")
        if isinstance(raw_ids, str):
            raw_ids = [raw_ids]
        record_ids = [str(item).strip() for item in (raw_ids or [])
                      if str(item or "").strip()]

        if not self.is_admin:
            # 双保险：HTTP 白名单已经 403；直接调用（脚本/旧客户端）也必须拒绝。
            return self._sss_uncertain_failure(
                "forbidden", "解除未决记录阻断仅管理员可用", "联系管理员处理",
                status="forbidden")
        if decision not in self._SSS_RESOLVE_DECISIONS:
            return self._sss_uncertain_failure(
                "invalid_payload",
                "decision 只允许 station_absent / station_present / keep",
                "选择允许的 decision 后重试", status="invalid")
        if decision == "keep":
            return {
                "ok": True, "status": "kept", "code": "", "reason": "",
                "next_action": "已保留阻断：未决记录未做任何改动",
                "contract_version": 1, "read_only": False, "cloud_write": False,
                "post_sent": False, "changed": False, "decision": decision,
                "record_ids": [], "affected": 0, "remaining": 0, "audit": {},
                "allowed_decisions": list(self._SSS_RESOLVE_DECISIONS),
            }
        if len(note) < self._SSS_RESOLVE_MIN_NOTE:
            return self._sss_uncertain_failure(
                "note_required", "必须填写至少 4 个字符的人工核对说明（写入审计）",
                "补充 note 后重试")
        if confirm != decision:
            return self._sss_uncertain_failure(
                "confirmation_required",
                "必须显式确认：confirm 必须与 decision 完全相同（" + decision + "）",
                "重新提交并令 confirm=" + decision)
        if not record_ids:
            return self._sss_uncertain_failure(
                "invalid_record_ids", "必须明确列出要处置的未决记录 id，不支持整库清空",
                "在未决记录面板里勾选记录后重试")

        reservation = self._operations.try_reserve(
            "sss_uncertain_resolve", summary={"title": "解除闪时送未决记录阻断"},
            next_action="等待处置结束或查询 operation_status")
        if not reservation.granted:
            conflict = self._operation_conflict_payload(
                reservation.conflict, action="处置闪时送未决记录")
            return self._sss_uncertain_failure(
                "operation_conflict", str(conflict.get("reason") or "busy"),
                str(conflict.get("next_action") or "等待当前操作结束后重试"),
                status="rejected",
                extra={"operation_id": str(conflict.get("operation_id") or "")})
        operation = reservation.operation
        assert operation is not None
        result = self._sss_uncertain_failure(
            "internal_error", "处置未完成（异常中止）", "查看日志后重试", status="error")
        try:
            result = self._sss_uncertain_resolve_impl(
                decision=decision, note=note, actor=actor, record_ids=record_ids)
        except UncertainJournalError as exc:
            result = self._sss_uncertain_failure(
                "journal_unreadable", str(exc), "人工核对本地记录文件后再试")
        except OSError as exc:
            result = self._sss_uncertain_failure(
                "journal_write_failed", str(exc), "确认存储可写后重试")
        finally:
            if self._operations.is_active(operation):
                self._operations.finish(
                    operation, status="success" if result.get("ok") else "error",
                    reason=str(result.get("code") or result.get("status") or "")[:200],
                    summary={"title": "解除闪时送未决记录阻断",
                             "decision": decision,
                             "changed": bool(result.get("changed"))},
                    next_action=str(result.get("next_action") or "")[:200])
        return result

    def _sss_uncertain_resolve_impl(self, *, decision: str, note: str, actor: str,
                                    record_ids: list[str]) -> dict[str, Any]:
        """实际写动作：先校验记录归属，再按证据分支 resolved/discarded。"""
        ctx = self._sss_batch_context()
        journal = ctx["journal"]
        key = ctx["batch_key"]
        guard = batch_submission_lock(journal, key)
        try:
            guard.acquire()
        except UncertainJournalError as exc:
            return self._sss_uncertain_failure(
                "journal_write_failed", "无法获取未决记录锁：" + str(exc),
                "等待另一个进程结束后重试")
        try:
            view = pending_record_views(journal, key)
            active: dict[str, dict[str, Any]] = {}
            for record in view["records"]:
                for identifier in (record.get("journal_id"), record.get("identifier")):
                    text = str(identifier or "")
                    if text:
                        active[text] = record
            unknown = [identifier for identifier in record_ids
                       if identifier not in active]
            if unknown:
                return self._sss_uncertain_failure(
                    "unknown_record_ids",
                    "选择的记录已不存在或不属于当前批次：" + "、".join(unknown[:5]),
                    "刷新未决记录列表后重新选择",
                    extra={"unknown_record_ids": unknown[:20],
                           "remaining": len(view["records"])})
            targets = sorted({str(active[identifier].get("journal_id") or identifier)
                              for identifier in record_ids})
            fingerprint = journal_fingerprint(journal)
            timestamp = _dt.datetime.now().isoformat(timespec="seconds")

            if decision == "station_present":
                # 安全方向：站内确实有这些订单 → 标记 resolved，本批不再重发。
                resolved = resolve_uncertain_records(
                    journal, key, targets, note=note, actor=actor)
                if resolved != len(targets):
                    return self._sss_uncertain_failure(
                        "journal_write_failed",
                        "只写入 " + str(resolved) + "/" + str(len(targets))
                        + " 条，状态不可信",
                        "人工核对本地记录文件后再试", status="error")
                remaining = len(pending_record_views(journal, key)["records"])
                return self._sss_uncertain_success(
                    status="resolved", decision=decision, targets=targets,
                    affected=resolved, remaining=remaining,
                    audit={"actor": actor, "note": note, "at": timestamp,
                           "journal": str(journal),
                           "journal_fingerprint": fingerprint, "reviewed_at": ""},
                    next_action="已按“站内确实有”标记确认；重跑本批不会再提交这些订单")

            # station_absent：解除阻断会让下一轮真的重发，因此必须先有一次新鲜的、
            # 覆盖全部所选记录、且把每条都判成“站内缺失”的只读核对作为证据。
            with self._sss_review_lock:
                snapshot = dict(self._sss_review_snapshot or {})
            if not snapshot:
                return self._sss_uncertain_failure(
                    "review_required", "必须先做一次只读核对，再用核对结果解除阻断",
                    "先点“只读核对站内订单”，确认站内确实没有这些订单")
            checked_at = str(snapshot.get("checked_at") or "")
            try:
                age = (_dt.datetime.now()
                       - _dt.datetime.fromisoformat(checked_at)).total_seconds()
            except ValueError:
                age = _SSS_REVIEW_TTL_S + 1.0
            if age < 0 or age > _SSS_REVIEW_TTL_S:
                return self._sss_uncertain_failure(
                    "review_stale",
                    "只读核对结果已过期（" + str(int(max(age, 0.0))) + " 秒前）",
                    "重新只读核对后再解除")
            if str(snapshot.get("journal_fingerprint") or "") != fingerprint:
                return self._sss_uncertain_failure(
                    "journal_changed", "本地未决记录在只读核对之后发生了变化",
                    "重新只读核对后再解除")
            classifications = snapshot.get("classifications") if isinstance(
                snapshot.get("classifications"), dict) else {}
            found_other_day = [identifier for identifier in targets
                               if str(classifications.get(identifier) or "")
                               == "station_found_other_day"]
            if found_other_day:
                return self._sss_uncertain_failure(
                    "station_state_changed",
                    "只读核对在站内宽窗内找到了这些订单（送达日不同）："
                    + "、".join(found_other_day[:5]),
                    "先人工核对站内订单；若确认已落单，请改用“已在站内找到”")
            not_covered = [identifier for identifier in targets
                           if str(classifications.get(identifier) or "")
                           != "station_missing"]
            if not_covered:
                return self._sss_uncertain_failure(
                    "review_required",
                    "这次只读核对没有覆盖全部所选记录（或扫描未完成）",
                    "重新只读核对后再解除")

            discarded = discard_uncertain_records(
                journal, key, targets,
                reason="人工核对站内无此订单，确认未落单（管理员带审计解除）",
                note=note, actor=actor)
            if discarded != len(targets):
                return self._sss_uncertain_failure(
                    "journal_write_failed",
                    "只写入 " + str(discarded) + "/" + str(len(targets)) + " 条，状态不可信",
                    "人工核对本地记录文件后再试", status="error")
            remaining = len(pending_record_views(journal, key)["records"])
            return self._sss_uncertain_success(
                status="discarded", decision=decision, targets=targets,
                affected=discarded, remaining=remaining,
                audit={"actor": actor, "note": note, "at": timestamp,
                       "journal": str(journal),
                       "journal_fingerprint": fingerprint,
                       "reviewed_at": checked_at},
                next_action=("阻断已解除；重跑本批时提交前仍会再做一次只读对账，"
                             "若站内已存在则不会重复提交"))
        finally:
            guard.release()

    def _launch(self, mode: str, config: AppConfig, count: int | None, password: str) -> bool:
        """启动 worker；已有任务时拒绝，返回 False（不覆盖在跑线程）。"""
        with self._worker_lock:
            if self._worker is not None and self._worker.is_alive():
                self.log("已有任务在运行，拒绝并发启动", "WARN")
                return False
            self._stop_event.clear()
            self._task_finished = False
            self._set_status("running")
            self.log({
                "order": "开始处理订单...",
                "sss_review": "开始只读核对未决记录与站内订单（不会发送任何下单请求）...",
            }.get(mode, "开始闪时送下单..."))
            target = {"order": self._run_order,
                      "sss_review": self._run_sss_review}.get(mode, self._run_sss)
            args = (config, count, password) if mode == "order" else (config, password)
            operation_id = self._task_operation_id

            def guarded_target() -> None:
                try:
                    target(*args)
                finally:
                    # 正常路径会由 _finish_task/_task_error 先终结；这里只兜住
                    # 未走常规出口的 BaseException/编程错误，保证互斥槽位不会永久泄漏。
                    if self._operations.is_active(operation_id):
                        self._operations.finish_if_active(
                            operation_id, status="error",
                            reason="worker_exited_without_result",
                            next_action="查看日志后重新启动")

            self._worker = threading.Thread(target=guarded_target, daemon=True)
            self._worker.start()
            return True

    def _run_order(self, config: AppConfig, count: int | None, password: str) -> None:
        try:
            result = run_job(config, count, self._stop_event, lambda msg: self.log(msg), password=password,
                             order_decision_callback=self._order_decision,
                             save_decision_callback=self._save_decision,
                             pending_address_callback=self._pending_address_input)
            self._finish_task(self._order_task_message(result), result)
        except Exception as exc:
            self._task_error(str(exc))

    def _run_sss(self, config: AppConfig, password: str) -> None:
        try:
            result = run_sss_job(config, self._stop_event, lambda msg: self.log(msg),
                                 password=password, decision_callback=self._sss_decision,
                                 captcha_callback=self._sss_captcha,
                                 store_cache_callback=self._remember_sss_store_cache)
            status = result.get("status")
            source_label = ("本地 Excel" if result.get("source") == "excel"
                            else "云端当天名单")
            if status == "dry_run":
                message = (f"闪时送干跑完成：已组装 {result.get('previewed', 0)} 单"
                           f"（名单来源：{source_label}），未创建真实订单")
            elif status == "no_orders":
                meals = (result.get("import") or {}).get("meals") or {}
                detail = "；".join(
                    f"{name}不下单（{info.get('reason') or '没有当天列'}）"
                    if info.get("skipped") else f"{name} {info.get('orders', 0)} 人"
                    for name, info in meals.items())
                message = "闪时送没有需要下单的订单" + (f"：{detail}" if detail else "")
            elif status == "insufficient_balance":
                message = (f"闪时送已安全停止：余额不足，本批未提交，"
                           f"预计 {result.get('estimate', '?')} 元")
            elif status == "balance_unknown":
                message = "闪时送已安全停止：余额未知，本批未提交"
            elif status == "preflight_ok":
                message = (f"闪时送预检完成：已有站内匹配 {result.get('created', 0)} 单，"
                           "未提交新订单")
            elif status in {"preflight_uncertain", "duplicate_detected"}:
                message = "闪时送预检停止：未提交新订单，请先处理日志中的风险"
            elif status in {"failed", "error"}:
                message = ("闪时送任务失败：本批结果未成功确认，请查看日志并只读核对，"
                           "严禁重跑或补发")
            elif status == "blocked_concurrent":
                # R6-9：并发阻断不是“站内对账失败”。runner 在发送任何 POST 之前
                # 就因批次级跨进程锁拿不到而退出，用户要做的只是等待后刷新。
                message = ("另一个任务正在运行，请等待后刷新"
                           "（本次未发送任何下单请求）")
            elif status == "blocked_uncertain":
                message = ("闪时送任务被阻断：存在未解决的不确定记录，"
                           "请先只读核对站内订单与本地记录；未确认前不要重跑或补发")
            elif status in {"unconfirmed", "uncertain"}:
                message = ("闪时送任务结果未确认：存在未确认或已发送未知订单，"
                           "请仅做只读核对，不要重试或重跑本批")
            elif result.get("stopped"):
                reconciliation = "已完成站内对账" if result.get("reconciled") else "站内对账失败"
                message = (f"闪时送任务已停止：{reconciliation}，"
                           f"已确认 {result.get('created', '?')}/{result.get('processed', '?')} 单"
                           + ("" if result.get("reconciled") else "，请勿手动重复提交"))
            elif result.get("uncertain") or not result.get("reconciled"):
                result["partial"] = True
                message = ("闪时送任务结束：站内对账失败，无法确认已创建数量，"
                           "请勿手动重复提交")
            elif result.get("partial"):
                created = result.get("created", "?")
                processed = result.get("processed", "?")
                missing = processed - created if isinstance(processed, int) and isinstance(created, int) else "?"
                message = (f"闪时送任务部分完成：已确认 {created}/{processed} 单，"
                           f"{missing} 单未完成")
            else:
                message = (f"闪时送下单完成：已创建 {result.get('created', '?')} 单，"
                           f"处理 {result.get('processed', '?')} 项")
            self._finish_task(message, result)
        except Exception as exc:
            self._task_error(str(exc))

    def _run_sss_review(self, config: AppConfig, password: str) -> None:
        """只读核对 worker：只读站内订单与本地未决记录，绝不发送 POST。"""
        try:
            result = run_sss_review_job(
                config, self._stop_event, lambda msg: self.log(msg),
                password=password, captcha_callback=self._sss_captcha,
                snapshot_sink=self._remember_sss_review)
            self._finish_task(self._sss_review_message(result), result)
        except Exception as exc:
            self._task_error(str(exc))

    def _remember_sss_review(self, snapshot: dict[str, Any]) -> None:
        """保存逐条分类证据（仅内存）：写入口只认它，不认前端的自述。"""
        with self._sss_review_lock:
            self._sss_review_snapshot = dict(snapshot or {})

    @staticmethod
    def _sss_review_message(result: dict[str, Any]) -> str:
        """只读核对的消息：读不到就说读不到，绝不写成“站内没有”。"""
        review = result.get("review") if isinstance(result.get("review"), dict) else {}
        counts = review.get("counts") if isinstance(review.get("counts"), dict) else {}
        status = str(result.get("status") or "")
        if status == "review_ok":
            return ("只读核对完成：" + str(counts.get("station_confirmed", 0))
                    + " 条未决记录已在站内确认并清理，阻断已解除")
        if status == "review_failed":
            return ("只读核对失败：没有拿到可用的核对结果，请查看日志后重试，"
                    "不要据此解除阻断")
        return ("只读核对完成：站内缺失 " + str(counts.get("station_missing", 0))
                + " 条、宽窗内命中（送达日不同）"
                + str(counts.get("station_found_other_day", 0))
                + " 条、无法完成扫描 " + str(counts.get("scan_failed", 0))
                + " 条；未确认前不要重跑或补发")

    @staticmethod
    def _order_task_message(result: dict[str, Any]) -> str:
        """订单 runner 明确 status 对应的保守完成消息，避免 failed/partial 被说成成功。"""
        status = str(result.get("status") or "").strip().lower()
        processed = result.get("processed", "?")
        found = result.get("found", "?")
        planned = result.get("planned", "?")
        if status == "confirmed":
            return f"处理完成：已处理 {processed} 项，找到 {found} 项"
        if status == "no_orders":
            return "订单处理结束：没有需要处理的订单，未创建真实订单"
        if status == "partial":
            return (f"订单处理部分完成：已处理 {found}/{planned} 个订单，"
                    "存在未完成订单；请核对失败单号后再处理，不要手工追加")
        if status in ("failed", "error"):
            return "订单处理失败：本轮未成功完成，请查看日志并只读核对原表/站内结果后处理"
        if status == "stopped":
            return "订单处理已停止：本轮未成功完成，请查看日志并核对原表/备份后再处理"
        return (f"订单处理结束：状态 {status or '未知'}，已处理 {processed} 项，"
                f"找到 {found} 项")

    @staticmethod
    def _task_status_flags(result: dict[str, Any]) -> dict[str, Any]:
        """把 runner 的明确 status 归一化成保守的 Bridge 状态与布尔标记。"""
        explicit = str(result.get("status") or "").strip().lower()
        try:
            failed_count = int(result.get("failed") or 0)
        except (TypeError, ValueError):
            failed_count = 0
        try:
            confirmed_count = int(result.get("created")
                                  or result.get("confirmed")
                                  or result.get("found") or 0)
        except (TypeError, ValueError):
            confirmed_count = 0
        stopped_flag = bool(result.get("stopped"))
        partial_flag = bool(result.get("partial"))
        uncertain_flag = bool(result.get("uncertain"))

        status_map = {
            "confirmed": "success",
            "success": "success",
            "ok": "success",
            "partial": "partial",
            "stopped": "stopped",
            "failed": "error",
            "error": "error",
            "blocked_concurrent": "blocked_concurrent",
            "blocked_uncertain": "blocked_uncertain",
            "uncertain": "uncertain",
            "unconfirmed": "uncertain",
            "preflight_uncertain": "uncertain",
            "duplicate_detected": "uncertain",
            "dry_run": "dry_run",
            "preflight_ok": "preflight_ok",
            # 只读核对（review）：读成功但仍有阻断 → 仍是 blocked_uncertain；
            # 核对失败 → error（绝不能落进 success/uncertain 的模糊地带）。
            "review_ok": "success",
            "review_blocked": "blocked_uncertain",
            "review_failed": "error",
            "no_orders": "no_orders",
            "insufficient_balance": "insufficient_balance",
            "balance_unknown": "balance_unknown",
        }
        if explicit:
            status = status_map.get(explicit, explicit)
            if status == "blocked_concurrent":
                # R6-9：并发阻断发生在任何 POST 之前，没有需要核对的对账结果，
                # 但必须保持“未创建订单、不是成功”的保守语义。
                return {
                    "explicit_status": explicit,
                    "status": "blocked_concurrent",
                    "success": False,
                    "ok": False,
                    "real_order": False,
                    "partial": False,
                    "stopped": True,
                    "uncertain": False,
                    "blocked": True,
                    "needs_review": False,
                }
            if status == "success" and (failed_count > 0 or uncertain_flag):
                status = "uncertain" if uncertain_flag else (
                    "partial" if partial_flag or confirmed_count > 0 else "error")
            elif status == "success" and partial_flag:
                status = "partial"
            elif status == "success" and stopped_flag:
                status = "stopped"
        else:
            if failed_count > 0:
                status = "partial" if (partial_flag or confirmed_count > 0) else "error"
            elif partial_flag or uncertain_flag:
                status = "partial"
            elif stopped_flag:
                status = "stopped"
            else:
                status = "success"

        success = status == "success"
        return {
            "explicit_status": explicit,
            "status": status,
            "success": success,
            "ok": success,
            "real_order": bool(success and explicit in ("", "confirmed", "success", "ok")),
            "partial": status == "partial",
            "stopped": bool(stopped_flag or status in (
                "stopped", "insufficient_balance", "balance_unknown")),
            "uncertain": bool(uncertain_flag or status in (
                "uncertain", "blocked_uncertain")),
            "blocked": bool(status == "blocked_uncertain" or result.get("blocked")),
            "needs_review": bool(not success and status not in ("no_orders",)),
        }

    @staticmethod
    def _task_next_action(result: dict[str, Any], status: str) -> str:
        next_action = str(result.get("next_action") or "").strip()
        if next_action:
            return next_action
        if status == "blocked_concurrent":
            # R6-9：并发阻断发生在任何 POST 之前，不需要“只读核对”那套动作。
            return "另一个任务正在运行，请等待后刷新"
        if result.get("uncertain") and status != "success":
            # 无显式 next_action 的旧 runner 结果也必须落到“只读核对”语义。
            return "只读核对，不要重试或重跑本批"
        defaults = {
            "success": "",
            "partial": "只处理未完成项；核对失败单号后再处理，不要手工追加",
            "stopped": "查看日志并核对结果；不要整批重跑",
            "error": "查看日志并核对结果；修复后重试，不要重复提交",
            "uncertain": "只读核对，不要重试或重跑本批",
            "blocked_concurrent": "另一个任务正在运行，请等待后刷新",
            "blocked_uncertain": "先只读核对站内订单与本地记录；未确认前不要重跑或补发",
            "dry_run": "干跑未发送任何 POST；确认报文后再正式运行",
            "preflight_ok": "预检只读模式，未提交新订单；确认后再正式运行",
            "no_orders": "没有需要处理的订单，无需操作",
            "insufficient_balance": "余额不足，本批未提交；充值后先只读核对再运行",
            "balance_unknown": "余额未知，本批未提交；确认余额后先只读核对再运行",
        }
        return defaults.get(status, "核对任务结果后再决定下一步")

    @staticmethod
    def _task_message(status: str, message: str) -> str:
        """失败/不确定消息不能沿用成功型“处理完成/下单完成”文案。"""
        text = str(message or "").strip()
        labels = {
            "error": "任务失败",
            "uncertain": "任务结果不确定，需人工核对",
            "blocked_uncertain": "任务被阻断，需人工核对",
            "blocked_concurrent": "另一个任务正在运行，请等待后刷新",
            "dry_run": "干跑完成（未真实下单）",
            "preflight_ok": "预检完成（未提交新订单）",
            "no_orders": "没有需要处理的订单",
            "insufficient_balance": "任务已安全停止（余额不足）",
            "balance_unknown": "任务已安全停止（余额未知）",
        }
        if status == "blocked_concurrent":
            # R6-9：并发阻断只允许这一句用户可读文案。旧消息链把 blocked_concurrent
            # 落进 stopped 分支输出“站内对账失败”，会让人以为需要对账/重试。
            canonical = "另一个任务正在运行，请等待后刷新（本次未发送任何下单请求）"
            return canonical if "另一个任务正在运行" not in text else text
        if not text:
            return labels.get(status, "任务未成功；请核对结果")
        success_tokens = ("处理完成", "下单完成", "已创建", "成功", "已完成")
        safety_tokens = (
            "失败", "部分", "停止", "未确", "未提交", "未创建", "风险",
            "核对", "未完成", "不要", "阻断", "无法确认",
        )
        if (status in ("error", "uncertain", "blocked_uncertain")
                and any(token in text for token in success_tokens)
                and not any(token in text for token in safety_tokens)):
            text = f"{labels.get(status, '任务未成功')}：{text}"
        return text

    def _task_outcome(self, message: str,
                      result: dict[str, Any]) -> dict[str, Any]:
        flags = self._task_status_flags(result)
        status = str(flags["status"])
        next_action = self._task_next_action(result, status)
        reason = str(result.get("reason") or result.get("semantics")
                     or result.get("abort_reason") or "").strip()
        if not reason and status != "success":
            reason = f"task_status:{status}"
        summary: dict[str, Any] = {}
        raw_summary = result.get("summary")
        if isinstance(raw_summary, dict):
            summary.update(raw_summary)
        summary.setdefault("status", flags["explicit_status"] or status)
        summary.update({
            "bridge_status": status,
            "ok": flags["ok"],
            "success": flags["success"],
            "real_order": flags["real_order"],
            "stopped": flags["stopped"],
            "partial": flags["partial"],
            "uncertain": flags["uncertain"],
            "blocked": flags["blocked"],
            "needs_review": flags["needs_review"],
            "next_action": next_action,
        })
        if reason:
            summary["reason"] = reason
        summary["result"] = result
        text = self._task_message(status, message)
        done_statuses = {
            "success", "noop", "partial", "stopped", "dry_run",
            "preflight_ok", "no_orders", "insufficient_balance",
            "balance_unknown",
        }
        return {
            **flags,
            "message": text,
            "next_action": next_action,
            "reason": reason,
            "summary": summary,
            "event": "task:done" if status in done_statuses else "task:error",
            "log_level": ("OK" if flags["success"] else (
                "WARN" if status in (
                    "partial", "stopped", "dry_run", "preflight_ok", "no_orders",
                    "insufficient_balance", "balance_unknown",
                    "blocked_concurrent") else "ERROR")),
        }

    def _finish_task(self, message: str, result: dict[str, Any]) -> None:
        with self._task_finish_lock:
            if self._task_finished:
                return
            self._task_finished = True
        outcome = self._task_outcome(message, result)
        operation_id = self._task_operation_id
        self._task_operation_id = ""
        self._cancel_pending_interactions("任务结束")
        self._task_owner = ""
        self._task_owner_is_admin = False
        self.log(outcome["message"], outcome["log_level"])
        self._worker = None
        status = outcome["status"]
        self._set_status(status)
        self._operations.finish(
            operation_id, status=status,
            reason=str(outcome["reason"] or ""),
            summary=dict(outcome["summary"]),
            next_action=str(outcome["next_action"] or ""))
        payload = {
            "message": outcome["message"],
            "stopped": outcome["stopped"],
            "partial": outcome["partial"],
            "status": status,
            "result_status": outcome["explicit_status"] or status,
            "ok": outcome["ok"],
            "success": outcome["success"],
            "real_order": outcome["real_order"],
            "uncertain": outcome["uncertain"],
            "blocked": outcome["blocked"],
            "needs_review": outcome["needs_review"],
            "next_action": outcome["next_action"],
            "reason": outcome["reason"],
            "summary": outcome["summary"],
            "operation_id": operation_id,
            "result": result,
        }
        self._emit_event(str(outcome["event"]), payload)

    def _merge_sss_store_cache_from_disk(self) -> None:
        """避免运行线程写入的门店缓存被下一次启动时的旧内存配置覆盖。"""
        try:
            stored = AppConfig.load(self._config.config_path)
            if stored.sss_store_name_cached == self._config.sss_store_name:
                self._config.sss_store_id = stored.sss_store_id
                self._config.sss_store_name_cached = stored.sss_store_name_cached
        except Exception:
            pass

    def _remember_sss_store_cache(self, store_name: str, store_id: int) -> None:
        """把 worker 配置快照中发现的门店缓存同步回常驻配置。"""
        try:
            self._config.sss_store_id = int(store_id)
            self._config.sss_store_name_cached = str(store_name)
            self._config.save()
        except Exception:
            pass

    def _task_error(self, message: str) -> None:
        with self._task_finish_lock:
            if self._task_finished:
                return
            self._task_finished = True
        operation_id = self._task_operation_id
        self._task_operation_id = ""
        self._cancel_pending_interactions("任务异常")
        self._task_owner = ""
        self._task_owner_is_admin = False
        self.log("错误: " + message, "ERROR")
        self._worker = None
        self._set_status("error")
        next_action = "查看日志后重试"
        summary = {
            "message": message,
            "status": "error",
            "ok": False,
            "success": False,
            "real_order": False,
            "stopped": False,
            "partial": False,
            "uncertain": False,
            "blocked": False,
            "needs_review": True,
            "next_action": next_action,
        }
        self._operations.finish(
            operation_id, status="error", reason="task_error",
            summary=summary, next_action=next_action)
        self._emit_event("task:error", {
            "message": message,
            "status": "error",
            "result_status": "error",
            "ok": False,
            "success": False,
            "real_order": False,
            "stopped": False,
            "partial": False,
            "uncertain": False,
            "blocked": False,
            "needs_review": True,
            "next_action": next_action,
            "reason": "task_error",
            "summary": summary,
            "operation_id": operation_id,
            "result": {},
        })

    def stop_task(self) -> dict[str, Any]:
        """请求停止当前任务（置停止事件并取消等待中的交互）。没有任务在跑时回 ``{"ok": False}``。"""
        if not self._worker or not self._worker.is_alive():
            return {"ok": False}
        self._stop_event.set()
        # 等待 decision/captcha 的 worker 必须被唤醒，否则 stop 后仍永久挂起。
        self._cancel_pending_interactions("用户停止任务")
        self._set_status("stopping")
        self._operations.update(
            self._task_operation_id, phase="stopping",
            next_action="等待任务完成站内对账/退出")
        self.log("已请求停止，正在等待浏览器操作结束...")
        return {"ok": True}

    def worker_alive(self) -> bool:
        """当前是否有任务线程在运行。"""
        return bool(self._worker and self._worker.is_alive())

    # ------------------------------------------------------------------
    # js_api：阻塞式决策（旧 askyesnocancel 的异步等价物）
    # ------------------------------------------------------------------
    def _interaction_default(self, kind: str) -> Any:
        if kind == "captcha":
            return ""
        if kind == "address_input":
            return {}
        if kind == "close_confirm":
            return "keep"
        if kind == "save_retry":
            return "cancel"
        return "stop"

    def _register_interaction(self, kind: str,
                              request: dict[str, Any] | None = None
                              ) -> tuple[str, _PendingInteraction]:
        """登记交互；记录稳定 operation_id 与所有者，供只读恢复过滤。"""
        now = time.time()
        owner = self._task_owner or self._request_identity()
        owner_is_admin = bool(self._task_owner_is_admin or self.is_admin)
        with self._push_lock:
            self._decision_seq += 1
            prefix = "c" if kind == "captcha" else "d"
            interaction_id = f"{prefix}{self._decision_seq}"
            entry = _PendingInteraction(
                event=threading.Event(),
                kind=kind,
                created_at=now,
                expires_at=now + max(0.0, float(self._interaction_timeout_s)),
                operation_id=(self._task_operation_id
                              or self._operations.active_operation_id()),
                owner=owner,
                owner_is_admin=owner_is_admin,
                request=copy.deepcopy(dict(request or {})),
            )
            self._decisions[interaction_id] = entry
        return interaction_id, entry

    def _wait_interaction(self, interaction_id: str, entry: _PendingInteraction, *, default: Any = "") -> Any:
        entry.event.wait(self._interaction_timeout_s)
        timed_out = False
        with self._push_lock:
            current = self._decisions.pop(interaction_id, None)
            if current is entry:
                if not entry.holder:
                    entry.holder.append(default)
                timed_out = True
        if timed_out:
            shown = default or "取消"
            self.log(f"交互请求 {interaction_id} 等待超时或窗口关闭，自动按「{shown}」处理", "WARN")
            self._clear_android_interaction_notification()
        return entry.holder[0] if entry.holder else default

    def _notify_android_interaction(self, kind: str) -> None:
        """APK 模式下把等待交互同步到前台通知；失败不影响任务。"""
        if not android_runtime.is_android():
            return
        try:
            android_runtime.notify_interaction(kind)
        except android_runtime.AndroidRuntimeError:
            pass

    def _clear_android_interaction_notification(self) -> None:
        if not android_runtime.is_android():
            return
        try:
            android_runtime.clear_interaction_notifications()
        except android_runtime.AndroidRuntimeError:
            pass

    def _cancel_pending_interactions(self, reason: str,
                                     except_kinds: frozenset[str] = frozenset()) -> int:
        """唤醒所有等待中的 decision/captcha，避免 worker/关闭线程永久阻塞。"""
        with self._push_lock:
            entries = [
                (interaction_id, entry)
                for interaction_id, entry in list(self._decisions.items())
                if entry.kind not in except_kinds
            ]
            for interaction_id, _entry in entries:
                self._decisions.pop(interaction_id, None)
        for interaction_id, entry in entries:
            if not entry.holder:
                entry.holder.append(self._interaction_default(entry.kind))
            entry.event.set()
        if entries:
            self.log(f"已取消 {len(entries)} 个等待中的交互请求（{reason}）", "WARN")
            self._clear_android_interaction_notification()
        return len(entries)

    def _request_decision(self, kind: str, title: str, message: str,
                          choices: list[dict[str, str]]) -> str:
        decision_id, entry = self._register_interaction(kind, request={
            "title": title, "message": message, "choices": copy.deepcopy(choices),
        })
        self._emit_event("decision", {"id": decision_id, "kind": kind, "title": title,
                                "message": message, "choices": choices})
        return str(self._wait_interaction(decision_id, entry, default=self._interaction_default(kind)))

    @staticmethod
    def _sanitize_interaction_request(value: Any) -> Any:
        """递归移除交互 request 中可能出现的密码/令牌键，不碰业务事实字段。"""
        secret_keys = frozenset({
            "password", "passwd", "pwd", "code", "token", "token_hash",
            "secret", "credential", "authorization", "cookie",
        })
        if isinstance(value, dict):
            return {
                str(key): Bridge._sanitize_interaction_request(item)
                for key, item in value.items()
                if str(key).strip().lower() not in secret_keys
            }
        if isinstance(value, list):
            return [Bridge._sanitize_interaction_request(item) for item in value]
        return copy.deepcopy(value)

    @classmethod
    def _safe_pending_address_items(cls, value: Any) -> list[dict[str, Any]]:
        if not isinstance(value, list):
            return []
        items: list[dict[str, Any]] = []
        for item in value[:200]:
            if not isinstance(item, dict):
                continue
            numbers = item.get("order_numbers")
            if not isinstance(numbers, list):
                numbers = []
            items.append({
                "raw_address": str(item.get("raw_address") or "")[:500],
                "order_numbers": [str(number)[:64] for number in numbers[:50]],
                "campus": str(item.get("campus") or "")[:200],
                "reason": str(item.get("reason") or "")[:500],
                "suggested_point": str(item.get("suggested_point") or "")[:500],
                "confidence": str(item.get("confidence") or "")[:100],
            })
        return items

    @classmethod
    def _safe_pending_request(cls, kind: str, value: Any) -> dict[str, Any]:
        """按 kind 白名单重建交互 request，未知/未来新增字段一律丢弃。"""
        request = value if isinstance(value, dict) else {}
        if kind == "captcha":
            image = request.get("image")
            return {"image": image} if isinstance(image, str) and image else {}
        title = str(request.get("title") or "")[:500]
        message = str(request.get("message") or "")[:4000]
        if kind == "address_input":
            return {"title": title, "message": message,
                    "items": cls._safe_pending_address_items(request.get("items"))}
        choices: list[dict[str, str]] = []
        raw_choices = request.get("choices")
        if isinstance(raw_choices, list):
            for item in raw_choices[:50]:
                if not isinstance(item, dict):
                    continue
                choices.append({
                    "value": str(item.get("value") or "")[:200],
                    "label": str(item.get("label") or "")[:500],
                    "style": str(item.get("style") or "")[:50],
                })
        return {"title": title, "message": message, "choices": choices}

    def pending_interactions(self, operation_id: str = "") -> dict[str, Any]:
        """只读列出当前有效且调用者有权处理的交互。

        - 管理员可见全部；普通用户只可见自己发起的任务的交互。
        - ``operation_id`` 非空时只返回该操作名下的交互。
        - 只返回未过期的 pending 项；不会 resolve/消费/取消任何交互。
        - 不返回密码、令牌、holder 中已提交的值或任务上下文之外的数据。
        """
        wanted_operation = str(operation_id or "").strip()
        viewer_admin = bool(self.is_admin)
        viewer_identity = self._request_identity()
        now = time.time()
        with self._push_lock:
            entries = list(self._decisions.items())
        interactions: list[dict[str, Any]] = []
        for interaction_id, entry in entries:
            if entry.expires_at and now >= entry.expires_at:
                continue
            if wanted_operation and str(entry.operation_id or "") != wanted_operation:
                continue
            is_owner = bool(viewer_identity) and entry.owner == viewer_identity
            if not viewer_admin and not is_owner:
                # 普通用户必须是有名有姓的发起者本人；空 owner 的遗留/后台交互
                # 不向普通用户公开，避免借旧 pending 列表越权。
                continue
            # 管理员如果不是该交互 owner，只给元数据；request 属于任务业务数据，
            # 不把“管理员身份”默认等价于可见全部客户敏感明细。
            request = (self._safe_pending_request(
                str(entry.kind or ""),
                self._sanitize_interaction_request(dict(entry.request or {})))
                if is_owner else {})
            item = {
                "interaction_id": interaction_id,
                "operation_id": str(entry.operation_id or ""),
                "kind": str(entry.kind or ""),
                "created_at": float(entry.created_at or 0.0),
                "expires_at": float(entry.expires_at or 0.0),
                "status": "pending",
                "request": request,
            }
            if not is_owner:
                item["request_redacted"] = True
            interactions.append(item)
        interactions.sort(key=lambda item: item["created_at"])
        return {
            "ok": True,
            "interactions": interactions,
            "count": len(interactions),
            "next_action": ("resolve_decision/resolve_captcha/resolve_address_input"
                            if interactions else ""),
        }

    def _resolve_interaction(self, interaction_id: str, value: Any, *,
                             kinds: frozenset[str],
                             clear_notification: bool = False) -> dict[str, Any]:
        key = str(interaction_id or "").strip()
        with self._push_lock:
            entry = self._decisions.get(key)
            if entry is None:
                return {"ok": False, "status": "not_pending",
                        "reason": "interaction_not_found_or_resolved",
                        "interaction_id": key, "operation_id": "",
                        "next_action": "pending_interactions"}
            if not self.is_admin:
                caller = self._request_identity()
                if not caller or entry.owner != caller:
                    return {"ok": False, "status": "forbidden",
                            "reason": "interaction_not_owned_by_caller",
                            "interaction_id": key,
                            "operation_id": str(entry.operation_id or ""),
                            "next_action": "pending_interactions"}
            if entry.kind not in kinds:
                return {"ok": False, "status": "wrong_kind",
                        "reason": "interaction_kind_mismatch",
                        "interaction_id": key,
                        "operation_id": str(entry.operation_id or ""),
                        "next_action": "pending_interactions"}
            now = time.time()
            if entry.expires_at and now >= entry.expires_at:
                self._decisions.pop(key, None)
                return {"ok": False, "status": "expired",
                        "reason": "interaction_expired",
                        "interaction_id": key,
                        "operation_id": str(entry.operation_id or ""),
                        "next_action": "等待任务结束/重新发起任务"}
            self._decisions.pop(key, None)
            entry.holder.append(value)
            entry.event.set()
        if clear_notification:
            self._clear_android_interaction_notification()
        return {"ok": True, "status": "accepted", "interaction_id": key,
                "operation_id": str(entry.operation_id or ""),
                "next_action": ""}

    def resolve_decision(self, decision_id: str, choice: str) -> dict[str, Any]:
        """把用户在决策弹窗里的选择交回等待中的任务线程。

        同一个 id 只能兑现一次；重复/未知返回结构化非成功结果，不抛异常。
        """
        return self._resolve_interaction(
            decision_id, str(choice),
            kinds=frozenset({"order_retry", "sss_retry", "save_retry",
                             "close_confirm", "decision"}))

    def _order_decision(self, code: str, error: str) -> str:
        return self._request_decision(
            "order_retry", "订单定位失败",
            f"订单 {code} 定位失败：\n{error}", RETRY_CHOICES)

    def _sss_decision(self, identifier: str, error: str) -> str:
        return self._request_decision(
            "sss_retry", "下单失败",
            f"订单 {identifier} 创建失败：\n{error}", RETRY_CHOICES)

    def _request_captcha(self, image_bytes: bytes) -> str:
        import base64

        image_b64 = base64.b64encode(image_bytes).decode("ascii")
        captcha_id, entry = self._register_interaction(
            "captcha", request={"image": image_b64})
        self._emit_event("captcha", {"id": captcha_id, "image": image_b64})
        self._notify_android_interaction("captcha")
        code = self._wait_interaction(captcha_id, entry, default="")
        if not str(code).strip():
            raise _InteractionCancelled("验证码输入已取消或超时")
        return str(code)

    def _sss_captcha(self, image_bytes: bytes) -> str:
        return self._request_captcha(image_bytes)

    def resolve_captcha(self, captcha_id: str, code: str) -> dict[str, Any]:
        """把用户输入的验证码交回等待中的任务线程；同一 id 只能兑现一次，未知 id 返回非成功。

        验证码内容不会出现在返回值/日志/只读恢复接口中。
        """
        return self._resolve_interaction(
            captcha_id, str(code), kinds=frozenset({"captcha"}),
            clear_notification=True)

    def _request_address_input(self, items: list[dict[str, Any]]) -> dict[str, str]:
        """向 UI 发起待确认地址填写，阻塞等待返回 {原始地址: 最终地址}。"""
        request_id, entry = self._register_interaction(
            "address_input", request={
                "title": "地址待确认",
                "message": ("以下地址无法自动识别，请输入要写入表格的最终地址；"
                            "留空则保持原待确认流程。"),
                "items": copy.deepcopy(items),
            })
        self._emit_event("address_input", {
            "id": request_id,
            "title": "地址待确认",
            "message": "以下地址无法自动识别，请输入要写入表格的最终地址；留空则保持原待确认流程。",
            "items": items,
        })
        self._notify_android_interaction("address")
        result = self._wait_interaction(request_id, entry,
                                        default=self._interaction_default("address_input"))
        values: Any = result
        if isinstance(result, str):
            try:
                values = json.loads(result)
            except (TypeError, ValueError):
                values = {}
        if not isinstance(values, dict):
            return {}
        cleaned: dict[str, str] = {}
        for key, value in values.items():
            text = str(value or "").strip()
            if text:
                cleaned[str(key)] = text
        return cleaned

    def resolve_address_input(self, input_id: str, entries: Any) -> dict[str, Any]:
        """前端提交待确认地址输入；entries 为 {原始地址: 最终地址} 或 JSON 字符串。

        同一 id 只能兑现一次；结果不在只读恢复接口中回显。
        """
        return self._resolve_interaction(
            input_id, entries, kinds=frozenset({"address_input"}),
            clear_notification=True)

    def _pending_address_input(self, items: list[dict[str, Any]]) -> dict[str, str]:
        return self._request_address_input(items)

    def _save_decision(self, error: str) -> str:
        return self._request_decision(
            "save_retry", "Excel 文件正在使用",
            "保存失败，Excel 文件可能正在被打开或占用。\n请关闭 Excel 文件后点击“重试保存”。\n\n" + error,
            [{"value": "retry", "label": "重试保存", "style": "primary"},
             {"value": "cancel", "label": "取消", "style": "neutral"}])

    # ------------------------------------------------------------------
    # js_api：文件对话框与模板
    # ------------------------------------------------------------------
    def choose_excel(self, mode: str = "order", path: str = "") -> dict[str, Any]:
        """校验服务端文件浏览器选中的 Excel 路径。

        网页版没有原生文件对话框：``path`` 必填，由前端调用
        ``GET /api/fs/list`` 浏览服务器文件系统后回填。返回
        ``{"path": ..., "error": ...}``，``error`` 由 :func:`_excel_field_error`
        给出（空路径 / 文件不存在 / 后缀不是 ``.xlsx``/``.xlsm``）。
        """
        path = str(path or "").strip()
        if not path:
            return {"path": "", "error": "请通过服务器端文件浏览器选择 Excel 文件"}
        error = _excel_field_error(path)
        return {"path": path, "error": error}

    def new_template(self, mode: str = "order", path: str = "") -> dict[str, Any]:
        """在服务端目录生成空白模板（``mode="order"`` 排单表，否则闪时送表）。

        ``path`` 必填，由前端服务器端文件浏览器选好落盘位置。目标文件已存在时，
        用 ``O_CREAT|O_EXCL`` 原子占位拒绝覆盖；写失败会清理占位文件，保证不会把
        用户已有模板覆盖成半成品。
        """
        conflict = self._reject_if_operation_active("生成模板")
        if conflict is not None:
            conflict["path"] = ""
            conflict["error"] = conflict.get("message") or "已有操作进行中"
            return conflict
        path = str(path or "").strip()
        if not path:
            return {"path": "", "error": "请通过服务器端文件浏览器选择保存位置"}
        dest = _with_excel_suffix(_Path(path))
        try:
            # 原子占位：外部已有一个同名文件（含并发另一个 new_template）时拒绝覆盖。
            fd = os.open(str(dest), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            os.close(fd)
        except FileExistsError:
            return {
                "ok": False,
                "status": "rejected",
                "reason": "template_exists",
                "next_action": "另选一个文件名",
                "summary": {"path": str(dest)},
                "operation_id": "",
                "path": "",
                "error": "目标文件已存在，拒绝覆盖；请另选文件名",
            }
        except OSError as exc:
            return {
                "ok": False,
                "status": "failed",
                "reason": "template_create_failed",
                "next_action": "检查目录权限后重试",
                "summary": {"path": str(dest), "error": str(exc)},
                "operation_id": "",
                "path": "",
                "error": f"无法创建模板文件：\n{exc}",
            }
        try:
            if mode == "order":
                write_order_template(dest)
            else:
                write_sss_template(dest)
        except Exception as exc:
            try:
                dest.unlink(missing_ok=True)
            except OSError:
                pass
            return {"path": "", "error": f"无法写入模板文件：\n{exc}"}
        self.log(f"已生成{'排单' if mode == 'order' else '闪时送'}模板：{dest}")
        return {"path": str(dest), "error": ""}

    # ------------------------------------------------------------------
    # js_api：浏览器检查 / 凭据 / 更新
    # ------------------------------------------------------------------
    # ------------------------------------------------------------------
    # WPS 云文档同步
    # ------------------------------------------------------------------

    def _wps_cli(self) -> KdocsCli:
        return KdocsCli(self._config.wps_cli_path or None)

    def _wps_effective_tables(self) -> dict[str, dict[str, str]]:
        """返回实际写入目标，并拒绝过期或越权目标（实现在 wps_cloud.effective_tables）。"""
        return effective_tables(self._config)

    def save_wps_config(self, payload: dict[str, Any]) -> dict[str, Any]:
        """保存云文档同步的配置（沿用 AppConfig 的原子保存）。"""
        conflict = self._reject_if_operation_active("保存云同步配置")
        if conflict is not None:
            return conflict
        cfg = self._config
        if "enabled" in payload:
            was_enabled = bool(cfg.wps_enabled)
            cfg.wps_enabled = bool(payload.get("enabled"))
            if was_enabled and not cfg.wps_enabled:
                # W5：关闭云同步必须**立刻**作废所有未使用令牌，不能等下一次上传
                # 才懒作废 —— 否则客户端在关闭窗口内不调用上传，重新开启后旧
                # preview_id 仍能真的写云端。
                self._previews.invalidate_all("preview_invalidated")
        if "test_mode" in payload:
            cfg.wps_test_mode = bool(payload.get("test_mode"))
        if "cli_path" in payload:
            cfg.wps_cli_path = str(payload.get("cli_path") or "").strip()
        if "drive_id" in payload:
            cfg.wps_drive_id = str(payload.get("drive_id") or "").strip()
        if "test_file_id" in payload:
            cfg.wps_test_file_id = str(payload.get("test_file_id") or "").strip()
        if "test_drive_id" in payload:
            cfg.wps_test_drive_id = str(payload.get("test_drive_id") or "").strip()
        if "test_tables" in payload:
            from app.core.config import normalize_wps_test_tables
            cfg.wps_test_tables = normalize_wps_test_tables(payload.get("test_tables"))
        if "marker_enabled" in payload:
            cfg.wps_marker_enabled = bool(payload.get("marker_enabled"))
        if "sort_enabled" in payload:
            cfg.wps_sort_enabled = bool(payload.get("sort_enabled"))
        if "address_order" in payload:
            from app.core.config import normalize_wps_address_order
            # 以**当前配置**为底：界面只回传部分子表时，没提到的子表保持原样。
            # （原实现以出厂默认为底，会把用户自定义的表 ID/地址顺序静默重置。）
            cfg.wps_address_order = normalize_wps_address_order(
                payload.get("address_order"), base=cfg.wps_address_order)
        if "tables" in payload:
            from app.core.config import normalize_wps_tables
            cfg.wps_tables = normalize_wps_tables(
                payload.get("tables"), base=cfg.wps_tables)
        try:
            cfg.save()
        except OSError:
            return {"ok": False, "reason": "write_failed"}
        return {"ok": True}

    def restore_wps_production_tables(self) -> dict[str, Any]:
        """把写入目标切回正式排单表（配置里的备份 ID）。"""
        conflict = self._reject_if_operation_active("切换正式排单表")
        if conflict is not None:
            return conflict
        backup = self._config.wps_production_tables or {}
        if not backup:
            return {"ok": False, "reason": "没有保存正式表备份"}
        from app.core.config import normalize_wps_tables
        self._config.wps_tables = normalize_wps_tables(backup, base=backup)
        try:
            self._config.save()
        except OSError:
            return {"ok": False, "reason": "写入配置失败"}
        self.log("[云同步] 写入目标已切回正式排单表", "WARN")
        return {"ok": True, "tables": {k: v.get("file_id", "")
                                       for k, v in self._config.wps_tables.items()}}

    def wps_status(self) -> dict[str, Any]:
        """返回云同步的当前状态（不联网、不写任何东西）。"""
        cfg = self._config
        status: dict[str, Any] = {
            "ok": True,
            "enabled": bool(cfg.wps_enabled),
            "test_mode": bool(cfg.wps_test_mode),
            "cli_path": "",
            "cli_found": False,
            "authenticated": False,
            "target_date": "",
            "weekday_number": 0,
            "excel_path": str(cfg.excel_path) if cfg.excel_path else "",
            "tables": [],
            "marker_enabled": bool(cfg.wps_marker_enabled),
            "sort_enabled": bool(cfg.wps_sort_enabled),
            "address_order": {sheet: list(order)
                              for sheet, order in (cfg.wps_address_order or {}).items()},
            # 出厂默认顺序（界面「恢复默认」用；避免前后端各写一份）
            "address_order_defaults": {
                sheet: list(order)
                for sheet, order in default_wps_address_order().items()},
        }
        try:
            cli = self._wps_cli()
            status["cli_path"] = cli.path
            status["cli_found"] = True
            status["authenticated"] = cli.authenticated()
        except WpsCloudError as exc:
            status["reason"] = str(exc)

        target = target_date_for(start_hour=cfg.wps_target_hour_start,
                                 end_hour=cfg.wps_target_hour_end)
        from app.wps.sync import weekday_number
        status["target_date"] = target.isoformat()
        # 通讯记号写的是**运行日**的周几（实测目标表：周四晚跑记 5、周五晚跑记 6）
        status["weekday_number"] = weekday_number(_dt.date.today())

        ledger = None
        try:
            ledger = SyncLedger()
        except WpsCloudError as exc:
            # 损坏/不可读账本必须失败关闭；状态入口也不能 500。
            status["ok"] = False
            status["reason"] = str(exc)
        try:
            effective = self._wps_effective_tables()
        except WpsCloudError as exc:
            status["reason"] = str(exc)
            effective = {}
        for sheet, conf in cfg.wps_tables.items():
            file_id = effective.get(sheet, {}).get("file_id", "")
            entry = {
                "sheet": sheet,
                "file_id": conf.get("file_id", ""),
                "effective_file_id": file_id,
                "last_sync": "",
                "last_people": 0,
            }
            summary = (ledger.batch_summary(target.isoformat(), file_id)
                       if ledger is not None else None)
            if summary:
                entry["last_sync"] = summary["synced_at"]
                entry["last_people"] = summary["people"]
            status["tables"].append(entry)
        for entry, conf in zip(status["tables"], cfg.wps_tables.values()):
            entry["file_id"] = conf.get("file_id", "")
        status["test_file_id"] = cfg.wps_test_file_id
        status["test_tables"] = dict(cfg.wps_test_tables)
        production_ids = {v.get("file_id", "") for v in (cfg.wps_production_tables or {}).values()}
        # 用**实际生效**的目标判断，而不是 cfg.wps_tables ——
        # 测试模式下生效目标是 cfg.wps_test_tables（副本）；拿 wps_tables
        # （可能是正式表 ID）去比，会把"正在写副本"误报成"正在写正式表"。
        try:
            effective_ids = {c.get("file_id", "")
                             for c in self._wps_effective_tables().values()}
        except WpsCloudError:
            effective_ids = {c.get("file_id", "") for c in cfg.wps_tables.values()}
        status["writing_test_copies"] = bool(effective_ids) and not (effective_ids & production_ids)
        status["effective_targets"] = sorted(fid for fid in effective_ids if fid)
        status["production_tables"] = {k: v.get("file_id", "")
                                       for k, v in (cfg.wps_production_tables or {}).items()}
        status["state_path"] = str(ledger.path) if ledger is not None else ""
        return status

    # ------------------------------------------------------------------
    # WPS 只读恢复：Bridge 侧白名单 DTO，不透明透传 B 的字段
    # ------------------------------------------------------------------
    @staticmethod
    def _wps_recovery_status_value(value: Any) -> str:
        raw = "failed" if str(value or "") == "failed_no_write" else str(value or "")
        allowed = {"planned", "writing", "ledger_pending", "uncertain",
                   "verified", "failed", "not_started", "retired_guarded"}
        return raw if raw in allowed else "uncertain"

    @staticmethod
    def _wps_recovery_counts(value: Any) -> dict[str, int]:
        keys = ("planned", "writing", "ledger_pending", "uncertain",
                "verified", "failed", "not_started", "retired_guarded")
        counts = {key: 0 for key in keys}
        if isinstance(value, dict):
            for key in keys:
                try:
                    counts[key] = max(0, min(1_000_000, int(value.get(key) or 0)))
                except (TypeError, ValueError):
                    counts[key] = 0
        return counts

    @staticmethod
    def _wps_recovery_error_code(value: Any, *, ok: bool) -> str:
        text = str(value or "").strip().lower()
        if re.fullmatch(r"wps_recovery_[a-z_]{1,48}", text):
            return text
        return "" if ok else "wps_recovery_error"

    @staticmethod
    def _wps_recovery_next_action(value: Any, *, ok: bool,
                                  counts: dict[str, int]) -> str:
        text = str(value or "").strip().lower()
        allowed = {"manual_reconcile", "recover_journal", "repreview",
                   "fix_journal", "none", "wait_for_recovery_lock"}
        if text in allowed:
            return text
        if not ok:
            return "fix_journal"
        if counts.get("uncertain", 0) or counts.get("retired_guarded", 0):
            return "manual_reconcile"
        if any(counts.get(key, 0) for key in ("writing", "ledger_pending", "planned")):
            return "recover_journal"
        return "none"

    @staticmethod
    def _wps_recovery_guidance(next_action: str, error_code: str = "") -> str:
        if error_code and error_code != "wps_recovery_ledger_missing":
            return "恢复状态不可用：请联系管理员只读核对，不要直接重试或重新上传"
        guidance = {
            "manual_reconcile": "存在不确定结果：请先只读核对云端与日志，不要直接重传",
            "recover_journal": "有待恢复操作：请联系管理员执行恢复流程，不要直接重试或重新上传",
            "repreview": "历史操作未执行：请重新预览并生成新的上传令牌",
            "fix_journal": "本地恢复日志不可用：请联系管理员修复，不要直接重试或重新上传",
            "wait_for_recovery_lock": "另一个恢复/上传操作正在进行：请稍后再查询，不要重试",
            "none": "没有待恢复的操作",
        }
        return guidance.get(next_action, "恢复状态未知：请联系管理员只读核对，不要直接重试")

    @classmethod
    def _wps_recovery_safe_sheet(cls, sheet: Any) -> dict[str, Any] | None:
        if not isinstance(sheet, dict):
            return None
        raw_status = str(sheet.get("raw_status") or sheet.get("status") or "uncertain")
        if raw_status not in {"planned", "writing", "ledger_pending", "uncertain",
                              "verified", "failed_no_write", "not_started",
                              "retired_guarded"}:
            raw_status = "uncertain"
        status = "failed" if raw_status == "failed_no_write" else raw_status
        if status not in {"planned", "writing", "ledger_pending", "uncertain",
                          "verified", "failed", "not_started", "retired_guarded"}:
            status = "uncertain"
        target_ref = str(sheet.get("target_ref") or "")
        if not re.fullmatch(r"wps-target:[0-9a-f]{12}", target_ref):
            target_ref = ""
        target_date = str(sheet.get("target_date") or "")
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", target_date):
            target_date = ""
        evidence = str(sheet.get("evidence") or "local_journal")
        if evidence not in {"local_journal", "journal+cloud_read",
                            "resolve+cloud_read", "executor_cloud_readback"}:
            evidence = "local_journal"
        action_map = {
            "uncertain": ["manual_reconcile"],
            "retired_guarded": ["manual_reconcile"],
            "writing": ["recover_journal"],
            "ledger_pending": ["recover_journal"],
            "planned": ["recover_journal"],
            "failed": ["repreview"],
            "not_started": ["repreview"],
            "verified": [],
        }
        code_map = {
            "uncertain": "wps_recovery_uncertain",
            "retired_guarded": "wps_recovery_retired_guarded",
            "writing": "wps_recovery_writing",
            "ledger_pending": "wps_recovery_ledger_pending",
            "planned": "wps_recovery_planned",
            "failed": "wps_recovery_failed",
            "not_started": "wps_recovery_not_started",
            "verified": "wps_recovery_verified",
        }
        return {
            "target_date": target_date,
            "target_ref": target_ref,
            "status": status,
            "raw_status": raw_status,
            "error_code": code_map.get(status, "wps_recovery_uncertain"),
            "allowed_next_actions": list(action_map.get(status, ["manual_reconcile"])),
            "manual_required": bool(sheet.get("manual_required", False)),
            "cloud_checked": bool(sheet.get("cloud_checked", False)),
            "evidence": evidence,
        }

    @classmethod
    def _wps_recovery_safe_operation(cls, operation: Any) -> dict[str, Any] | None:
        if not isinstance(operation, dict):
            return None
        raw_sheets = operation.get("sheets")
        sheets = []
        if isinstance(raw_sheets, list):
            sheets = [item for item in (
                cls._wps_recovery_safe_sheet(sheet) for sheet in raw_sheets)
                if item is not None]
        statuses = {sheet["status"] for sheet in sheets}
        order = {"uncertain": 0, "retired_guarded": 1, "writing": 2,
                 "ledger_pending": 3, "planned": 4, "failed": 5,
                 "not_started": 6, "verified": 7}
        if statuses:
            status = sorted(statuses, key=lambda item: order.get(item, 99))[0]
        else:
            status = str(operation.get("status") or "uncertain")
            if status not in order:
                status = "uncertain"
        operation_id = str(operation.get("operation_id") or "")
        if not re.fullmatch(r"wps-[0-9a-f]{16}", operation_id):
            operation_id = ""
        operation_ref = str(operation.get("operation_ref") or "")
        if not re.fullmatch(r"wps-op:[0-9a-f]{12}", operation_ref):
            operation_ref = ""
        target_refs: list[str] = []
        for sheet in sheets:
            ref = sheet.get("target_ref") or ""
            if ref and ref not in target_refs:
                target_refs.append(ref)
        target_refs.sort()
        target_date = str(operation.get("target_date") or "")
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", target_date):
            target_date = ""
        if not target_date:
            for sheet in sheets:
                if sheet.get("target_date"):
                    target_date = str(sheet["target_date"])
                    break
        created_at = str(operation.get("created_at") or "")
        if not re.match(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}", created_at):
            created_at = ""
        updated_at = str(operation.get("updated_at") or "")
        if not re.match(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}", updated_at):
            updated_at = ""
        action_map = {
            "uncertain": ["manual_reconcile"],
            "retired_guarded": ["manual_reconcile"],
            "writing": ["recover_journal"],
            "ledger_pending": ["recover_journal"],
            "planned": ["recover_journal"],
            "failed": ["repreview"],
            "not_started": ["repreview"],
            "verified": [],
        }
        code_map = {
            "uncertain": "wps_recovery_uncertain",
            "retired_guarded": "wps_recovery_retired_guarded",
            "writing": "wps_recovery_writing",
            "ledger_pending": "wps_recovery_ledger_pending",
            "planned": "wps_recovery_planned",
            "failed": "wps_recovery_failed",
            "not_started": "wps_recovery_not_started",
            "verified": "wps_recovery_verified",
        }
        return {
            "operation_id": operation_id,
            "operation_ref": operation_ref,
            "status": status,
            "pending": bool(operation.get("pending", False)),
            "cloud_checked": bool(operation.get("cloud_checked", False)),
            "created_at": created_at,
            "updated_at": updated_at,
            "target_date": target_date,
            "target_refs": target_refs,
            "sheet_count": len(sheets),
            "error_code": code_map.get(status, "wps_recovery_uncertain"),
            "allowed_next_actions": list(action_map.get(status, ["manual_reconcile"])),
            "manual_required": any(sheet["manual_required"] for sheet in sheets),
            "sheets": sheets,
        }

    @classmethod
    def _wps_recovery_summary(cls, operations: list[dict[str, Any]],
                              counts: dict[str, int], next_action: str,
                              error_code: str = "") -> dict[str, Any]:
        pending_count = sum(1 for op in operations if op.get("pending"))
        return {
            "operation_count": len(operations),
            "pending_count": pending_count,
            "uncertain_count": int(counts.get("uncertain") or 0),
            "failed_count": int(counts.get("failed") or 0),
            "not_started_count": int(counts.get("not_started") or 0),
            "retired_guarded_count": int(counts.get("retired_guarded") or 0),
            "has_pending": bool(pending_count),
            "needs_review": bool(counts.get("uncertain") or counts.get("failed")
                                 or counts.get("not_started")
                                 or counts.get("retired_guarded")
                                 or pending_count
                                 or error_code),
            "guidance": cls._wps_recovery_guidance(next_action, error_code),
        }

    @classmethod
    def _wps_recovery_failure(cls, error_code: str, next_action: str,
                              *, viewer_admin: bool) -> dict[str, Any]:
        counts = cls._wps_recovery_counts({})
        code = cls._wps_recovery_error_code(error_code, ok=False)
        next_action = cls._wps_recovery_next_action(
            next_action, ok=False, counts=counts)
        base: dict[str, Any] = {
            "ok": False,
            "contract_version": 1,
            "source": "local_journal",
            "read_only": True,
            "queried_cloud": False,
            "contains_cloud_checked_records": False,
            "operations": [],
            "pending_operations": [],
            "counts": counts,
            "next_action": next_action,
            "error_code": code,
            "scope": "admin" if viewer_admin else "summary",
        }
        base["summary"] = cls._wps_recovery_summary(
            [], counts, next_action, code)
        if not viewer_admin:
            base.pop("operations", None)
            base.pop("pending_operations", None)
        return base

    @classmethod
    def _sanitize_wps_recovery(cls, raw: Any, *, viewer_admin: bool) -> dict[str, Any]:
        """把 B 的安全 DTO/任意底层返回重新做 Bridge 字段白名单。

        未知字段直接丢弃；异常/损坏/未知状态只返回稳定 error_code 与固定指引。
        """
        if not isinstance(raw, dict):
            return cls._wps_recovery_failure(
                "wps_recovery_contract_invalid", "fix_journal",
                viewer_admin=viewer_admin)
        if not raw.get("ok"):
            code = cls._wps_recovery_error_code(
                raw.get("error_code") or raw.get("reason"), ok=False)
            counts = cls._wps_recovery_counts(raw.get("counts"))
            next_action = cls._wps_recovery_next_action(
                raw.get("next_action"), ok=False, counts=counts)
            return cls._wps_recovery_failure(code, next_action,
                                             viewer_admin=viewer_admin)
        raw_operations = raw.get("operations")
        raw_operations = raw_operations if isinstance(raw_operations, list) else []
        operations = [item for item in (
            cls._wps_recovery_safe_operation(op) for op in raw_operations)
            if item is not None]
        counts = cls._wps_recovery_counts(raw.get("counts"))
        if operations:
            counts = {key: 0 for key in counts}
            for op in operations:
                for sheet in op.get("sheets") or []:
                    status = sheet.get("status") or "uncertain"
                    counts[status] = counts.get(status, 0) + 1
        pending_operations = [op for op in operations if op.get("pending")]
        next_action = cls._wps_recovery_next_action(
            raw.get("next_action"), ok=True, counts=counts)
        error_code = cls._wps_recovery_error_code(raw.get("error_code"), ok=True)
        result: dict[str, Any] = {
            "ok": True,
            "contract_version": 1,
            "source": "local_journal",
            "read_only": True,
            "queried_cloud": False,
            "contains_cloud_checked_records": bool(
                raw.get("contains_cloud_checked_records", False)),
            "counts": counts,
            "next_action": next_action,
            "error_code": error_code,
            "scope": "admin" if viewer_admin else "summary",
        }
        result["summary"] = cls._wps_recovery_summary(
            operations, counts, next_action, error_code)
        if viewer_admin:
            result["operations"] = operations
            result["pending_operations"] = pending_operations
        return result

    def _wps_recovery_snapshot(self) -> tuple[Any, dict[str, Any] | None, str]:
        """只读取出 (ledger, 原始恢复 DTO, error_code)；失败时 ledger 可能为 None。"""
        try:
            ledger = SyncLedger()
        except WpsCloudError as exc:
            code = ("wps_recovery_ledger_unreadable"
                    if type(exc).__name__ == "LedgerCorruptError"
                    else "wps_recovery_journal_unreadable")
            return None, None, code
        except Exception:  # noqa: BLE001 - 只读入口错误也必须脱敏
            return None, None, "wps_recovery_internal_error"
        try:
            raw = _wps_recovery_status_contract(ledger)
        except WpsCloudError as exc:
            code = ("wps_recovery_journal_unreadable"
                    if type(exc).__name__ in ("JournalError", "JournalCorruptError")
                    else "wps_recovery_ledger_unreadable")
            return ledger, None, code
        except Exception:  # noqa: BLE001
            return ledger, None, "wps_recovery_internal_error"
        return ledger, raw, ""

    def wps_recovery_status(self) -> dict[str, Any]:
        """只读 WPS 恢复查询：普通用户仅安全摘要，管理员仅最小白名单 DTO。

        不返回原始 journal、problems/risk_reason、客户姓名/电话/地址/餐次、
        sheet/file_id、异常原文或未来新增字段；不写云端/日志/账本。
        """
        viewer_admin = bool(self.is_admin)
        _ledger, raw, error_code = self._wps_recovery_snapshot()
        if error_code:
            return self._wps_recovery_failure(
                error_code, "fix_journal", viewer_admin=viewer_admin)
        assert raw is not None
        return self._sanitize_wps_recovery(raw, viewer_admin=viewer_admin)

    # ------------------------------------------------------------------
    # W3：管理员专用恢复/退场入口
    #
    # 普通用户既看不到也调不动（HTTP 层白名单默认拒绝 + 方法内再次校验角色）。
    # 语义边界：
    # * ``retire_guarded`` 只把不可判定的旧任务**带审计地退出 pending**，
    #   同一 target_date + 云表仍保留防重复闸门 —— 未知写入结果**不会**因此
    #   变成“可以自动重传”；同一日期再次上传会被 apply_plan 拒绝为 uncertain。
    # * ``cloud_verified`` / ``cloud_untouched`` 必须重新只读云端并实际证明
    #   verified / not_started 才能清除闸门；证明不了就保持阻断（B 的
    #   ``cloud_verify_failed`` / ``cloud_not_untouched``）。
    # * 本入口**永不写云端**（cloud_write 恒为 False），只写本地 journal 审计。
    # ------------------------------------------------------------------
    #: 允许的管理员决策（别名在入口处归一）。
    _WPS_RESOLVE_DECISIONS = ("retire_guarded", "cloud_verified",
                              "cloud_untouched", "keep")
    #: ``retire_guarded`` 的等价别名（B 也接受，这里显式列出以便提示）。
    _WPS_RETIRE_ALIASES = frozenset({
        "retire", "retire_manual", "retire_old_operation", "guarded_retire",
        "abandon", "abandon_guarded", "manual_retire", "audited_retire",
    })
    #: 审计备注最短长度：强制管理员写下“为什么可以退出”。
    _WPS_RESOLVE_MIN_NOTE = 4

    def _wps_resolve_failure(self, code: str, reason: str, next_action: str, *,
                             status: str = "rejected",
                             operation_ref: str = "",
                             extra: dict[str, Any] | None = None) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "ok": False,
            "status": status,
            "code": str(code),
            "reason": str(reason or code),
            "next_action": str(next_action or ""),
            "contract_version": 1,
            "read_only": False,
            "cloud_write": False,
            "operation_ref": str(operation_ref or ""),
            "changed": False,
            "scope": {},
            "audit": {},
        }
        if extra:
            payload.update(extra)
        return payload

    def wps_recovery_resolve(self, payload: dict[str, Any] | None = None,
                             **options: Any) -> dict[str, Any]:
        """管理员专用恢复入口：带确认、范围与审计地处理 pending 旧任务。

        请求（HTTP ``POST /api/wps_recovery_resolve``，JSON 数组单对象或对象键值均可）::

            {"operation_id": "wps-<16hex>",
             "decision": "retire_guarded" | "cloud_verified"
                         | "cloud_untouched" | "keep",
             "confirm": "<与 decision 完全相同的字符串>",
             "note": "人工核对说明（至少 4 个字符，写进审计）",
             "confirm_structure_checked": true}   # 仅 retire_guarded 需要

        权限：仅管理员。普通用户 HTTP 403 ``admin_only``；即使绕过 HTTP 直接调用
        也会得到 ``ok=false, status=forbidden``。重复请求幂等：同一 operation 已经
        ``retired_guarded`` 时返回 ``already_retired`` 且不写任何东西。
        """
        data: dict[str, Any] = dict(payload) if isinstance(payload, dict) else {}
        if isinstance(payload, str) and payload.strip():
            data.setdefault("operation_id", payload.strip())
        for key, value in options.items():
            if value is not None:
                data[key] = value
        actor = self._request_identity() or "local-admin"
        operation_id = str(data.get("operation_id") or "").strip()
        raw_decision = str(data.get("decision") or "").strip().lower()
        confirm = str(data.get("confirm") or "").strip()
        note = str(data.get("note") or "").strip()
        structure_checked = bool(data.get("confirm_structure_checked", False))
        decision = ("retire_guarded" if raw_decision in self._WPS_RETIRE_ALIASES
                    else raw_decision)

        if not self.is_admin:
            # 双保险：HTTP 白名单已经 403；直接调用（脚本/旧客户端）也必须拒绝。
            return self._wps_resolve_failure(
                "forbidden", "恢复/退场仅管理员可用", "联系管理员处理",
                status="forbidden")
        if not re.fullmatch(r"wps-[0-9a-f]{16}", operation_id):
            return self._wps_resolve_failure(
                "invalid_operation_id",
                "operation_id 必须是内部生成的 wps-<16位小写hex>",
                "先用 wps_recovery_status 取出 operation_ref/operation_id",
                status="invalid", extra={"allowed_decisions": list(
                    self._WPS_RESOLVE_DECISIONS)})
        if decision not in self._WPS_RESOLVE_DECISIONS:
            return self._wps_resolve_failure(
                "decision_not_allowed",
                "只允许 retire_guarded / cloud_verified / cloud_untouched / keep",
                "选择允许的 decision 后重试", status="invalid",
                extra={"allowed_decisions": list(self._WPS_RESOLVE_DECISIONS)})
        if len(note) < self._WPS_RESOLVE_MIN_NOTE:
            return self._wps_resolve_failure(
                "note_required", "必须填写至少 4 个字符的人工核对说明（写入审计）",
                "补充 note 后重试")
        if confirm != decision:
            return self._wps_resolve_failure(
                "confirmation_required",
                f"必须显式确认：confirm 必须与 decision 完全相同（{decision}）",
                f"重新提交并令 confirm={decision}")
        if decision == "retire_guarded" and not structure_checked:
            return self._wps_resolve_failure(
                "structure_confirmation_required",
                "退场不会清除防重复闸门，但必须确认已人工核对过云端表结构",
                "确认已核对后传 confirm_structure_checked=true")

        reservation = self._operations.try_reserve(
            "wps_recovery_resolve",
            summary={"title": "恢复/退场旧任务", "decision": decision},
            next_action="等待恢复操作结束或查询 operation_status")
        if not reservation.granted:
            conflict = self._operation_conflict_payload(
                reservation.conflict, action="恢复/退场旧任务")
            return self._wps_resolve_failure(
                "operation_conflict", str(conflict.get("reason") or "busy"),
                str(conflict.get("next_action") or "等待当前操作结束后重试"),
                status="rejected",
                extra={"operation_id": str(conflict.get("operation_id") or ""),
                       "changed": False})
        operation = reservation.operation
        assert operation is not None
        # 预置结果：即使实现里出现未预期异常，finally 也不会因未绑定变量而掩盖异常，
        # 互斥槽位也一定会被释放（result 记录“已中止”）。
        result: dict[str, Any] = self._wps_resolve_failure(
            "internal_error", "恢复动作未完成（异常中止）", "查看日志后重试",
            status="error", extra={"operation_id": operation_id})
        try:
            result = self._wps_recovery_resolve_impl(
                operation_id=operation_id, decision=decision, note=note,
                confirm_structure_checked=structure_checked, actor=actor)
        finally:
            if self._operations.is_active(operation):
                self._operations.finish(
                    operation,
                    status="success" if result.get("ok") else "error",
                    reason=str(result.get("code") or result.get("reason") or "")[:200],
                    summary={"title": "恢复/退场旧任务", "decision": decision,
                             "changed": bool(result.get("changed"))},
                    next_action=str(result.get("next_action") or "")[:200])
        return result

    def _wps_recovery_resolve_impl(self, *, operation_id: str, decision: str,
                                   note: str, confirm_structure_checked: bool,
                                   actor: str) -> dict[str, Any]:
        """实际执行一次恢复/退场：只读快照 → B 的入口 → 复核落盘 → 脱敏返回。"""
        ledger, raw, error_code = self._wps_recovery_snapshot()
        if error_code or ledger is None or raw is None:
            return self._wps_resolve_failure(
                error_code or "wps_recovery_internal_error",
                "本地账本/意图日志不可用，拒绝任何恢复动作",
                "先修复本地日志/账本", status="blocked",
                extra={"operation_id": operation_id})
        snapshot = self._sanitize_wps_recovery(raw, viewer_admin=True)
        current = next((op for op in (snapshot.get("operations") or [])
                        if op.get("operation_id") == operation_id), None)
        if current is None:
            return self._wps_resolve_failure(
                "not_found", "找不到该 operation_id（可能已被归档或从未存在）",
                "用 wps_recovery_status 重新查询", status="not_found",
                extra={"operation_id": operation_id})
        operation_ref = str(current.get("operation_ref") or "")
        scope_base = {
            "operation_ref": operation_ref,
            "target_dates": sorted({d for d in [str(current.get("target_date") or "")]
                                    if d}),
            "target_refs": list(current.get("target_refs") or []),
            "sheet_count": self._as_int(current.get("sheet_count")),
        }
        status_now = str(current.get("status") or "uncertain")
        if decision == "retire_guarded" and status_now == "retired_guarded":
            # 重复请求保护：已经退场过就什么都不写，明确告知“未改变”。
            return {
                "ok": True, "status": "already_retired", "code": "already_retired",
                "reason": "该操作已带闸门退场，本次未重复写入",
                "next_action": "manual_reconcile",
                "contract_version": 1, "read_only": False, "cloud_write": False,
                "operation_id": operation_id, "operation_ref": operation_ref,
                "changed": False, "verified_on_disk": True,
                "scope": {**scope_base, "guard_retained": True,
                          "blocking": "retired_guarded"},
                "audit": {"actor": actor,
                          "at": _dt.datetime.now().isoformat(timespec="seconds"),
                          "decision": decision, "note_recorded": False,
                          "duplicate": True,
                          "effects": {"cloud_written": False, "guard_retained": True,
                                      "auto_retry_allowed": False}},
                "recovery": self._wps_resolve_recovery_view(snapshot),
            }
        if decision == "keep" and status_now == "verified":
            return self._wps_resolve_failure(
                "not_pending", "该操作已确认完成，无需再记录核对备注",
                "无需操作", status="not_pending", operation_ref=operation_ref,
                extra={"operation_id": operation_id,
                       "scope": {**scope_base, "guard_retained": False,
                                "blocking": "none"}})

        try:
            # 只有需要重新只读核对云端的决策才构造 CLI；``retire_guarded``/``keep``
            # 绝不碰云端，因此 WPS 未授权、even wps_enabled=False 时也必须可用。
            cli = None
            if decision in ("cloud_verified", "cloud_untouched"):
                try:
                    cli = self._wps_cli()
                except WpsCloudError as exc:
                    return self._wps_resolve_failure(
                        "cli_unavailable", f"无法初始化 kdocs-cli：{exc}",
                        "先在「云文档同步」里完成授权后重试", status="blocked",
                        operation_ref=operation_ref,
                        extra={"operation_id": operation_id, "scope": scope_base})
            resolved = _wps_resolve_pending_operation(
                ledger, operation_id, decision, note=note, cli=cli,
                confirm_structure_checked=confirm_structure_checked)
        except WpsCloudError as exc:
            return self._wps_resolve_failure(
                "local_state_blocked", f"本地账本/意图日志不可用：{exc}",
                "先修复本地日志/账本", status="blocked",
                operation_ref=operation_ref,
                extra={"operation_id": operation_id, "scope": scope_base})
        except Exception as exc:  # noqa: BLE001 - 对外必须是 JSON
            self.log(f"[云同步恢复] 异常：{type(exc).__name__}", "ERROR")
            return self._wps_resolve_failure(
                "internal_error", f"{type(exc).__name__}", "查看日志后重试",
                status="error", operation_ref=operation_ref,
                extra={"operation_id": operation_id, "scope": scope_base})

        raw_status = str(resolved.get("status") or "")
        ok = bool(resolved.get("ok"))
        reason = str(resolved.get("reason") or "")
        results = [item for item in (resolved.get("operations") or [])
                   if isinstance(item, dict)]
        sheet_codes = [str(item.get("reason") or "") for item in results]
        if decision == "cloud_verified" and ok:
            code = "cloud_verified"
        elif decision == "cloud_untouched" and ok:
            code = "cloud_untouched"
        elif decision == "keep":
            # keep 的正常返回是 ok=False + 仍然 uncertain（只留痕、不解除阻断），
            # 但对调用方而言“备注已记录”是成功；只有 journal 落盘失败才算失败。
            keep_failed = bool(
                raw_status == "blocked"
                or reason.startswith(("journal_save_failed", "journal_unreadable")))
            if keep_failed:
                code = ("journal_write_failed" if reason.startswith("journal_save_failed")
                        else "journal_unreadable")
                ok = False
            else:
                code = "keep_recorded"
                ok = True
        elif decision == "retire_guarded" and ok:
            code = "retired_guarded"
        else:
            code = next((candidate for candidate in (
                "cloud_verify_failed", "cloud_not_untouched", "cli_required",
                "ledger_pending", "journal_save_failed", "unsupported_status_"
                "requires_manual") if candidate in sheet_codes), "")
            if not code:
                code = (reason.split(":", 1)[0] if reason else raw_status) \
                    or "resolve_failed"
        next_action = str(resolved.get("next_action") or "")
        # 重新读一遍本地状态，证明效果真的落盘（而不是只看 B 的返回值）。
        _ledger2, raw2, _err2 = self._wps_recovery_snapshot()
        sanitized2: dict[str, Any] | None = (
            self._sanitize_wps_recovery(raw2, viewer_admin=True)
            if isinstance(raw2, dict) else None)
        found2 = next((op for op in ((sanitized2 or {}).get("operations") or [])
                       if op.get("operation_id") == operation_id), None)
        expected_after = {"retire_guarded": "retired_guarded",
                          "cloud_verified": "verified",
                          "cloud_untouched": "not_started",
                          "keep": None}.get(decision)
        after_status = str((found2 or {}).get("status") or "")
        verified_on_disk = bool(
            found2 is not None
            and (expected_after is None or after_status == expected_after))
        if after_status:
            guard_retained = after_status == "retired_guarded"
        else:
            guard_retained = status_now == "retired_guarded"
        after_pending = bool((found2 or current).get("pending"))
        if guard_retained:
            # 同一 target_date + 云表的防重复闸门仍在，自动重传依然被拒。
            blocking = "retired_guarded"
        elif after_pending:
            # 仍是全局 pending：写入被意图日志恢复流程阻断。
            blocking = "pending"
        else:
            blocking = "none"
        audit_effects = {"cloud_written": False, "guard_retained": guard_retained,
                         "blocking": blocking, "auto_retry_allowed": False}
        self.log(
            f"[云同步恢复] 管理员 {actor} 对 {operation_ref or operation_id} "
            f"执行 {decision} → {code or raw_status}（"
            f"{'已落盘' if verified_on_disk else '状态未确认'}；"
            f"防重复闸门{'保留' if guard_retained else '已清除'}）", "WARN")
        view = self._wps_resolve_recovery_view(sanitized2 or snapshot)
        if not ok:
            # 失败/仍需人工：不把结果说成成功，闸门语义保持原样。
            return self._wps_resolve_failure(
                code or "resolve_failed",
                "恢复动作未成功；云端与本地状态未被当作已核对，不能自动重传",
                next_action or "manual_reconcile",
                status=raw_status or "rejected", operation_ref=operation_ref,
                extra={"operation_id": operation_id,
                       "scope": {**scope_base, "guard_retained": guard_retained,
                                 "blocking": blocking},
                       "verified_on_disk": verified_on_disk,
                       "sheet_results": [{"status": str(item.get("status") or ""),
                                          "code": str(item.get("reason") or "")}
                                         for item in results][:20],
                       "recovery": view})
        return {
            "ok": True,
            "status": code or raw_status or "resolved",
            "code": code or raw_status or "resolved",
            "reason": "已按管理员决策处理，本地 journal 已写入审计",
            "next_action": next_action or "manual_reconcile",
            "contract_version": 1,
            "read_only": False,
            "cloud_write": False,
            "operation_id": operation_id,
            "operation_ref": operation_ref,
            "changed": True,
            "verified_on_disk": verified_on_disk,
            "scope": {**scope_base, "guard_retained": guard_retained,
                      "blocking": blocking},
            "audit": {
                "actor": actor,
                "at": _dt.datetime.now().isoformat(timespec="seconds"),
                "decision": decision,
                "note_recorded": True,
                "duplicate": False,
                "effects": audit_effects,
            },
            "recovery": view,
        }

    @staticmethod
    def _wps_resolve_recovery_view(snapshot: dict[str, Any]) -> dict[str, Any]:
        """恢复入口返回的精简只读视图（沿用 R3 脱敏口径，不含客户/路径）。"""
        return {
            "counts": dict(snapshot.get("counts") or {}),
            "next_action": str(snapshot.get("next_action") or ""),
            "error_code": str(snapshot.get("error_code") or ""),
            "summary": dict(snapshot.get("summary") or {}),
        }

    # ------------------------------------------------------------------
    # WPS 预览令牌：只读上下文、计划指纹与结构化结果
    # ------------------------------------------------------------------
    @staticmethod
    def _wps_string_tables(value: Any) -> dict[str, dict[str, str]]:
        """把表映射规整成稳定 JSON 结构，供指纹使用。"""
        result: dict[str, dict[str, str]] = {}
        if isinstance(value, dict):
            for sheet, conf in value.items():
                name = str(sheet or "").strip()
                if not name:
                    continue
                if isinstance(conf, dict):
                    result[name] = {str(key): str(item or "")
                                    for key, item in sorted(conf.items())}
                else:
                    result[name] = {"file_id": str(conf or "")}
        return result

    def _wps_context_snapshot(self, *, local_sha256: str, tables: dict[str, Any],
                              target: _dt.date) -> dict[str, Any]:
        """预览与上传共同确认的配置/日期/目标表输入快照。"""
        cfg = self._config
        return {
            "excel_path": str(cfg.excel_path) if cfg.excel_path else "",
            "local_sha256": str(local_sha256 or ""),
            "target_date": target.isoformat(),
            "run_date": _dt.date.today().isoformat(),
            "test_mode": bool(cfg.wps_test_mode),
            "wps_enabled": bool(cfg.wps_enabled),
            "wps_target_hour_start": int(cfg.wps_target_hour_start),
            "wps_target_hour_end": int(cfg.wps_target_hour_end),
            "marker_enabled": bool(cfg.wps_marker_enabled),
            "sort_enabled": bool(cfg.wps_sort_enabled),
            "address_order": {
                str(sheet): [str(item) for item in (order or [])]
                for sheet, order in (cfg.wps_address_order or {}).items()
            },
            "tables": self._wps_string_tables(tables),
            "wps_tables": self._wps_string_tables(cfg.wps_tables),
            "wps_test_tables": {
                str(sheet): str(file_id)
                for sheet, file_id in (cfg.wps_test_tables or {}).items()
            },
            "wps_production_tables": self._wps_string_tables(cfg.wps_production_tables),
        }

    # ------------------------------------------------------------------
    # W6：计划口径 vs 执行口径
    #
    # 计划数（``planned_summary``）只说明“准备改几行”；执行结果
    # （``execution_summary``）才说明“实际证明改了几行”。两者绝不混用：
    # 拿不到可证明的行数时一律 ``None``（未知），不用计划数或“成功表数”顶替。
    # ------------------------------------------------------------------
    #: ``execution_summary["sheets"]`` 的固定分类（单位是**表**，不是行）。
    _WPS_EXEC_SHEET_KEYS = ("verified", "noop", "failed", "uncertain",
                            "skipped", "blocked", "other")
    #: 逐表回读校验通过时执行器返回的“已写入人数/行数”字段。
    _WPS_EXEC_ROW_FIELD = "people"

    @staticmethod
    def _as_int(value: Any, default: int = 0) -> int:
        """宽松整数：布尔、``None``、非数字文本都回退默认值，绝不抛异常。"""
        if isinstance(value, bool):
            return default
        try:
            return int(value)
        except (TypeError, ValueError):
            return default

    @classmethod
    def _wps_planned_summary(cls, plans: Any) -> dict[str, Any]:
        """计划口径：明确标注 ``kind="plan"``，避免被当成执行结果。"""
        try:
            stats = summarize_plan(plans)
        except Exception:  # noqa: BLE001 - 摘要失败不能影响写入结果
            stats = {}
        planned: dict[str, Any] = {str(key): value
                                   for key, value in (stats or {}).items()}
        planned["kind"] = "plan"
        planned["rows"] = {
            "to_update": cls._as_int(planned.get("to_update")),
            "to_append": cls._as_int(planned.get("to_append")),
            "unchanged": cls._as_int(planned.get("unchanged")),
            "skipped": cls._as_int(planned.get("skipped")),
            "warned": cls._as_int(planned.get("warned")),
        }
        planned["note"] = "计划要改动的行数，不是执行结果；执行结果见 execution_summary.rows"
        return planned

    @classmethod
    def _wps_execution_summary(cls, *, status: str,
                               sheets: list[dict[str, Any]],
                               sheet_statuses: list[str],
                               written: int, failed: int,
                               uncertain: bool | None,
                               malformed: bool, next_action: str,
                               planned_summary: dict[str, Any] | None = None,
                               proven_no_write: bool = False,
                               executed: bool = True,
                               counts_source: str = "") -> dict[str, Any]:
        """执行口径：逐表状态计数 + 可证明/未知的行数。

        统计口径（固定，不随调用方变化）：

        * ``sheets.*`` = **表数**：``verified``（ok/verified）、``noop``、
          ``failed``（failed/stale_batch）、``uncertain``、``skipped``、
          ``blocked``、``other``（未知/畸形状态）。
        * ``rows.verified`` = 逐表 ``people`` 求和；任何一张 ok 表拿不到整数
          计数就整体为 ``None``（未知）。noop 表按 0 计入（执行器证明没有写）。
        * ``rows.failed`` = 0（执行器对 failed/stale_batch 表保证零写入）或
          ``None``（表结构畸形，无法证明）。
        * ``rows.uncertain`` = 0（不存在不确定表）或 ``None``（未知）。
        * ``rows.skipped`` = 0（skipped 表在任何写入之前被跳过）或 ``None``。
        * ``rows.planned`` = 计划要改的行数，单独标注为计划口径。
        * ``rows_unknown`` = 上面任意一个为 ``None``。
        * ``proven_no_write`` = 这次调用是否**已被证明**没有发生任何云端写入
          （例如在取占位/消费令牌之前就拒绝）。
        """
        sheet_counts = {key: 0 for key in cls._WPS_EXEC_SHEET_KEYS}
        item_uncertain = False
        for item, sheet_status in zip(sheets, sheet_statuses):
            if sheet_status in ("ok", "verified"):
                sheet_counts["verified"] += 1
            elif sheet_status == "noop":
                sheet_counts["noop"] += 1
            elif sheet_status in ("failed", "stale_batch"):
                sheet_counts["failed"] += 1
            elif sheet_status == "uncertain":
                sheet_counts["uncertain"] += 1
            elif sheet_status == "skipped":
                sheet_counts["skipped"] += 1
            elif sheet_status == "blocked":
                sheet_counts["blocked"] += 1
            else:
                sheet_counts["other"] += 1
            if bool(item.get("uncertain")) and sheet_status not in ("ok", "verified", "noop"):
                item_uncertain = True
        if item_uncertain:
            sheet_counts["uncertain"] = max(sheet_counts["uncertain"], 1)

        rows_verified: int | None = 0
        rows_verified_known = not malformed
        # 同一张表被重复上报（畸形/拼接返回）会重复计数：一律按未知处理，
        # 宁可说“不知道”，也不能给出被重复累加的数字。
        seen_sheets: list[str] = []
        for item in sheets:
            key = str(item.get("sheet") or "")
            if key in seen_sheets:
                rows_verified_known = False
                break
            seen_sheets.append(key)
        for item, sheet_status in zip(sheets, sheet_statuses):
            if not rows_verified_known:
                break
            if sheet_status not in ("ok", "verified"):
                continue
            people = item.get(cls._WPS_EXEC_ROW_FIELD)
            if isinstance(people, bool) or not isinstance(people, int) or people < 0:
                # 拿不到执行器证明的写入行数（缺失/类型错/负数畸形）：整体未知，
                # 绝不用计划数顶替，也不把 -5 夹成 0 当成“已证明”。
                rows_verified_known = False
                break
            rows_verified += people
        if rows_verified_known and not proven_no_write:
            aggregate = str(status or "").strip().lower()
            if uncertain is not False or sheet_counts["uncertain"] or sheet_counts["other"]:
                # 整轮结果不确定（或存在状态未知的表）时，“已验证行数”无法证明是
                # 完整数字，一律标成未知（None）：绝不给出一个会被读成“只写了这么
                # 多行”的 0 或部分和，更不能用计划数顶替。
                rows_verified_known = False
            elif executed and not sheet_statuses:
                # 执行了却一张表都没回报：无法证明任何行数。
                rows_verified_known = False
            elif aggregate in ("failed", "error", "blocked", "rejected") \
                    and sheet_counts["verified"]:
                # 顶层失败与“逐表已验证”自相矛盾：行数不可信，按未知处理。
                rows_verified_known = False
        if not rows_verified_known:
            rows_verified = None

        rows_failed: int | None = None if malformed else 0
        uncertain_known = bool(
            uncertain is False and not sheet_counts["uncertain"]
            and not sheet_counts["other"] and not malformed)
        rows_uncertain: int | None = 0 if (uncertain_known or proven_no_write) else None
        rows_skipped: int | None = None if malformed else 0
        if not executed and not proven_no_write:
            # 执行过程的异常路径（例如 apply_plan 抛错）：可能已写入一部分，
            # 四个行数全部按“未知”上报，不得给出任何 0 的假象。
            rows_verified = rows_failed = rows_uncertain = rows_skipped = None
        planned_rows = (planned_summary or {}).get("rows") or {}
        rows_planned = (cls._as_int(planned_rows.get("to_update"))
                        + cls._as_int(planned_rows.get("to_append")))
        rows_unknown = any(value is None for value in (
            rows_verified, rows_failed, rows_uncertain, rows_skipped))

        return {
            "kind": "execution",
            "contract_version": 1,
            "status": str(status or ""),
            "executed": bool(executed),
            "counts_source": (str(counts_source)
                              or ("apply_plan" if executed
                                  else "rejected_before_write")),
            "sheets": {"total": len(sheet_statuses), **sheet_counts},
            "rows": {
                "verified": rows_verified,
                "failed": rows_failed,
                "uncertain": rows_uncertain,
                "skipped": rows_skipped,
                "planned": rows_planned,
            },
            "rows_unknown": bool(rows_unknown),
            "proven_no_write": bool(proven_no_write),
            "written_sheets": cls._as_int(written),
            "failed_sheets": cls._as_int(failed),
            "note": ("rows.verified 只统计 apply_plan 逐格回读校验通过的行；"
                     "无法证明时为 null（未知），不能用计划数或成功表数顶替"),
            "next_action": str(next_action or ""),
        }

    def _wps_reject(self, code: str, reason: str, next_action: str = "", *,
                    status: str = "rejected",
                    summary: dict[str, Any] | None = None,
                    operation_id: str = "",
                    proven_no_write: bool = False,
                    planned_summary: dict[str, Any] | None = None,
                    execution_summary: dict[str, Any] | None = None,
                    counts_source: str = "",
                    **extra: Any) -> dict[str, Any]:
        """WPS 预览/上传的统一失败结果（仍保留旧 response 字段）。

        默认按**最保守**口径返回执行摘要：``rows`` 全为 ``None``（无法证明）；
        只有调用方明确知道“这次拒绝发生在任何云端写入之前”时才可传
        ``proven_no_write=True``，把 ``rows`` 标成可证明的 0。
        """
        payload: dict[str, Any] = {
            "ok": False,
            "status": status,
            "reason": str(reason or code),
            "code": str(code or "wps_rejected"),
            "next_action": str(next_action or ""),
            "summary": dict(summary or {"code": code}),
            "operation_id": str(operation_id or ""),
            # written/failed 是**执行结果**字段（不是计划数）：能证明零写入时是 0，
            # 不能证明时是 None（未知）——绝不用计划数或成功表数顶替（W6）。
            "written": 0 if proven_no_write else None,
            "failed": 0 if proven_no_write else None,
        }
        if planned_summary is not None:
            payload["planned_summary"] = planned_summary
        if execution_summary is None:
            execution_summary = self._wps_execution_summary(
                status=status, sheets=[], sheet_statuses=[], written=0, failed=0,
                uncertain=None, malformed=False, next_action=str(next_action or ""),
                planned_summary=planned_summary,
                proven_no_write=proven_no_write, executed=False,
                counts_source=counts_source)
        payload["execution_summary"] = execution_summary
        payload.update(extra)
        return payload

    def _wps_context_bundle(self) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
        """只读准备当前上下文；失败返回统一的拒绝结果，绝不触发写入。

        这里会**建立一次账本快照**（``bundle["ledger"]``），并让后续的
        ``build_plan`` 与 ``apply_plan`` 共用同一个对象（W1）：

        * 计划里的 ``SheetPlan.ledger_digest`` 记录的就是这份快照；
        * ``apply_plan`` 在跨进程锁内重新读取磁盘账本，把
          ``plan.ledger_digest`` 与锁内摘要比较，不一致即零写入 ``repreview``；
        * 同时传给 ``apply_plan`` 的 ledger 对象自己的加载摘要也锚在计划时刻，
          两层都能发现“另一个进程在计划构建后提交了账本”的情况。

        绝不能在建计划与执行之间各新建一个 ``SyncLedger()``：那样比较的就是
        “同一时刻的两份相同快照”，旧计划会静默重复写入（R6 W1）。
        """
        cfg = self._config
        if not cfg.excel_path:
            return None, self._wps_reject(
                "missing_excel", "请先在「订单处理」里选择排单表",
                "选择排单表后重新预览", proven_no_write=True)
        try:
            tables = self._wps_effective_tables()
        except WpsCloudError as exc:
            return None, self._wps_reject(
                "effective_tables_error", str(exc),
                "检查测试模式/目标表配置后重试", proven_no_write=True)
        if not tables:
            return None, self._wps_reject(
                "missing_tables", "测试模式未配置测试文件 id",
                "配置测试文件或关闭测试模式后重试", proven_no_write=True)
        target = target_date_for(start_hour=cfg.wps_target_hour_start,
                                 end_hour=cfg.wps_target_hour_end)
        try:
            local_sha = sha256_file(cfg.excel_path)
        except OSError as exc:
            return None, self._wps_reject(
                "local_file_unreadable", f"无法读取本地排单表：{exc}",
                "确认排单表可读后重试", proven_no_write=True)
        # 账本快照必须早于任何云端读取建立，才能覆盖“计划构建期间另一个进程提交”
        # 的窗口；损坏/不可读账本在这里就失败关闭（零云端写入）。
        try:
            ledger = SyncLedger()
        except WpsCloudError as exc:
            return None, self._wps_reject(
                "local_state_blocked", f"本地账本/意图日志不可用：{exc}",
                "只读核对；先修复本地日志/账本后重新预览",
                status="blocked", summary={"code": "local_state_blocked"},
                proven_no_write=True)
        except Exception as exc:  # noqa: BLE001 - 账本异常一律失败关闭
            return None, self._wps_reject(
                "local_state_blocked", f"本地账本不可用：{type(exc).__name__}",
                "只读核对；先修复本地日志/账本后重新预览",
                status="blocked", summary={"code": "local_state_blocked"},
                proven_no_write=True)
        context = self._wps_context_snapshot(
            local_sha256=local_sha, tables=tables, target=target)
        return {
            "config": cfg,
            "tables": tables,
            "target": target,
            "local_sha256": local_sha,
            "context": context,
            "context_fingerprint": fingerprint_payload(context),
            "ledger": ledger,
        }, None

    def _wps_read_plans(self, bundle: dict[str, Any]
                        ) -> tuple[list[Any] | None, dict[str, Any] | None]:
        """按 bundle 只读构建计划；不消费预览令牌、不写云端/账本。

        ``ledger`` 必须来自 :meth:`_wps_context_bundle` 的那一份快照：它既被写进
        ``SheetPlan.ledger_digest``，也会原样交给 ``apply_plan``，从而保证
        “计划时刻的账本快照”与“执行时刻的磁盘账本”可比（W1）。
        """
        cfg = bundle["config"]
        try:
            cli = self._wps_cli()
            if not cli.authenticated():
                return None, self._wps_reject(
                    "unauthenticated", "尚未授权云文档，请先点击「去授权」",
                    "点击「去授权」后重新预览", proven_no_write=True)
            # 构建前后各校验一次本地文件；文件读取期间被写就拒绝，避免计划与指纹错配。
            if sha256_file(cfg.excel_path) != bundle["local_sha256"]:
                return None, self._wps_reject(
                    "local_file_changed", "本地排单表在预览后已变化，请重新预览",
                    "文件稳定后重新预览", proven_no_write=True)
            local = read_local_orders(cfg.excel_path, log=self.log)
            if sha256_file(cfg.excel_path) != bundle["local_sha256"]:
                return None, self._wps_reject(
                    "local_file_changed", "本地排单表在读取过程中发生变化，请重新预览",
                    "文件稳定后重新预览", proven_no_write=True)
            ledger = bundle.get("ledger")
            if ledger is None:
                # 兼容直接构造 bundle 的调用方（测试/未来重构）：没有随 bundle
                # 传递快照时绝不静默新建第二个账本，而是就地建立并写回 bundle，
                # 保证同一次预览/上传里 build_plan 与 apply_plan 用的是同一份。
                try:
                    ledger = SyncLedger()
                except WpsCloudError as exc:
                    return None, self._wps_reject(
                        "local_state_blocked", f"本地账本/意图日志不可用：{exc}",
                        "只读核对；先修复本地日志/账本后重新预览",
                        status="blocked", summary={"code": "local_state_blocked"},
                        proven_no_write=True)
                bundle["ledger"] = ledger
            plans = build_plan(
                cli, local_orders=local, tables=bundle["tables"],
                target=bundle["target"], ledger=ledger,
                marker_enabled=bool(cfg.wps_marker_enabled) and not bool(cfg.wps_test_mode),
                run_date=_dt.date.today(),
                address_order=cfg.wps_address_order,
                sort_enabled=cfg.wps_sort_enabled,
                log=self.log)
            if sha256_file(cfg.excel_path) != bundle["local_sha256"]:
                return None, self._wps_reject(
                    "local_file_changed", "本地排单表在计划构建期间发生变化，请重新预览",
                    "文件稳定后重新预览", proven_no_write=True)
            return list(plans), None
        except WpsCloudError as exc:
            error_name = type(exc).__name__
            if error_name in ("LedgerCorruptError", "JournalError",
                              "JournalCorruptError"):
                return None, self._wps_reject(
                    "local_state_blocked",
                    f"本地账本/意图日志不可用：{exc}",
                    "只读核对；先修复本地日志/账本后重新预览",
                    status="blocked",
                    summary={"code": "local_state_blocked"},
                    proven_no_write=True)
            return None, self._wps_reject(
                "plan_failed", str(exc), "检查云端状态/授权后重新预览",
                status="failed", proven_no_write=True)
        except Exception as exc:  # noqa: BLE001 - 不能把异常抛给前端
            return None, self._wps_reject(
                "unexpected", f"{type(exc).__name__}: {exc}",
                "查看日志后重试", status="failed", proven_no_write=True)

    def _wps_structured_tables(self, plans: list[Any]) -> list[dict[str, Any]]:
        """把计划转成前端可直接渲染的逐表结构，并补每表统计。"""
        tables = canonical_plan(plans)
        for table in tables:
            changes = table.get("changes") or []
            counts = {
                "to_update": 0,
                "to_append": 0,
                "unchanged": 0,
                "skipped": 0,
                "warned": len(table.get("warnings") or []),
                "blocked": 1 if table.get("blocked_reason") else 0,
            }
            for change in changes:
                if change.get("target_blocked"):
                    counts["skipped"] += 1
                elif str(change.get("kind") or "") == "new":
                    counts["to_append"] += 1
                elif change.get("needs_write"):
                    counts["to_update"] += 1
                else:
                    counts["unchanged"] += 1
            table["counts"] = counts
            table["sort"] = {
                "enabled": bool(table.get("sort_enabled")),
                "sort_range": table.get("sort_range") or "",
                "sort_key_col": table.get("sort_key_col") or 0,
                "row_keys_count": len(table.get("row_keys") or []),
                "unknown_addresses": table.get("unknown_addresses") or [],
            }
        return tables

    def _wps_blocked_list(self, tables: list[dict[str, Any]]) -> list[dict[str, str]]:
        return [{"sheet": str(item.get("sheet") or ""),
                 "reason": str(item.get("blocked_reason") or "")}
                for item in tables if item.get("blocked_reason")]

    def _wps_warning_list(self, tables: list[dict[str, Any]]) -> list[str]:
        warnings: list[str] = []
        for item in tables:
            sheet = str(item.get("sheet") or "")
            for warning in item.get("warnings") or []:
                warnings.append(f"{sheet}：{warning}" if sheet else str(warning))
        return warnings

    @staticmethod
    def _wps_context_changes(old: dict[str, Any],
                             new: dict[str, Any]) -> list[str]:
        return sorted(
            key for key in set(old or {}) | set(new or {})
            if (old or {}).get(key) != (new or {}).get(key))

    def _preview_rejection(self, preview_id: str, code: str,
                           operation_id: str = "") -> dict[str, Any]:
        if code == "preview_not_found":
            reason = "预览不存在或已被清理，请重新预览"
        elif code == "preview_expired":
            reason = "预览已过期（超过 10 分钟），请重新预览"
        elif code == "preview_consumed":
            reason = "该预览已使用或已在处理中，不能重放，请重新预览"
        elif code in ("preview_changed", "preview_invalidated"):
            reason = "预览后内容或环境已变化，该预览已失效，请重新预览"
        elif code == "missing_preview":
            reason = "缺少 preview_id：旧版无参上传已禁用，请先预览"
        else:
            reason = f"预览状态不可用（{code}），请重新预览"
        return self._wps_reject(
            code, reason, "重新调用 wps_preview()",
            summary={"code": code, "preview_id": preview_id},
            operation_id=operation_id, preview_id=preview_id,
            # 令牌不存在/过期/已消费：都在任何云端写入之前返回。
            proven_no_write=True)

    def _preview_changed(self, preview_id: str, changed: list[str],
                         operation_id: str) -> dict[str, Any]:
        return self._wps_reject(
            "preview_changed",
            "预览后本地文件、目标表配置或云端计划已变化，拒绝上传且未写入任何内容",
            "重新调用 wps_preview()",
            summary={"code": "preview_changed", "preview_id": preview_id,
                     "changed": sorted(set(changed))},
            operation_id=operation_id, preview_id=preview_id,
            proven_no_write=True)

    # ------------------------------------------------------------------
    # W7：只读云入口与上传/任务的统一互斥
    #
    # preview / check_copies / sss_day_orders 都会读云端（消耗同一份每日额度），
    # sss_day_orders 还会把“上传中途的半份名单”留档进《闪时送.xlsx》——它可能是
    # 闪时送下单来源。因此这些入口必须与 wps_upload / 订单 / 闪时送任务共用同一个
    # 原子占位：占位**只覆盖内存状态**（微秒级），绝不包住云端调用，冲突立即返回、
    # 不排队，所以不存在“持锁再请求持锁”的嵌套，也就没有死锁。
    #
    # 跨进程范围（明确边界，不夸大）：
    # * 本进程内：所有入口共用 OperationCoordinator，覆盖完整；
    # * 跨进程：只有 ``apply_plan`` 自己持 ``<ledger>.oplock`` 排他锁，只读入口
    #   **不**去抢这把锁——读请求动辄数秒，若持锁会让另一进程的 apply_plan 等满
    #   30 秒后失败，反而制造新的故障模式。因此双实例部署下，另一个进程仍可能
    #   在写入期间发起只读读取。这是已知范围限制：不宣称跨进程已覆盖。
    # ------------------------------------------------------------------
    def _begin_cloud_read(self, mode: str, title: str,
                          action: str) -> tuple[Any, dict[str, Any] | None]:
        """只读云入口取统一占位；失败返回 (None, 冲突结果)。"""
        reservation = self._operations.try_reserve(
            mode, summary={"title": title}, phase="reading",
            next_action=f"等待{title}结束后重试")
        if not reservation.granted:
            return None, self._operation_conflict_payload(reservation.conflict,
                                                          action=action)
        return reservation.operation, None

    def _finish_cloud_read(self, operation: Any, result: dict[str, Any], *,
                           title: str) -> None:
        """只读云入口释放占位；重复调用/已被终结时静默返回。"""
        if operation is None or not isinstance(result, dict):
            return
        if not self._operations.is_active(operation):
            return
        ok = bool(result.get("ok"))
        raw_status = str(result.get("status") or "").strip()
        allowed = {"success", "noop", "partial", "failed", "error", "rejected",
                   "uncertain", "blocked", "recovered", "not_started"}
        status = raw_status if raw_status in allowed else (
            "success" if ok else "error")
        summary = {"title": title, "ok": ok}
        for key in ("code", "reason"):
            value = str(result.get(key) or "").strip()
            if value:
                summary[key] = value[:200]
        self._operations.finish(
            operation, status=status,
            reason=str(result.get("code") or result.get("reason") or "")[:200],
            summary=summary, next_action=str(result.get("next_action") or "")[:200])

    def _cloud_read_conflict(self, conflict: dict[str, Any], *,
                             code: str = "operation_conflict") -> dict[str, Any]:
        """把占位冲突转成 WPS 入口的拒绝结果（保留旧 ok/status/reason 字段）。"""
        return self._wps_reject(
            code,
            str(conflict.get("message") or "已有其他操作正在进行，已拒绝本次只读读取"),
            str(conflict.get("next_action") or "等待当前操作结束后重试"),
            status="rejected", summary={"code": code},
            operation_id=str(conflict.get("operation_id") or ""),
            proven_no_write=True,
            conflicting_operation=(conflict.get("summary") or {}
                                   ).get("conflicting_operation"))

    def wps_preview(self) -> dict[str, Any]:
        """只读云端，生成结构化预览并发放 10 分钟一次性上传令牌。

        不写云端、不动账本；成功结果的 ``preview_id`` 必须原样传给
        :meth:`wps_upload`。其他旧字段（text/summary/target_date/test_mode）保留。

        ``wps_enabled=False`` 时服务端直接拒绝（W5）：这不是前端隐藏按钮的问题，
        任何客户端直调接口都不允许读云。
        """
        if not self._config.wps_enabled:
            return self._wps_reject(
                "wps_disabled",
                "云文档同步已关闭（wps_enabled=False），服务端已拒绝预览，"
                "未读取也未写入任何云端内容",
                "在「云文档同步」中开启后再试", status="rejected",
                proven_no_write=True)
        operation, conflict = self._begin_cloud_read(
            "wps_preview", "云文档预览", "预览云文档")
        if conflict is not None:
            return self._cloud_read_conflict(conflict)
        try:
            result = self._wps_preview_impl()
        except BaseException:
            # 意外异常也必须先释放占位：否则一次预览失败会把整个 Bridge 永久锁死
            # （后续订单/上传/恢复全部 operation_conflict）。异常本身照旧向上抛，
            # 保持“非预期异常 = 500/调用方可见”的既有行为。
            self._finish_cloud_read(operation, {"ok": False, "status": "error",
                                                "code": "unexpected"},
                                    title="云文档预览")
            raise
        self._finish_cloud_read(operation, result, title="云文档预览")
        return result

    def _wps_preview_impl(self) -> dict[str, Any]:
        """``wps_preview`` 的实际实现（调用方已取占位）。"""
        bundle, error = self._wps_context_bundle()
        if error is not None:
            return error
        assert bundle is not None
        plans, error = self._wps_read_plans(bundle)
        if error is not None:
            return error
        assert plans is not None
        try:
            for plan in plans:
                for warning in (getattr(plan, "warnings", None) or []):
                    self.log(f"[云同步预览] {getattr(plan, 'sheet', '?')}：{warning}", "WARN")
            text = format_plan(plans)
            stats = summarize_plan(plans)
        except Exception as exc:  # noqa: BLE001
            return self._wps_reject(
                "preview_format_failed", f"{type(exc).__name__}: {exc}",
                "查看日志后重试", status="failed", proven_no_write=True)
        tables = self._wps_structured_tables(plans)
        blocked = self._wps_blocked_list(tables)
        warnings = self._wps_warning_list(tables)
        plan_fp = plan_fingerprint(plans)
        record = self._previews.create(
            local_sha256=bundle["local_sha256"],
            context=bundle["context"],
            context_fingerprint=bundle["context_fingerprint"],
            plan=tables,
            plan_fingerprint=plan_fp,
            summary=stats,
            text=text,
            tables=tables,
            blocked=blocked,
            warnings=warnings,
            target_date=bundle["target"].isoformat(),
            target_tables=self._wps_string_tables(bundle["tables"]),
        )
        planned_summary = self._wps_planned_summary(plans)
        result: dict[str, Any] = {
            "ok": True,
            "status": "preview_ready",
            "reason": "",
            "code": "",
            "next_action": "wps_upload(preview_id)",
            "summary": stats,
            "stats": stats,
            "planned_summary": planned_summary,
            "execution_summary": self._wps_execution_summary(
                status="preview_ready", sheets=[], sheet_statuses=[],
                written=0, failed=0, uncertain=False, malformed=False,
                next_action="wps_upload(preview_id)",
                planned_summary=planned_summary, proven_no_write=True,
                executed=False),
            "operation_id": "",
            "text": text,
            "tables": tables,
            "blocked": blocked,
            "warnings": warnings,
            "test_mode": bool(bundle["config"].wps_test_mode),
        }
        result.update(self._previews.public(record))
        return result

    def wps_upload(self, preview_id: str = "") -> dict[str, Any]:
        """真正写云端；必须传入预览令牌，旧无参调用会被安全拒绝。

        执行前会在互斥占位内重新只读构建计划并核对预览指纹；任何变化都在消费
        令牌/写入前拒绝。通过后原子消费令牌再 ``apply_plan``。

        ``wps_enabled=False`` 时服务端直接拒绝（W5）：即使客户端手里还攥着
        关闭之前拿到的 ``preview_id``，也不会写一个字；该令牌同时被作废，
        重新开启云同步后也不能用它上传（必须重新预览）。
        """
        pid = str(preview_id or "").strip()
        if not pid:
            return self._wps_reject(
                "missing_preview",
                "旧版无参上传已禁用：请先调用 wps_preview()，再传入 preview_id",
                "重新预览并传入 preview_id", proven_no_write=True)
        if not self._config.wps_enabled:
            if pid:
                # 作废手上这份旧令牌：开启后必须重新预览，不能拿旧 preview_id 直接写。
                self._previews.invalidate(pid, "preview_invalidated")
            return self._wps_reject(
                "wps_disabled",
                "云文档同步已关闭（wps_enabled=False），服务端已拒绝上传，"
                "未写入任何云端内容",
                "在「云文档同步」中开启后重新预览", status="rejected",
                preview_id=pid, proven_no_write=True)
        if self.worker_alive():
            return self._busy_result_from_worker(
                "订单/闪时送任务正在运行，请先停止后再上传云文档")
        reservation = self._operations.try_reserve(
            "wps_upload", summary={"preview_id": pid, "title": "云文档上传"},
            next_action="等待云文档上传结束或查询 operation_status")
        if not reservation.granted:
            return self._operation_conflict_payload(reservation.conflict,
                                                    action="上传云文档")
        operation = reservation.operation
        assert operation is not None
        return self._wps_upload_with_reservation(pid, operation)

    def _wps_upload_with_reservation(self, preview_id: str,
                                     operation: Any) -> dict[str, Any]:
        """包住占位生命周期：无论内部怎么返回/异常，都在 finally 释放互斥槽位。"""
        result: dict[str, Any] | None = None
        self._set_status("updating")
        try:
            result = self._wps_upload_impl(preview_id, operation)
            return result
        except Exception as exc:  # noqa: BLE001 - 对外必须是 JSON
            logger.exception("wps_upload failed")
            self.log(f"[云同步] 异常：{type(exc).__name__}: {exc}", "ERROR")
            recorded = getattr(operation, "summary", None)
            planned = (recorded.get("planned_summary")
                       if isinstance(recorded, dict) else None)
            result = self._wps_reject(
                "unexpected", f"{type(exc).__name__}: {exc}",
                "查看日志后重新预览", status="failed",
                planned_summary=planned if isinstance(planned, dict) else None,
                operation_id=operation.operation_id, preview_id=preview_id,
                # 这里可能是“写入之后”的异常（例如结果组装失败）：行数未知，
                # 且 counts_source 不能谎称“写前拒绝”。
                counts_source="unknown_after_exception")
            return result
        finally:
            if self._operations.is_active(operation):
                if result is None:
                    operation_status = "error"
                    reason = "upload_aborted_unexpectedly"
                    next_action = "查看日志后重新预览"
                    summary = {"preview_id": preview_id}
                else:
                    raw_status = str(result.get("status") or "")
                    if raw_status in {"success", "noop", "partial", "failed",
                                      "error", "rejected", "uncertain",
                                      "blocked", "recovered", "not_started"}:
                        operation_status = raw_status
                    elif result.get("ok"):
                        operation_status = "success"
                    else:
                        operation_status = "error"
                    reason = str(result.get("code") or result.get("reason") or "")
                    next_action = str(result.get("next_action") or "")
                    summary = {"preview_id": preview_id,
                               "status": result.get("status")}
                    if isinstance(result.get("summary"), dict):
                        summary.update(result["summary"])
                    for key in ("executor_next_action", "journal_path",
                                "recovery", "failed", "written"):
                        if key in result and result.get(key) is not None:
                            summary[key] = result[key]
                    summary.setdefault("message", str(result.get("reason") or ""))
                self._operations.finish(
                    operation, status=operation_status, reason=reason,
                    summary=summary, next_action=next_action)
            if self._status == "updating":
                self._set_status("ready")

    def _wps_upload_impl(self, preview_id: str,
                         operation: Any) -> dict[str, Any]:
        record, code = self._previews.get(preview_id)
        if code:
            return self._preview_rejection(preview_id, code,
                                           operation.operation_id)
        assert record is not None
        bundle, error = self._wps_context_bundle()
        if error is not None:
            error["preview_id"] = preview_id
            error["operation_id"] = operation.operation_id
            return error
        assert bundle is not None
        changed = self._wps_context_changes(record.context, bundle["context"])
        if changed:
            self._previews.invalidate(preview_id, "preview_changed")
            return self._preview_changed(preview_id, changed,
                                         operation.operation_id)
        plans, error = self._wps_read_plans(bundle)
        if error is not None:
            if error.get("code") == "local_file_changed":
                self._previews.invalidate(preview_id, "preview_changed")
            error.setdefault("preview_id", preview_id)
            error.setdefault("operation_id", operation.operation_id)
            return error
        assert plans is not None
        fresh_tables = self._wps_structured_tables(plans)
        fresh_fp = plan_fingerprint(plans)
        if (fresh_fp != record.plan_fingerprint
                or fresh_tables != record.plan):
            self._previews.invalidate(preview_id, "preview_changed")
            return self._preview_changed(
                preview_id, changed + ["plan"], operation.operation_id)
        consumed, consume_code = self._previews.consume(preview_id)
        if consume_code:
            return self._preview_rejection(preview_id, consume_code,
                                           operation.operation_id)
        assert consumed is not None
        return self._wps_apply_plan(plans, record, operation, bundle)

    def _wps_apply_plan(self, plans: list[Any], record: Any, operation: Any,
                        bundle: dict[str, Any]) -> dict[str, Any]:
        cfg = self._config
        planned_summary = self._wps_planned_summary(plans)
        self._operations.update(
            operation, phase="applying",
            summary={"preview_id": record.preview_id,
                     "planned_summary": planned_summary},
            next_action="等待云端写入与回读校验完成")
        self.log(f"[云同步] 目标日期 {bundle['target'].isoformat()}，"
                 f"{'测试模式（只写测试文件）' if cfg.wps_test_mode else '正式模式'}"
                 + ("；按地址顺序重排整表" if cfg.wps_sort_enabled else "；已关闭排序"))
        summary = summarize_plan(plans)
        try:
            cli = self._wps_cli()
            # W1：必须复用 bundle 里那份“计划时刻”的账本快照，不能在这里再新建
            # 一个 SyncLedger()——那会让 apply_plan 的 stale 比较退化成
            # “同一时刻的两份相同快照”，旧计划就能跨进程重复写入。
            ledger = bundle.get("ledger")
            if ledger is None:
                ledger = SyncLedger()
                bundle["ledger"] = ledger
            result = apply_plan(
                cli, plans, ledger=ledger,
                marker_enabled=bool(cfg.wps_marker_enabled) and not bool(cfg.wps_test_mode),
                log=self.log)
        except WpsCloudError as exc:
            error_name = type(exc).__name__
            self.log(f"[云同步] 失败：{exc}", "ERROR")
            # apply_plan 抛出时可能已经写过一部分：行数一律按未知上报（W6）。
            if error_name in ("LedgerCorruptError", "JournalError",
                              "JournalCorruptError"):
                return self._wps_reject(
                    "local_state_blocked", f"本地账本/意图日志不可用：{exc}",
                    "只读核对；先修复本地日志/账本后重新预览",
                    status="blocked",
                    summary={"code": "local_state_blocked", "stats": summary},
                    operation_id=operation.operation_id,
                    preview_id=record.preview_id,
                    target_date=bundle["target"].isoformat(),
                    planned_summary=planned_summary,
                    execution_summary=self._wps_execution_summary(
                        status="blocked", sheets=[], sheet_statuses=[],
                        written=0, failed=0, uncertain=None, malformed=False,
                        next_action="fix_journal",
                        planned_summary=planned_summary, executed=False),
                    summary_stats=summary)
            return self._wps_reject(
                "cloud_error", str(exc), "检查授权/云端状态后重新预览",
                status="failed", summary={"code": "cloud_error", "stats": summary},
                operation_id=operation.operation_id, preview_id=record.preview_id,
                target_date=bundle["target"].isoformat(),
                planned_summary=planned_summary,
                execution_summary=self._wps_execution_summary(
                    status="failed", sheets=[], sheet_statuses=[],
                    written=0, failed=0, uncertain=None, malformed=False,
                    next_action="repreview", planned_summary=planned_summary,
                    executed=False),
                summary_stats=summary)
        except Exception as exc:  # noqa: BLE001
            self.log(f"[云同步] 异常：{type(exc).__name__}: {exc}", "ERROR")
            # 未捕获异常无法证明是否已写入：executed=False + 不宣称零写入。
            return self._wps_reject(
                "unexpected", f"{type(exc).__name__}: {exc}",
                "查看日志后重新预览", status="failed",
                summary={"code": "unexpected", "stats": summary},
                operation_id=operation.operation_id, preview_id=record.preview_id,
                target_date=bundle["target"].isoformat(),
                planned_summary=planned_summary,
                execution_summary=self._wps_execution_summary(
                    status="failed", sheets=[], sheet_statuses=[],
                    written=0, failed=0, uncertain=None, malformed=False,
                    next_action="manual_reconcile",
                    planned_summary=planned_summary, executed=False))

        raw_sheets = result.get("sheets")
        sheets = raw_sheets if isinstance(raw_sheets, list) else []
        malformed_sheets = raw_sheets is not None and not isinstance(raw_sheets, list)
        sheet_dicts = [item for item in sheets if isinstance(item, dict)]
        sheet_values_valid = len(sheet_dicts) == len(sheets)
        sheet_statuses = [str(item.get("status") or "").strip().lower()
                          for item in sheet_dicts]
        try:
            failed = int(result.get("failed") or 0)
            written = int(result.get("written") or 0)
        except (TypeError, ValueError):
            failed = written = 0
        executor_status = str(result.get("status") or "").strip().lower()
        uncertain = (
            bool(result.get("uncertain"))
            or executor_status in ("uncertain", "unknown")
            or any(bool(item.get("uncertain"))
                   or str(item.get("status") or "").strip().lower()
                   in ("uncertain", "unknown")
                   for item in sheet_dicts)
        )
        sheets_present = bool(sheets)
        all_sheets_successish = (
            sheets_present and sheet_values_valid
            and all(item_status in ("ok", "noop", "verified")
                    for item_status in sheet_statuses)
        )
        written_verified = sum(
            1 for item_status in sheet_statuses
            if item_status in ("ok", "verified"))
        failed_sheets = [str(item.get("sheet") or "") for item in sheet_dicts
                         if str(item.get("status") or "").strip().lower() in
                         ("failed", "verify_failed", "stale_batch", "uncertain",
                          "blocked")]
        possible_write = bool(written > 0 or written_verified > 0)
        verification_label = str(result.get("verification") or "").strip().lower()
        verification_missing = bool(
            result.get("verified") is False
            or result.get("verification_missing")
            or verification_label in ("missing", "unverified", "none",
                                      "not_verified"))
        contradictory = bool(
            malformed_sheets
            or not sheet_values_valid
            or (not sheets_present and written > 0)
            or (written_verified > 0 and written == 0)
            or (written > written_verified)
        )
        # 成功白名单：B 契约明确 ok / 兼容旧 verified；且必须存在已验证的逐表 ok/noop。
        explicit_success = executor_status in ("", "ok", "verified")
        verified_success = bool(
            not uncertain
            and failed == 0
            and not contradictory
            and not verification_missing
            and sheets_present
            and sheet_values_valid
            and all_sheets_successish
            and written <= written_verified
        )
        known_non_success = {
            "blocked", "uncertain", "unconfirmed", "partial", "failed",
            "error", "recovered", "not_started", "dry_run", "preflight_ok",
            "no_orders", "insufficient_balance", "balance_unknown", "noop",
        }
        if executor_status == "blocked":
            status = "blocked"
        elif uncertain:
            status = "uncertain"
        elif explicit_success and verified_success:
            status = "success"
        elif (executor_status == "noop" and failed == 0 and not contradictory
              and (all_sheets_successish or (not sheets_present
                                             and not malformed_sheets))):
            status = "noop"
        elif executor_status in ("recovered", "not_started"):
            status = executor_status
        elif executor_status == "partial":
            # 显式 partial 绝不能因为 failed 计数缺失而掉进 success 分支。
            status = "partial"
        elif executor_status in ("failed", "error"):
            status = "failed"
        elif executor_status in ("dry_run", "preflight_ok", "no_orders",
                                 "insufficient_balance", "balance_unknown"):
            status = executor_status
        elif explicit_success:
            # 旧结果没有显式 status 时，failed/sheet failed 仍按 partial/failed 承接；
            # 显式 ok/verified 与逐表失败矛盾时则必须 uncertain。
            sheet_failed = any(item_status in (
                "failed", "verify_failed", "stale_batch", "blocked")
                for item_status in sheet_statuses)
            if executor_status == "" and (failed > 0 or sheet_failed):
                status = "partial" if possible_write else "failed"
            else:
                status = "uncertain" if (possible_write or contradictory
                                         or malformed_sheets or sheets_present) else "failed"
        elif executor_status == "noop":
            status = ("uncertain" if (possible_write or contradictory
                                      or malformed_sheets or sheets_present)
                      else "noop")
        elif executor_status in known_non_success:
            status = "failed"
        else:
            # 未知/缺失执行器状态：有写入或任何逐表记录就不能猜 success。
            status = "uncertain" if (possible_write or contradictory
                                     or malformed_sheets or bool(sheets)) else "failed"
        if status == "uncertain":
            uncertain = True
        ok = status in ("success", "noop")
        reason = str(result.get("reason") or "")
        if uncertain:
            detail = reason
            reason = "只读核对，不重新上传"
            if detail and detail not in reason:
                reason = f"{reason}：{detail}"
        elif status == "recovered":
            reason = reason or ("发现历史未完成操作并完成只读恢复，"
                                "本轮计划未执行，请重新预览")
        elif status == "not_started":
            reason = reason or ("发现历史未完成操作，本轮计划未开始执行，"
                                "请重新预览")
        elif status == "blocked":
            reason = reason or "本地意图日志不可用/不可写，已阻断写入；请先修复日志"
        elif not ok and not reason:
            reason = (f"以下表写入失败：{'、'.join(failed_sheets)}，详见日志"
                      if failed_sheets else "云端写入未全部成功")

        executor_next_action = str(result.get("next_action") or "").strip()
        next_action = executor_next_action
        if status == "blocked":
            next_action = next_action or "fix_journal"
        elif status == "uncertain":
            # 用户可读的下一步必须是明确动作，而不是再次提交。
            next_action = "只读核对，不重新上传"
        elif status in ("recovered", "not_started"):
            next_action = next_action or "repreview"
        elif status == "noop":
            next_action = next_action or "none"
        elif not ok:
            next_action = next_action or "repreview"

        payload_code = str(result.get("code") or "")
        if not payload_code:
            payload_code = {
                "uncertain": "uncertain",
                "blocked": "blocked",
                "recovered": "recovered",
                "not_started": "not_started",
            }.get(status, "" if ok else "cloud_write_failed")
        # 保留旧日志逐表输出。
        for plan in plans:
            for warning in (getattr(plan, "warnings", None) or []):
                self.log(f"[云同步] {getattr(plan, 'sheet', '?')}：{warning}", "WARN")
        for item in sheet_dicts:
            item_status = item.get("status")
            if item_status == "ok":
                detail = item.get("reason") or f"{item.get('people', 0)} 人"
                self.log(f"[云同步] ✔ {item.get('sheet', '?')}：{detail}", "OK")
            elif item_status == "verify_failed":
                self.log(f"[云同步] ✘ {item.get('sheet', '?')}：写入后回读校验未通过"
                         f"（{'; '.join(item.get('problems', [])[:3])}）", "ERROR")
            elif item_status == "failed":
                self.log(f"[云同步] ✘ {item.get('sheet', '?')}：{item.get('reason', '写入失败')}", "ERROR")
            elif item_status == "stale_batch":
                self.log(f"[云同步] ✘ {item.get('sheet', '?')}：本地表批次日期不符，已拒绝写入"
                         f"（{item.get('reason', '')}）", "ERROR")
            elif item_status == "blocked":
                self.log(f"[云同步] ✘ {item.get('sheet', '?')}：意图日志阻断，未写入"
                         f"（{item.get('reason', '')}）", "ERROR")
            elif item_status == "uncertain":
                self.log(f"[云同步] ? {item.get('sheet', '?')}：写入结果不确定"
                         f"（{item.get('reason', '')}）", "ERROR")
            elif item_status == "skipped":
                self.log(f"[云同步] — {item.get('sheet', '?')}：{item.get('reason', '跳过')}")
            if item.get("sort_mismatch"):
                self.log(f"[云同步] {item.get('sheet', '?')}：云端实际排序位置与预测略有出入，"
                         f"已按实际行号写入（数据无误）", "WARN")
        level = "ERROR" if status not in ("success", "noop") else "OK"
        execution_summary = self._wps_execution_summary(
            status=status, sheets=sheet_dicts, sheet_statuses=sheet_statuses,
            written=written, failed=failed, uncertain=bool(uncertain),
            malformed=bool(malformed_sheets or not sheet_values_valid),
            next_action=next_action, planned_summary=planned_summary)
        # W6：日志分开“计划”和“实际”，不再把计划行数当成功行数。
        planned_rows = planned_summary.get("rows") or {}
        updated_count = self._as_int(planned_rows.get("to_update"))
        appended_count = self._as_int(planned_rows.get("to_append"))
        unchanged_count = self._as_int(planned_rows.get("unchanged"))
        planned_skipped = self._as_int(planned_rows.get("skipped"))
        warned_count = self._as_int(planned_rows.get("warned"))
        exec_rows = execution_summary["rows"]
        exec_sheets = execution_summary["sheets"]
        if exec_rows["verified"] is None:
            actual_text = "实际写入行数未知（需只读核对云端）"
        else:
            actual_text = f"实际已验证 {exec_rows['verified']} 行"
        self.log(f"[云同步] 计划：更新 {updated_count} 行、新增 {appended_count} 行、"
                 f"不用动 {unchanged_count} 行"
                 + (f"、跳过 {planned_skipped} 人（日期格是协作者写的）"
                    if planned_skipped else "")
                 + (f"、注意 {warned_count} 项" if warned_count else ""), level)
        self.log(f"[云同步] 实际：{actual_text}；"
                 f"已验证表 {exec_sheets['verified']} 张、"
                 f"失败表 {exec_sheets['failed']} 张、"
                 f"不确定表 {exec_sheets['uncertain']} 张、"
                 f"跳过表 {exec_sheets['skipped']} 张、"
                 f"未写表 {exec_sheets['noop']} 张"
                 + (f"（未知表 {exec_sheets['other']} 张）"
                    if exec_sheets["other"] else ""), level)
        fresh_tables = self._wps_structured_tables(plans)
        blocked = self._wps_blocked_list(fresh_tables)
        try:
            result_text = format_plan(plans)
        except Exception:  # noqa: BLE001 - 写入已完成，展示文本不应再让调用失败
            result_text = str(getattr(record, "text", "") or "")
            self.log("[云同步] 结果文本格式化失败，已回退预览文本", "WARN")
        payload: dict[str, Any] = {
            "ok": ok,
            "status": status,
            "reason": reason,
            "code": payload_code,
            "next_action": next_action,
            # summary/stats 改为**执行口径**（W6）：旧字段名保留，含义不再含糊；
            # 计划数在 planned_summary 里单独标注 kind="plan"。
            "summary": execution_summary,
            "stats": execution_summary,
            "planned_summary": planned_summary,
            "execution_summary": execution_summary,
            "operation_id": operation.operation_id,
            "preview_id": record.preview_id,
            "target_date": bundle["target"].isoformat(),
            "result": result,
            "failed": failed,
            "written": written,
            "executor_status": executor_status,
            "executor_next_action": executor_next_action,
            "executor_operation_id": str(result.get("operation_id") or ""),
            "journal_path": str(result.get("journal_path") or ""),
            "verification_missing": verification_missing,
            "contradictory": contradictory,
            "possible_write": possible_write,
            "written_verified": written_verified,
            "recovery": result.get("recovery"),
            "text": result_text,
            "failed_sheets": [name for name in failed_sheets if name],
            "tables": fresh_tables,
            "blocked": blocked,
            "warnings": self._wps_warning_list(fresh_tables),
            "test_mode": bool(cfg.wps_test_mode),
            # 兼容旧调用方：summary_stats 仍是“计划摘要”，带 kind="plan" 标注。
            "summary_stats": planned_summary,
        }
        payload["uncertain"] = bool(uncertain)
        return payload

    def wps_check_copies(self) -> dict[str, Any]:
        """核对"当前写入目标"与正式表是否结构一致（只读）。

        测试副本是**静态快照**：只要协作者/用户在 WPS 里改了正式表，
        副本就会过时，测试结果就不能代表线上真实情况。这个检查用来提前发现。

        W7：它会读云端（最多 6 张表），必须与上传/任务统一互斥，否则上传进行中
        再跑一次核对会烧掉金山当日额度、并给出“上传中途”的过时快照。
        """
        operation, conflict = self._begin_cloud_read(
            "wps_check_copies", "核对云文档副本", "核对云文档副本")
        if conflict is not None:
            return {"ok": False, "reason": str(conflict.get("reason") or "busy"),
                    "status": str(conflict.get("status") or "rejected"),
                    "code": str(conflict.get("code") or "operation_conflict"),
                    "operation_id": str(conflict.get("operation_id") or ""),
                    "message": str(conflict.get("message") or ""),
                    "next_action": str(conflict.get("next_action") or "")}
        try:
            result = self._wps_check_copies_impl()
        except BaseException:
            # 关键：``_wps_check_copies_impl`` 允许抛出（``_wps_effective_tables``
            # 异常与非 WpsCloudError 异常都有回归测试钉住），但占位必须先释放，
            # 否则一次失败会让 Bridge 永久锁死。
            self._finish_cloud_read(operation, {"ok": False, "status": "error",
                                                "code": "unexpected"},
                                    title="核对云文档副本")
            raise
        self._finish_cloud_read(operation, result, title="核对云文档副本")
        return result

    def _wps_check_copies_impl(self) -> dict[str, Any]:
        cfg = self._config
        active = self._wps_effective_tables()
        production = cfg.wps_production_tables or {}
        try:
            cli = self._wps_cli()

            def people(file_id: str) -> dict[tuple[str, str], int]:
                """{（姓名, 电话）: 行号} —— 按人比对，而不是按行序。

                行序会因删行/重排变化，但"谁在里面"才是重点，因此用姓名+电话做键；
                顺序差异不算问题。
                """
                grid = cli.read_grid(file_id, 1, 2, 300, 0, 2)
                rows: dict[int, dict[int, str]] = {}
                for (r, c), v in grid.items():
                    rows.setdefault(r, {})[c] = str(v).strip()
                return {(row.get(0, ""), row.get(2, "")): r + 1
                        for r, row in rows.items() if r >= 2 and row.get(0)}

            def probe(sheet: str, conf: dict[str, str]) -> dict[str, Any]:
                """核对单个子表（只读）。

                只读写自己的局部变量，不碰 ``self`` 的任何可变状态，因此可以安全地
                放进工作线程并发执行；结果与串行版本逐字段一致。
                """
                file_id = conf.get("file_id", "")
                prod_id = (production.get(sheet) or {}).get("file_id", "")
                item: dict[str, Any] = {"sheet": sheet, "file_id": file_id,
                                        "production_id": prod_id}
                try:
                    active_people = people(file_id)
                except WpsCloudError as exc:
                    item.update(status="unreadable", reason=str(exc)[:120])
                    return item
                item["rows"] = len(active_people)
                if not prod_id or prod_id == file_id:
                    # 写入目标就是正式表本身（未使用副本），无需比对。
                    item.update(status="same_as_production")
                    return item
                try:
                    prod_people = people(prod_id)
                except WpsCloudError as exc:
                    item.update(status="production_unreadable", reason=str(exc)[:120])
                    return item
                item["production_rows"] = len(prod_people)
                if set(active_people) == set(prod_people):
                    item.update(status="aligned")
                else:
                    prod_names = {k[0] for k in prod_people}
                    active_names = {k[0] for k in active_people}
                    missing = sorted(prod_names - active_names)
                    extra = sorted(active_names - prod_names)
                    if not missing and not extra:
                        # 姓名一致、只是电话不同
                        item.update(status="drifted", phone_mismatch=True)
                    else:
                        item.update(status="drifted", missing=missing[:10],
                                    extra=extra[:10])
                return item

            items = list(active.items())
            results: list[dict[str, Any]] = []
            if len(items) <= 1:
                # 单张表没有可重叠的往返，直接顺序执行，省掉线程池开销。
                results = [probe(sheet, conf) for sheet, conf in items]
            else:
                # ``Executor.map`` 按**提交顺序**产出结果，因此 tables/drifted
                # 的顺序与串行版本完全一致，前端展示不受影响。
                #
                # 分批提交而不是一次提交全部：``probe`` 只把 ``WpsCloudError``
                # 转成状态，其他意外异常会向上抛。若一次提交全部，抛错时已经
                # 发出去的调用收不回来（``cancel()`` 只能取消尚未开始的任务），
                # 最多会白耗 n-1 次云端调用；分批后最多只多耗 workers-1 次，
                # 更贴近串行版本「出错即停」的行为。
                workers = max(1, min(WPS_COPY_CHECK_WORKERS, len(items)))
                with ThreadPoolExecutor(
                    max_workers=workers,
                    thread_name_prefix="wps-copy-check",
                ) as pool:
                    for start in range(0, len(items), workers):
                        batch = items[start:start + workers]
                        results.extend(pool.map(lambda pair: probe(*pair), batch))
        except WpsCloudError as exc:
            return {"ok": False, "reason": str(exc)}

        drifted = [r["sheet"] for r in results if r["status"] == "drifted"]
        for item in results:
            if item["status"] == "drifted":
                self.log(f"[云同步] 副本已过时：{item['sheet']}"
                         f"（正式 {item.get('production_rows')} 人 / 副本 {item.get('rows')} 人）",
                         "WARN")
        if drifted:
            self.log(f"[云同步] 建议重新同步副本：{'、'.join(drifted)}", "WARN")
        return {"ok": True, "drifted": drifted, "tables": results,
                "all_aligned": not drifted}

    def wps_authorize(self) -> dict[str, Any]:
        """启动 kdocs-cli 授权流程（在后台等用户在浏览器里确认）。"""
        try:
            cli = self._wps_cli()
        except WpsCloudError as exc:
            return {"ok": False, "reason": str(exc)}
        reservation = self._operations.try_reserve(
            "wps_authorize", summary={"title": "WPS 授权"},
            next_action="在浏览器完成授权或查询 operation_status")
        if not reservation.granted:
            conflict = self._operation_conflict_payload(
                reservation.conflict, action="启动 WPS 授权")
            conflict["hint"] = conflict.get("message") or "已有操作进行中"
            return conflict
        operation = reservation.operation
        assert operation is not None
        self._authorize_operation_id = operation.operation_id
        try:
            threading.Thread(target=self._wps_authorize_worker,
                             args=(cli,), daemon=True).start()
        except Exception:
            self._authorize_operation_id = ""
            self._operations.finish_if_active(
                operation, status="error", reason="authorize_start_failed",
                next_action="重试")
            raise
        return {
            "ok": True,
            "hint": "已启动授权，请按日志里的提示在浏览器中确认",
            "status": "running",
            "operation_id": operation.operation_id,
            "next_action": "在浏览器完成授权",
        }

    def _wps_authorize_worker(self, cli: KdocsCli) -> None:
        operation_id = self._authorize_operation_id
        try:
            if android_runtime.is_android():
                self._wps_authorize_worker_android()
            else:
                self._wps_authorize_worker_desktop(cli)
            self._emit_event("wps:status", self.wps_status())
        finally:
            self._authorize_operation_id = ""
            self._operations.finish_if_active(
                operation_id, status="success", reason="",
                next_action="授权流程已结束")

    def wps_logout(self) -> dict[str, Any]:
        """退出 WPS 授权（管理员操作），成功后前端刷新 wps_status。

        与云上传/订单任务共享互斥槽位；返回值保持旧协议 ``{ok, reason}`` 不变。
        """
        reservation = self._operations.try_reserve(
            "wps_logout", summary={"title": "WPS 退出授权"},
            next_action="等待退出授权完成")
        if not reservation.granted:
            return self._operation_conflict_payload(
                reservation.conflict, action="退出 WPS 授权")
        operation = reservation.operation
        assert operation is not None
        ok = False
        reason = ""
        try:
            try:
                if android_runtime.is_android():
                    android_runtime.cancel_authorization()
                    result = android_runtime.logout()
                    ok = result.ok
                    reason = result.error_code or result.stderr
                else:
                    cli = self._wps_cli()
                    ok = cli.logout()
                    reason = "" if ok else "kdocs-cli auth logout 执行失败"
            except (WpsCloudError, android_runtime.AndroidRuntimeError) as exc:
                reason = str(exc)
                return {"ok": False, "reason": reason}
            if ok:
                self.log("[云文档授权] 已退出 WPS 授权", "WARN")
            else:
                self.log(f"[云文档授权] 退出授权失败：{reason or '未知错误'}", "WARN")
            return {"ok": ok, "reason": reason}
        finally:
            self._operations.finish_if_active(
                operation, status="success" if ok else "error",
                reason="" if ok else reason, next_action="")

    def _wps_authorize_worker_desktop(self, cli: KdocsCli) -> None:
        import subprocess
        try:
            proc = subprocess.run(cli.login_argv(), capture_output=True, text=True,
                                  timeout=330, env=cli.login_env())
            out = (proc.stdout or "") + (proc.stderr or "")
            for line in out.splitlines():
                if line.strip():
                    self.log(f"[云文档授权] {line.strip()}")
            if cli.authenticated():
                self.log("[云文档授权] 授权成功", "OK")
            else:
                self.log("[云文档授权] 未检测到有效授权，请重试", "WARN")
        except Exception as exc:  # noqa: BLE001
            self.log(f"[云文档授权] 失败：{type(exc).__name__}: {exc}", "ERROR")

    def _wps_authorize_worker_android(self) -> None:
        """APK 内置模式：Kotlin 负责 proot/kdocs-cli/Custom Tabs，Python 只写日志。"""
        def on_url(url: str) -> None:
            # 授权 URL 只包含 client_id / state，用户需要看到链接或自动拉起浏览器；
            # 这里不打印 code，token 更不会出现在日志里。
            self.log(f"[云文档授权] 已请求系统浏览器打开授权页：{url}")

        try:
            result = android_runtime.authorize(timeout_ms=330_000, on_url=on_url)
            if result.ok:
                self.log("[云文档授权] 授权成功", "OK")
            else:
                detail = result.message or "未检测到有效授权，请重试"
                code = f"（{result.error_code}）" if result.error_code else ""
                self.log(f"[云文档授权] 失败{code}：{detail}", "WARN")
        except Exception as exc:  # noqa: BLE001
            self.log(f"[云文档授权] 失败：{type(exc).__name__}: {exc}", "ERROR")

    @staticmethod
    def _password_clear_failure(kind: str, state: str, reason: str,
                                next_action: str) -> dict[str, Any]:
        return {
            "ok": False,
            "status": "error",
            "state": state,
            "mode": kind,
            "deleted": False,
            "reason": reason,
            "next_action": next_action,
            "summary": {"mode": kind, "state": state},
        }

    def _clear_password_for(self, kind: str, account: str) -> dict[str, Any]:
        """清除单把密码；失败/无法确认绝不返回 ok=true。"""
        account = str(account or "").strip()
        label = "闪时送" if kind == "sss" else "管理后台"
        if not account:
            self.log(f"账号为空，无需清除本机保存的{label}密码")
            return {
                "ok": True, "status": "no_change", "state": "account_empty",
                "mode": kind, "deleted": False,
                "reason": "账号为空，本机没有可清除的密码",
                "next_action": "",
                "summary": {"mode": kind, "state": "account_empty"},
            }
        key = (kind, account)
        with self._password_clear_lock:
            already_cleared = key in self._cleared_password_accounts
        delete_fn = delete_sss_password if kind == "sss" else delete_password
        getter = get_sss_password if kind == "sss" else get_password
        try:
            deleted = bool(delete_fn(account))
        except Exception as exc:  # noqa: BLE001 - 凭据后端异常不能泄漏内容
            self.log(f"清除本机保存的{label}密码失败：{type(exc).__name__}", "WARN")
            return self._password_clear_failure(
                kind, "delete_error",
                f"清除密码时凭据后端异常（{type(exc).__name__}）",
                "检查系统密钥链权限/可用性后重试；不要把失败当作已清除")
        if deleted:
            with self._password_clear_lock:
                self._cleared_password_accounts.add(key)
            self.log("已清除本机保存的闪时送密码" if kind == "sss"
                     else "已清除本机保存的密码")
            return {
                "ok": True, "status": "success", "state": "deleted",
                "mode": kind, "deleted": True, "reason": "",
                "next_action": "",
                "summary": {"mode": kind, "state": "deleted"},
            }

        # 返回 False：再只读探测是否仍有可读值，以区分“仍保存”与“无法确认”。
        remaining: Any = None
        read_failed = False
        try:
            remaining = getter(account)
        except Exception:  # noqa: BLE001
            read_failed = True
        if remaining:
            self.log(f"清除本机保存的{label}密码失败：凭据仍可读", "WARN")
            return self._password_clear_failure(
                kind, "delete_failed",
                "清除密码失败：本机凭据存储仍能读到已保存的密码",
                "检查系统密钥链状态后重试；不要把失败当作已清除")
        if read_failed:
            self.log(f"清除本机保存的{label}密码失败：无法确认凭据状态", "WARN")
            return self._password_clear_failure(
                kind, "delete_unconfirmed",
                "清除密码返回失败，且无法确认凭据存储状态",
                "检查系统密钥链权限/可用性后重试；不要把失败当作已清除")
        if already_cleared:
            self.log(f"本机保存的{label}密码此前已清除，无需重复操作")
            return {
                "ok": True, "status": "no_change", "state": "already_cleared",
                "mode": kind, "deleted": False,
                "reason": "该密码在此前已经成功清除",
                "next_action": "",
                "summary": {"mode": kind, "state": "already_cleared"},
            }
        self.log(f"清除本机保存的{label}密码未成功：无法区分原本不存在与后端不可用", "WARN")
        return self._password_clear_failure(
            kind, "already_absent_or_unavailable",
            "删除接口返回 False，当前无法确认是原本不存在还是凭据后端不可用",
            "请在系统凭据管理器中确认；确认不存在后再重试清除")

    def clear_password(self, mode: str = "order") -> dict[str, Any]:
        """删除本机密钥链里保存的密码。

        ``mode="sss"`` 删闪时送那把，**其余取值一律删管理后台那把**。
        仅当删除函数明确返回成功，或账号为空、或本 Bridge 进程内已成功清除过，
        才返回 ``ok=True``；删除失败/无法确认返回明确 ``reason/next_action``，
        且不会写入密码内容。
        """
        if mode == "sss":
            return self._clear_password_for("sss", self._config.sss_account)
        return self._clear_password_for("order", self._config.phone_number)

    def check_updates(self, manual: bool = False) -> dict[str, Any]:
        """启动更新检查（后台线程）。正在安装时拒绝，避免状态互相覆盖。

        也参与统一互斥：检查期间不接受订单/闪时送/云上传等操作，检查本身
        也不会在订单任务运行时启动。返回值保持旧协议 ``{"ok": True}``。
        """
        if self._update_installing:
            return {"ok": False, "reason": "installing",
                    "message": "正在下载/安装更新，请稍候"}
        if self._update_checking:
            return {"ok": False, "reason": "already_checking"}
        reservation = self._operations.try_reserve(
            "check_update", summary={"title": "检查更新", "manual": bool(manual)},
            next_action="等待检查更新完成")
        if not reservation.granted:
            return self._operation_conflict_payload(
                reservation.conflict, action="检查更新")
        operation = reservation.operation
        assert operation is not None
        self._update_check_operation_id = operation.operation_id
        self._update_checking = True
        self._update_prev_status = self._status
        if self._status not in ("running", "stopping"):
            self._set_status("updating")
        self.log("正在检查更新...")
        try:
            threading.Thread(target=self._check_updates_worker,
                             args=(bool(manual),), daemon=True).start()
        except Exception:
            self._update_checking = False
            self._update_check_operation_id = ""
            self._operations.finish_if_active(
                operation, status="error", reason="check_start_failed",
                next_action="查看日志后重试")
            raise
        return {"ok": True}

    def _installed_app_version(self) -> str:
        """返回当前安装包的真实版本；非 Android 时退回 Python 版本号。"""
        if android_runtime.is_android():
            capabilities = android_runtime.update_capabilities()
            version = str(capabilities.get("versionName") or "").strip()
            if version:
                return version
        return __version__

    def _restore_after_update_task(self) -> None:
        """检查/安装结束后恢复进入前的状态（检查更新不算真的“正在更新”）。"""
        if self._status != "updating":
            return
        previous = self._update_prev_status
        self._set_status(previous if previous and previous != "updating" else "ready")

    def _check_updates_worker(self, manual: bool) -> None:
        ok = True
        current_version = self._installed_app_version()
        android_mode = android_runtime.is_android()
        try:
            release = check_for_update(current_version=current_version)
            if release:
                apk = select_android_apk(release) if android_mode else None
                if android_mode and apk is None:
                    self.log(f"发现新版本 {release.tag_name}，"
                             "但 Release 缺少 Android APK 资产，暂不可安装", "WARN")
                    if manual:
                        self._emit_event("update:error", {
                            "code": "no_apk",
                            "message": "发现新版本，但该 Release 没有 Android 安装包",
                        })
                else:
                    payload: dict[str, Any] = {
                        "tag": release.tag_name,
                        "current": current_version,
                        "body": release.body or "（暂无更新说明）",
                        "html_url": release.release_url,
                        "can_install": bool(android_mode and apk is not None),
                    }
                    if apk is not None:
                        payload.update({
                            "asset_name": apk.name,
                            "size": int(apk.size or 0),
                        })
                    self._emit_event("update:available", payload)
            elif manual:
                self._emit_event("update:latest", {"manual": True, "current": current_version})
            else:
                self.log("当前已是最新版本")
        except ReleaseCheckError as exc:
            ok = False
            self._set_status("error")
            self._emit_event("update:error", {"code": "check_failed", "message": str(exc)})
        finally:
            operation_id = self._update_check_operation_id
            self._update_check_operation_id = ""
            self._update_checking = False
            if ok:
                self._restore_after_update_task()
            self._operations.finish_if_active(
                operation_id,
                status="success" if ok else "error",
                reason="" if ok else "check_failed",
                next_action="" if ok else "查看日志后重试")

    def _emit_update_progress(self, phase: str, percent: int, *,
                              downloaded: int | None = None,
                              total: int | None = None,
                              message: str = "") -> None:
        self._update_phase = phase
        payload: dict[str, Any] = {
            "phase": phase,
            "percent": max(0, min(100, int(percent))),
        }
        if downloaded is not None:
            payload["downloaded"] = max(0, int(downloaded))
        if total is not None:
            payload["total"] = max(0, int(total))
        if message:
            payload["message"] = message
        self._emit_event("update:progress", payload)

    def install_update(self) -> dict[str, Any]:
        """准备应用内更新：Android APK 模式下下载并拉起系统安装器。

        会先检查平台、任务状态、未知来源安装权限；真正下载在后台线程执行，
        进度通过 ``update:progress`` 事件回传。
        """
        if self._update_installing:
            return {"ok": False, "reason": "already_installing",
                    "message": "正在下载或安装更新，请稍候"}
        if self._update_checking:
            return {"ok": False, "reason": "checking",
                    "message": "正在检查更新，请稍候"}
        if not android_runtime.is_android():
            return {"ok": False, "reason": "unsupported_platform",
                    "message": "当前不是 APK 模式，无法应用内安装更新"}
        if self.worker_alive():
            return {"ok": False, "reason": "busy",
                    "message": "任务正在运行，请先停止任务再更新"}
        capabilities = android_runtime.update_capabilities()
        if not capabilities.get("ok"):
            return {"ok": False, "reason": "native_unavailable",
                    "message": str(capabilities.get("message") or "Android 更新组件不可用")}
        if not capabilities.get("canInstall"):
            return {"ok": False, "reason": "permission_required",
                    "message": "请先允许本应用安装「未知来源应用」"}

        reservation = self._operations.try_reserve(
            "install_update", summary={"title": "安装更新"},
            next_action="等待下载/安装完成")
        if not reservation.granted:
            return self._operation_conflict_payload(
                reservation.conflict, action="安装更新")
        operation = reservation.operation
        assert operation is not None
        self._update_install_operation_id = operation.operation_id
        try:
            self._update_installing = True
            self._update_cancel.clear()
            self._update_phase = "checking"
            self._update_prev_status = self._status
            if self._status not in ("running", "stopping"):
                self._set_status("updating")
            self.log("开始准备更新...")
            threading.Thread(target=self._install_update_worker,
                             args=(capabilities,), daemon=True).start()
        except Exception:
            self._update_installing = False
            self._update_install_operation_id = ""
            self._operations.finish_if_active(
                operation, status="error", reason="install_start_failed",
                next_action="查看日志后重试")
            raise
        return {"ok": True}

    def cancel_update(self) -> dict[str, Any]:
        """取消正在进行的下载；已经交给系统安装器后无法取消。"""
        if not self._update_installing:
            return {"ok": True}
        if self._update_phase in ("checking", "downloading"):
            self._update_cancel.set()
            self.log("正在取消更新下载...")
        return {"ok": True}

    def open_install_settings(self) -> dict[str, Any]:
        """打开系统「安装未知应用」设置页。"""
        if not android_runtime.is_android():
            return {"ok": False, "reason": "unsupported_platform"}
        return {"ok": bool(android_runtime.open_install_permission_settings())}

    def _install_update_worker(self, capabilities: dict[str, Any]) -> None:
        worker_status = "success"
        try:
            current_version = str(capabilities.get("versionName") or __version__)
            current_code = int(capabilities.get("versionCode") or 0)
            release = check_for_update(current_version=current_version)
            if release is None:
                self._emit_event("update:latest", {"manual": True, "current": current_version})
                return

            apk = select_android_apk(release)
            if apk is None:
                worker_status = "error"
                self._emit_event("update:error", {
                    "code": "no_apk",
                    "message": "该版本没有 Android 安装包，无法自动更新",
                })
                return

            expected_sha = ""
            sha_asset = select_sha256_asset(release, apk)
            if sha_asset is not None:
                try:
                    expected_sha = parse_sha256(read_asset_text(sha_asset))
                except ReleaseCheckError as exc:
                    self.log(f"[更新] 校验文件读取失败：{exc}", "WARN")
            else:
                self.log("[更新] Release 缺少 sha256 校验文件，将依赖 APK 签名校验", "WARN")

            cache_root = android_runtime.cache_dir()
            if cache_root is None:
                worker_status = "error"
                self._emit_event("update:error", {
                    "code": "cache_unavailable",
                    "message": "无法获取应用缓存目录，更新已取消",
                })
                return
            destination = cache_root / "updates" / f"yikou-{release.version}-arm64.apk"

            total_hint = int(apk.size or 0)
            last_emit = [0.0]

            def on_progress(downloaded: int, total: int) -> None:
                resolved = total or total_hint
                now = time.monotonic()
                # 避免 19MB 下载产生几百条关键事件：约每 250ms 或整 5% 上报一次。
                if downloaded < resolved and now - last_emit[0] < 0.25:
                    return
                last_emit[0] = now
                percent = int(downloaded * 100 / resolved) if resolved > 0 else 0
                self._emit_update_progress("downloading", percent,
                                           downloaded=downloaded, total=resolved)

            self._emit_update_progress("downloading", 0, downloaded=0, total=total_hint,
                                       message="正在下载安装包…")
            self.log(f"正在下载 {apk.name}...")
            try:
                download_asset(
                    apk,
                    destination,
                    expected_sha256=expected_sha,
                    expected_size=apk.size or None,
                    on_progress=on_progress,
                    cancel_check=self._update_cancel.is_set,
                )
            except UpdateCancelled:
                self.log("已取消下载更新")
                worker_status = "stopped"
                self._emit_event("update:cancelled", {"message": "已取消更新下载"})
                return

            self._emit_update_progress("verifying", 100, downloaded=apk.size,
                                       total=apk.size, message="正在校验安装包…")
            self.log("正在校验安装包...")
            verify = android_runtime.verify_update_apk(str(destination))
            if not verify.get("ok"):
                try:
                    destination.unlink(missing_ok=True)
                except OSError:
                    pass
                worker_status = "error"
                self._emit_event("update:error", {
                    "code": str(verify.get("code") or "verify_failed"),
                    "message": str(verify.get("message") or "安装包校验失败"),
                })
                return
            if not verify.get("sameSignature"):
                worker_status = "error"
                self._emit_event("update:error", {
                    "code": "signature_mismatch",
                    "message": "安装包签名与当前应用不一致，已拒绝安装"
                               "（请确认新版使用同一把签名证书）",
                })
                return
            remote_code = int(verify.get("versionCode") or 0)
            if remote_code and current_code and remote_code <= current_code:
                self._emit_event("update:latest", {"manual": True, "current": current_version})
                return

            self._emit_update_progress("installing", 100, downloaded=apk.size,
                                       total=apk.size,
                                       message="已打开系统安装器，请按手机提示完成安装")
            self.log("安装包校验通过，正在拉起系统安装器...", "OK")
            result = android_runtime.install_apk(str(destination))
            if not result.get("ok"):
                code = str(result.get("code") or "install_failed")
                message = str(result.get("message") or "拉起系统安装器失败")
                if code in ("permission_required", "unknown_sources"):
                    worker_status = "error"
                    self._emit_event("update:permission_required", {"message": message})
                else:
                    worker_status = "error"
                    self._emit_event("update:error", {"code": code, "message": message})
                return
            self.log("已拉起系统安装器，请在手机弹窗中完成安装", "OK")
        except ReleaseCheckError as exc:
            worker_status = "error"
            self._emit_event("update:error", {"code": "check_failed", "message": str(exc)})
        except Exception as exc:  # noqa: BLE001
            logger.exception("update install failed")
            self.log(f"[更新] 失败：{type(exc).__name__}: {exc}", "ERROR")
            worker_status = "error"
            self._emit_event("update:error", {
                "code": "update_failed",
                "message": f"{type(exc).__name__}: {exc}",
            })
        finally:
            operation_id = self._update_install_operation_id
            self._update_install_operation_id = ""
            self._update_installing = False
            self._update_cancel.clear()
            self._update_phase = ""
            self._restore_after_update_task()
            self._operations.finish_if_active(
                operation_id, status=worker_status,
                reason="" if worker_status == "success" else "update_failed",
                next_action="" if worker_status == "success"
                else "查看更新日志后重试")

    def open_external(self, url: str) -> dict[str, Any]:
        """用系统默认程序打开外链。

        **协议白名单**：只放行 ``http://`` 与 ``https://``，其余（含 ``file://``）一律忽略。
        恒返回 ``{"ok": True}``。
        """
        if isinstance(url, str) and url.startswith(("https://", "http://")):
            if android_runtime.is_android():
                return {"ok": android_runtime.open_external(url)}
            webbrowser.open(url)
        return {"ok": True}

    # ------------------------------------------------------------------
    # js_api：窗口动作与关闭保护
    # ------------------------------------------------------------------
    def request_close(self) -> dict[str, Any]:
        """「停止服务」入口：有任务运行时先确认，空闲时关闭服务进程。"""
        if self.worker_alive():
            # 先唤醒 captcha/普通 decision，避免它们继续阻塞 worker；保留
            # close_confirm 自身，防止重复点击关闭时自唤醒。
            self._cancel_pending_interactions("请求关闭窗口", except_kinds=frozenset({"close_confirm"}))
            choice = self._request_decision(
                "close_confirm", "正在处理",
                "任务仍在运行。停止并关闭，还是继续处理？",
                [{"value": "stop_and_close", "label": "停止并关闭", "style": "danger"},
                 {"value": "keep", "label": "继续处理", "style": "primary"},
                 {"value": "cancel", "label": "取消", "style": "neutral"}])
            if choice != "stop_and_close":
                return {"action": "kept"}
            self._stop_and_close()
            return {"action": "accepted"}
        self._closing = True
        threading.Thread(target=self._destroy_soon, daemon=True).start()
        return {"action": "accepted"}

    def _stop_and_close(self) -> None:
        self._closing = True
        self._stop_event.set()
        self._cancel_pending_interactions("停止并关闭")
        self.log("正在停止任务并关闭服务，请稍候...")
        def watcher() -> None:
            while self._worker is not None and self._worker.is_alive():
                time.sleep(0.1)
            try:
                if self._window is not None:
                    self._window.destroy()
            except Exception:
                pass
        threading.Thread(target=watcher, daemon=True).start()

    def _destroy_soon(self) -> None:
        # 让 request_close 的返回值先送达前端再销毁窗口。
        time.sleep(0.05)
        try:
            if self._window is not None:
                self._window.destroy()
        except Exception:
            pass

    def set_split_ratio(self, ratio: float) -> dict[str, Any]:
        """设置并落盘界面分隔比例（经 :func:`clamp_split_ratio` 夹紧）。"""
        self._config.split_ratio = clamp_split_ratio(ratio)
        try:
            self._config.save()
        except OSError:
            pass
        return {"ok": True, "ratio": self._config.split_ratio}


# ----------------------------------------------------------------------
# 模块级工具
# ----------------------------------------------------------------------
def _excel_field_error(path: str) -> str:
    """Excel 字段校验：路径非空、文件存在、后缀为 .xlsx/.xlsm。"""
    if not path:
        return "请选择存在的 Excel 文件"
    candidate = _Path(path)
    if not candidate.is_file():
        return "请选择存在的 Excel 文件"
    if candidate.suffix.lower() not in EXCEL_EXTS:
        return "请选择 .xlsx 或 .xlsm 文件"
    return ""


def _with_excel_suffix(path: _Path) -> _Path:
    if path.suffix.lower() not in EXCEL_EXTS:
        return path.with_suffix(".xlsx")
    return path


def _apply_order_payload(cfg: AppConfig, p: dict[str, Any]) -> None:
    """就地更新订单处理侧配置字段（不触碰闪时送侧配置）。"""
    cfg.target_url = str(p.get("url", cfg.target_url) or "").strip()
    cfg.phone_number = str(p.get("phone", cfg.phone_number) or "").strip()
    excel = str(p.get("excel", "") or "").strip()
    if excel:
        cfg.excel_path = _Path(excel)
    cfg.order_date = str(p.get("date", cfg.order_date) or "").strip()
    if "count" in p:
        cfg.order_count = p["count"]  # None 表示「处理全部」，其余为 int|None


def _apply_sss_payload(cfg: AppConfig, p: dict[str, Any]) -> None:
    """就地更新闪时送侧配置字段（不触碰订单处理侧配置）。

    空值如实覆盖（用户清空输入就保存为空），与订单侧一致；真正开始下单时
    start_sss 会做非空校验，防抖保存本身不做启动校验——但带 ``url`` 的保存
    必须先通过闪时送网址校验（R8-S1：非规范写法不得落盘成另一个 authority
    scope），不合法时抛 ``SssUrlConfigError`` 由调用方转成明确错误。
    """
    if "url" in p:
        candidate = str(p.get("url") or "").strip()
        if candidate:
            canonical_sss_origin(candidate)
        cfg.sss_url = candidate
    cfg.sss_account = str(p.get("account", cfg.sss_account) or "").strip()
    excel = str(p.get("excel", "") or "").strip()
    if excel:
        cfg.sss_excel_path = _Path(excel)
    if "order_source" in p:
        source = str(p.get("order_source") or "").strip().lower()
        cfg.sss_order_source = "excel" if source == "excel" else "wps"
    cfg.sss_product_name = str(p.get("product_name", cfg.sss_product_name) or "").strip()
    cfg.sss_common_address = str(p.get("common_address", cfg.sss_common_address) or "").strip()
    use_fixed = bool(p.get("use_fixed_address", cfg.sss_use_fixed_address))
    cfg.sss_use_fixed_address = use_fixed
    if use_fixed:
        try:
            cfg.sss_fixed_lnt = float(p.get("fixed_lnt", cfg.sss_fixed_lnt))
        except (TypeError, ValueError):
            pass
        try:
            cfg.sss_fixed_lat = float(p.get("fixed_lat", cfg.sss_fixed_lat))
        except (TypeError, ValueError):
            pass
        cfg.sss_fixed_area_code = str(p.get("fixed_area_code", cfg.sss_fixed_area_code) or "").strip()
        cfg.sss_fixed_address_detail = str(p.get("fixed_address_detail", cfg.sss_fixed_address_detail) or "").strip()
    cfg.sss_dry_run = bool(p.get("dry_run", cfg.sss_dry_run))
    cfg.sss_preflight = bool(p.get("preflight", cfg.sss_preflight))
