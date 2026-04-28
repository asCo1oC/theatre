"""Система обработки ошибок и логирования."""
from __future__ import annotations

from .logger import setup_error_logging, log_error, log_warning, log_info
from .handler import ErrorHandler, RetryConfig

__all__ = [
    "setup_error_logging",
    "log_error",
    "log_warning",
    "log_info",
    "ErrorHandler",
    "RetryConfig",
]
