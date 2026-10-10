"""闪时送平台相关的常量、接口路径与阈值。"""

from __future__ import annotations

import os
import re
from threading import Lock


DEFAULT_SHEETS = ("午餐", "晚餐")

LUNCH_TIME = "11:00:00"

DINNER_TIME = "17:00:00"

DEFAULT_SSS_URL = "https://sssplusnew.zhuopaikeji.com/takeout"

_CREATE_ORDER_PATH = "/consumer/order/one-touch-send/create-order-from-client"

_ORDER_LIST_PATH = "/consumer/order/one-touch-send/list"

_STORE_LIST_PATH = "/consumer/customer/store/queryStoreAddresses?pageNo=1&pageSize=40"

_FREQUENT_ADDR_PATH = "/consumer/customer/customerAddress/queryFrequentAddressByCustomer"

_ACCOUNT_PATH = "/consumer/account/get-login-user-account"

_BLANK_ROWS_TO_STOP = 3

_PROGRESS_EVERY_N = 10

_DRY_RUN_PREVIEW_N = 3

#: 提交后对账的只读复查次数/间隔。3.6.15 重估：单次对账从「无过滤全量扫描」
#: （实测 69 秒）降为「时间窗查询 1-2 页」（约 2 秒），复查变便宜；而提交并发提高后
#: 收尾对账离 POST 更近，站内「刚写入还没被列表读到」的短延迟更容易撞上，所以把
#: 复查窗口从约 1.5 秒放宽到约 6 秒（只读，不重发 POST）。
_RECONCILE_POLL_ATTEMPTS = 4

_RECONCILE_POLL_INTERVAL_S = 2.0

_PREFILTER_ZERO_RETRY_DELAY_S = 2.0

_BATCH_CLOCK_SKEW_S = 120.0

#: 建单提交的最小间隔（``sss_submit_min_interval_s``）出厂默认值与夹紧范围都定义在
#: ``app.core.config`` / ``runner.resolve_submit_min_interval_s``，这里不再保留任何
#: 自适应参数：间隔只在运行期按配置生效，不因平台返回内部异常（如
#: IndexOutOfBoundsException）自动放宽——这类内部异常只代表“结果待对账”，既不是
#: 受理超速的证据，也不能被当成“没有落单”。

_LIST_PAGE_SIZE = 100

#: 带时间窗（预筛）查询的页大小。窗口内通常 100-140 条（2026-09-13 生产实测 116 条），
#: 300 可以一页取完（1 页 ≈ 1.9 秒，比 2 页 ≈ 2.8 秒省一次往返）。
#: 无过滤路径仍用 _LIST_PAGE_SIZE：pageSize=1000 实测 17.9 秒 / 1.9MB，盲目放大会更慢。
_WINDOW_PAGE_SIZE = 300

#: 时间窗自检结论的进程内缓存有效期（秒）。自检见 reconcile._verify_list_window：
#: 结论只与「服务端是否接受 startTime/endTime」有关，短时间重复探测没有意义。
_WINDOW_CHECK_TTL_S = 600.0

_SERVER_PREFILTER_MARGIN_DAYS = 1

#: 只读核对未决记录时的“宽窗”天数：站内订单的预约送达日可能被平台改到相邻日期，
#: 严格按目标日过滤会把它判成“站内没有”，从而把“落单了但日期变了”误当成“没落单”。
#: 宽窗复核只用于**报告分类**，不改变任何自动对账判定。
_REVIEW_WIDE_WINDOW_DAYS = 3

#: 只读核对证据的有效期（秒）。管理员据“站内查不到”解除阻断前，必须先有一次
#: 新鲜的只读核对；超期或 journal 已变化就要求重新核对，避免拿旧结论下单。
_SSS_REVIEW_TTL_S = 600.0

_SSS_SERVER_PREFILTER = os.environ.get(
    "YIKOU_SSS_SERVER_PREFILTER", "").strip().lower() not in ("0", "false", "no", "off")

_SERVER_PREFILTER_STATUS = 2

_CLIENT_IDEMPOTENCY_FIELD = ""

_SSS_RUN_LOCK = Lock()

_INACTIVE_ORDER_STATUS = frozenset()

_DOOR_RE = re.compile(r"\s+")

_PHONE_RE = re.compile(r"^[0-9]{11}$")

_BALANCE_KEYWORDS = ("余额", "充值", "欠费", "冻结", "钱包",
                     "balance", "insufficient", "recharge")
