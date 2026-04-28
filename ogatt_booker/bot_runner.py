"""Интеграция Telegram бота с системой мониторинга.

Этот модуль объединяет автоматическое бронирование с управлением через Telegram.
"""
from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Optional

from .bot_main import TelegramBotApp
from .errors import log_error, log_info
from .models import Session
from .qt import Contacts
from .storage import SQLiteStorage
from .watcher_managed import ManagedWatcher


async def run_integrated_system(
    tg_token: Optional[str] = None,
    title: Optional[str] = None,
    seats_wanted: int = 2,
    date_range: Optional[str] = None,
    contacts: Optional[Contacts] = None,
    artifact_dir: Path = Path("./artifacts"),
    headful: bool = False,
    max_sessions: Optional[int] = None,
    user_id: int = 0,  # Для тестирования
    chat_id: int = 0,  # Для тестирования
) -> None:
    """Запустить интегрированную систему бот + мониторинг.
    
    Параметры:
        tg_token: Telegram Bot Token (или из TELEGRAM_BOT_TOKEN)
        title: Название спектакля (опционально, через /watch)
        seats_wanted: Количество мест
        date_range: Диапазон дат
        contacts: Контактные данные
        artifact_dir: Директория для артефактов
        headful: Показывать браузер
        max_sessions: Максимум сеансов
        user_id: ID пользователя для тестирования
        chat_id: ID чата для тестирования
    
    Использование:
        # В режиме бота (управление через /watch команды)
        python -m ogatt_booker.bot_runner
        
        # С начальными параметрами (для тестирования)
        python -m ogatt_booker.bot_runner --title "Война и мир" --seats 2
    """
    # Получаем токен (поддерживаем оба имена переменных)
    token = tg_token or os.getenv("TG_BOT_TOKEN") or os.getenv("TELEGRAM_BOT_TOKEN")
    if not token:
        log_error("TG_BOT_TOKEN или TELEGRAM_BOT_TOKEN не установлен", context={})
        return
    
    log_info("Инициализация интегрированной системы", {})
    
    # Создаем хранилище
    storage = SQLiteStorage()
    
    # Создаем бота
    app = TelegramBotApp(token, storage)
    
    # Если указаны параметры - запускаем мониторинг
    if title and user_id and chat_id:
        log_info(
            "Запуск мониторинга с начальными параметрами",
            {"title": title, "user_id": user_id}
        )
        
        # Сохраняем параметры
        storage.set_watching_state(
            user_id=user_id,
            status="active",
            title=title,
            date_range=date_range or "все даты",
            seats_count=seats_wanted,
        )
        
        # Создаем managed watcher
        watcher = ManagedWatcher(app.bot, storage)
        
        # Запускаем бота и вотчер параллельно
        bot_task = asyncio.create_task(app.start_polling())
        watcher_task = asyncio.create_task(
            watcher.watch(
                user_id=user_id,
                chat_id=chat_id,
                title=title,
                seats_wanted=seats_wanted,
                date_range=date_range,
                contacts=contacts,
                artifact_dir=artifact_dir,
                headful=headful,
                max_sessions=max_sessions,
            )
        )
        
        try:
            await asyncio.gather(bot_task, watcher_task)
        except KeyboardInterrupt:
            log_info("Система остановлена пользователем", {})
            bot_task.cancel()
            watcher_task.cancel()
        finally:
            await app.close()
    
    else:
        # Только бот в режиме ожидания команд
        log_info("Запуск бота в режиме команд", {})
        try:
            await app.start_polling()
        except KeyboardInterrupt:
            log_info("Бот остановлен пользователем", {})
        finally:
            await app.close()


if __name__ == "__main__":
    import argparse
    
    parser = argparse.ArgumentParser(description="Интегрированная система мониторинга+бот")
    parser.add_argument("--title", help="Название спектакля")
    parser.add_argument("--seats", type=int, default=2, help="Количество мест")
    parser.add_argument("--dates", help="Диапазон дат")
    parser.add_argument("--headful", action="store_true", help="Показывать браузер")
    parser.add_argument("--max-sessions", type=int, help="Максимум сеансов")
    parser.add_argument("--user-id", type=int, default=0, help="ID пользователя (тест)")
    parser.add_argument("--chat-id", type=int, default=0, help="ID чата (тест)")
    parser.add_argument("--token", help="Telegram Bot Token")
    
    args = parser.parse_args()
    
    asyncio.run(run_integrated_system(
        tg_token=args.token,
        title=args.title,
        seats_wanted=args.seats,
        date_range=args.dates,
        headful=args.headful,
        max_sessions=args.max_sessions,
        user_id=args.user_id,
        chat_id=args.chat_id,
    ))
