"""WPS 云同步的异常类型。"""

from __future__ import annotations


class WpsCloudError(RuntimeError):
    """云文档同步过程中的可预期错误（调用方据此提示用户）。"""


class LedgerCorruptError(WpsCloudError):
    """账本不可信（损坏/截断/结构非法）——必须失败关闭，不能当空账本继续加餐。"""


class JournalError(WpsCloudError):
    """写入意图日志不可读/不可写：必须零云端写入或停止后续写入。"""


class JournalCorruptError(JournalError):
    """意图日志损坏或无法安全解析，不能当作没有未完成操作。"""
