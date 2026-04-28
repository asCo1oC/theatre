"""Обработчик ошибок с повторными попытками."""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Any, Callable, Optional, TypeVar

from .logger import log_error, log_warning

T = TypeVar("T")


@dataclass
class RetryConfig:
    """Конфигурация повторных попыток."""
    
    max_retries: int = 3
    initial_delay: float = 1.0  # секунды
    max_delay: float = 60.0     # секунды
    exponential_base: float = 2.0
    jitter: bool = True  # добавлять случайный шум
    
    def get_delay(self, attempt: int) -> float:
        """Вычислить задержку перед попыткой."""
        delay = self.initial_delay * (self.exponential_base ** attempt)
        delay = min(delay, self.max_delay)
        
        if self.jitter:
            import random
            delay *= (0.5 + random.random())
        
        return delay


class ErrorHandler:
    """Обработчик ошибок с поддержкой повторных попыток."""
    
    # Ошибки, которые стоит повторять
    RETRYABLE_ERRORS = (
        ConnectionError,
        TimeoutError,
        OSError,
        asyncio.TimeoutError,
    )
    
    @staticmethod
    async def with_retry(
        func: Callable[..., Any],
        *args,
        config: Optional[RetryConfig] = None,
        on_retry: Optional[Callable[[int, Exception], None]] = None,
        **kwargs
    ) -> Any:
        """Выполнить функцию с повторными попытками.
        
        Args:
            func: Функция для выполнения
            config: Конфигурация повторных попыток
            on_retry: Callback при каждой повторной попытке
            
        Returns:
            Результат функции
            
        Raises:
            Exception: Если все попытки исчерпаны
        """
        config = config or RetryConfig()
        last_exception: Optional[Exception] = None
        
        for attempt in range(config.max_retries + 1):
            try:
                if asyncio.iscoroutinefunction(func):
                    return await func(*args, **kwargs)
                else:
                    return func(*args, **kwargs)
            
            except ErrorHandler.RETRYABLE_ERRORS as exc:
                last_exception = exc
                
                if attempt == config.max_retries:
                    # Последняя попытка - бросаем исключение
                    break
                
                delay = config.get_delay(attempt)
                log_warning(
                    f"Ошибка в попытке {attempt + 1}/{config.max_retries + 1}. "
                    f"Повтор через {delay:.1f}с",
                    {"error": str(exc), "attempt": attempt + 1}
                )
                
                if on_retry:
                    on_retry(attempt + 1, exc)
                
                await asyncio.sleep(delay) if asyncio.iscoroutinefunction(func) else time.sleep(delay)
            
            except Exception as exc:
                # Не повторяемая ошибка
                log_error(
                    "Необработанная ошибка (не будет повторена)",
                    exception=exc,
                    context={"attempt": attempt + 1}
                )
                raise
        
        # Все попытки исчерпаны
        log_error(
            f"Ошибка после {config.max_retries + 1} попыток",
            exception=last_exception,
            context={"function": func.__name__}
        )
        raise last_exception or RuntimeError("Unknown error")
    
    @staticmethod
    def safe_execute(
        func: Callable[..., T],
        *args,
        default: Optional[T] = None,
        log_error_msg: str = "Ошибка при выполнении функции",
        **kwargs
    ) -> Optional[T]:
        """Безопасное выполнение функции без повторных попыток.
        
        Args:
            func: Функция для выполнения
            default: Значение по умолчанию при ошибке
            log_error_msg: Сообщение об ошибке
            
        Returns:
            Результат функции или default
        """
        try:
            return func(*args, **kwargs)
        except Exception as exc:
            log_error(log_error_msg, exception=exc)
            return default
