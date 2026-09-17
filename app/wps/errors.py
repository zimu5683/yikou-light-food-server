"""WPS 云同步的异常类型。"""

from __future__ import annotations


class WpsCloudError(RuntimeError):
    """云文档同步过程中的可预期错误（调用方据此提示用户）。"""
