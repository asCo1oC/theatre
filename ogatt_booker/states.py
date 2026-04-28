"""FSM states для Telegram бота."""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum, auto


class ConversationState(Enum):
    """Состояния разговора в боте."""
    
    # Главное меню
    START = auto()
    
    # Конфигурирование мониторинга
    WATCH_TITLE = auto()          # Выбор названия спектакля
    WATCH_DATE_RANGE = auto()      # Выбор диапазона дат
    WATCH_SEAT_COUNT = auto()      # Выбор количества мест
    WATCH_CONFIRM = auto()         # Подтверждение параметров
    
    # Управление статусом
    PAUSE_CONFIRM = auto()         # Подтверждение паузы
    RESUME_CONFIRM = auto()        # Подтверждение возобновления
    STOP_CONFIRM = auto()          # Подтверждение остановки
    
    # Диагностика после бронирования
    BOOKING_CONFIRMATION = auto()  # Запрос подтверждения
    BOOKING_PROBLEM = auto()       # Выбор типа проблемы
    
    # Завершение
    DONE = auto()


class CommandType(Enum):
    """Типы команд управления."""
    
    WATCH = auto()     # /watch - начать мониторинг
    PAUSE = auto()     # /pause - приостановить
    RESUME = auto()    # /resume - возобновить
    STOP = auto()      # /stop - остановить
    CHANGE = auto()    # /change - изменить параметры
    SLEEP = auto()     # /sleep - режим сна


@dataclass
class ConversationContext:
    """Контекст разговора с пользователем."""
    
    user_id: int
    chat_id: int
    state: ConversationState
    command: CommandType | None = None
    
    # Параметры для /watch
    title: str | None = None
    date_range: str | None = None
    seat_count: int | None = None
    
    # Для диагностики проблем
    problem_type: str | None = None  # "bot_error" или "user_timeout"
    
    def __hash__(self) -> int:
        """Сделать контекст hashable для использования как ключ."""
        return hash((self.user_id, self.chat_id))
    
    def __eq__(self, other: object) -> bool:
        """Сравнение контекстов."""
        if not isinstance(other, ConversationContext):
            return False
        return self.user_id == other.user_id and self.chat_id == other.chat_id
