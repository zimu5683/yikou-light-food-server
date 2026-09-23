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

_RECONCILE_POLL_ATTEMPTS = 3

_RECONCILE_POLL_INTERVAL_S = 0.5

_PREFILTER_ZERO_RETRY_DELAY_S = 2.0

_BATCH_CLOCK_SKEW_S = 120.0

_LIST_PAGE_SIZE = 100

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
