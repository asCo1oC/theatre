"""Логирование ошибок и информации в файлы."""
from __future__ import annotations

import logging
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional


class ColoredFormatter(logging.Formatter):
    """Форматер с цветами для консоли."""
    
    COLORS = {
        'DEBUG': '\033[36m',      # Cyan
        'INFO': '\033[32m',       # Green
        'WARNING': '\033[33m',    # Yellow
        'ERROR': '\033[31m',      # Red
        'CRITICAL': '\033[41m',   # Red background
    }
    RESET = '\033[0m'
    
    def format(self, record):
        log_color = self.COLORS.get(record.levelname, self.RESET)
        record.levelname = f"{log_color}{record.levelname}{self.RESET}"
        return super().format(record)


def setup_error_logging(log_dir: str | Path = "./logs") -> dict:
    """Инициализирует логирование в файлы и консоль.
    
    Args:
        log_dir: Директория для логов
        
    Returns:
        Словарь с логгерами для разных уровней
    """
    log_dir = Path(log_dir)
    log_dir.mkdir(exist_ok=True)
    
    # Создаём поддиректории
    (log_dir / "errors").mkdir(exist_ok=True)
    (log_dir / "warnings").mkdir(exist_ok=True)
    (log_dir / "info").mkdir(exist_ok=True)
    
    today = datetime.now().strftime("%Y-%m-%d")
    
    loggers = {}
    
    # ERROR логгер
    error_logger = logging.getLogger("theatre.errors")
    error_logger.setLevel(logging.ERROR)
    error_handler = logging.FileHandler(log_dir / "errors" / f"{today}.log", encoding="utf-8")
    error_handler.setFormatter(logging.Formatter(
        "[%(asctime)s] %(levelname)s - %(name)s\n%(message)s\n" + "="*80 + "\n"
    ))
    error_logger.addHandler(error_handler)
    loggers["error"] = error_logger
    
    # WARNING логгер
    warning_logger = logging.getLogger("theatre.warnings")
    warning_logger.setLevel(logging.WARNING)
    warning_handler = logging.FileHandler(log_dir / "warnings" / f"{today}.log", encoding="utf-8")
    warning_handler.setFormatter(logging.Formatter(
        "[%(asctime)s] %(levelname)s - %(name)s\n%(message)s\n"
    ))
    warning_logger.addHandler(warning_handler)
    loggers["warning"] = warning_logger
    
    # INFO логгер (для важной информации)
    info_logger = logging.getLogger("theatre.info")
    info_logger.setLevel(logging.INFO)
    info_handler = logging.FileHandler(log_dir / "info" / f"{today}.log", encoding="utf-8")
    info_handler.setFormatter(logging.Formatter(
        "[%(asctime)s] %(levelname)s - %(name)s\n%(message)s\n"
    ))
    info_logger.addHandler(info_handler)
    loggers["info"] = info_logger
    
    # Консольный логгер
    console_logger = logging.getLogger("theatre.console")
    console_logger.setLevel(logging.DEBUG)
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(ColoredFormatter(
        "[%(asctime)s] %(levelname)s - %(message)s",
        datefmt="%H:%M:%S"
    ))
    console_logger.addHandler(console_handler)
    loggers["console"] = console_logger
    
    return loggers


_loggers: Optional[dict] = None


def _get_loggers():
    """Получить или инициализировать логгеры."""
    global _loggers
    if _loggers is None:
        _loggers = setup_error_logging()
    return _loggers


def log_error(message: str, exception: Optional[Exception] = None, context: Optional[dict] = None) -> None:
    """Логировать ошибку.
    
    Args:
        message: Сообщение об ошибке
        exception: Исключение (если есть)
        context: Контекст ошибки (словарь с доп. информацией)
    """
    loggers = _get_loggers()
    full_message = message
    
    if context:
        full_message += "\n\nКонтекст:\n"
        for key, value in context.items():
            full_message += f"  {key}: {value}\n"
    
    if exception:
        full_message += f"\n\nИсключение:\n{type(exception).__name__}: {exception}"
        full_message += f"\n\nТрассировка:\n"
        import traceback
        full_message += traceback.format_exc()
    
    loggers["error"].error(full_message)
    loggers["console"].error(f"❌ {message}")


def log_warning(message: str, context: Optional[dict] = None) -> None:
    """Логировать предупреждение."""
    loggers = _get_loggers()
    full_message = message
    
    if context:
        full_message += "\nКонтекст: " + str(context)
    
    loggers["warning"].warning(full_message)
    loggers["console"].warning(f"⚠️ {message}")


def log_info(message: str, context: Optional[dict] = None) -> None:
    """Логировать информацию."""
    loggers = _get_loggers()
    full_message = message
    
    if context:
        full_message += "\nДетали: " + str(context)
    
    loggers["info"].info(full_message)
    loggers["console"].info(f"ℹ️ {message}")
