"""Главное приложение Telegram бота для управления мониторингом."""
from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path

import httpx

from .errors import log_error, log_info, log_warning
from .handlers import (
    clear_conversation_context,
    get_conversation_context,
    handle_booking_confirm_no,
    handle_booking_confirm_yes,
    handle_change,
    handle_pause,
    handle_problem_bot_error,
    handle_problem_user_timeout,
    handle_resume,
    handle_skip_date_range,
    handle_sleep,
    handle_stop,
    handle_watch_confirm_no,
    handle_watch_confirm_yes,
    handle_watch_date_range_received,
    handle_watch_seat_count_received,
    handle_watch_start,
    handle_watch_title_received,
)
from .telegram_bot import TelegramBot, handle_help, handle_start, handle_status
from .states import ConversationState
from .storage import SQLiteStorage
from .watcher_managed import ManagedWatcher

log = logging.getLogger(__name__)


class TelegramBotApp:
    """Главное приложение бота с обработкой обновлений."""
    
    def __init__(self, token: str, storage: SQLiteStorage | None = None):
        self.token = token
        self.storage = storage or SQLiteStorage()
        self.bot = TelegramBot(token, self.storage)
        self.watcher = ManagedWatcher(self.bot, self.storage)
        self.client = httpx.AsyncClient(timeout=30)
        self.last_update_id = 0
        self.watch_tasks: dict[int, asyncio.Task] = {}
    
    async def start_polling(self, poll_interval: float = 1.0) -> None:
        """Запустить опрос обновлений от Telegram."""
        log_info("Бот запущен, начинаем опрос обновлений", {})
        
        try:
            while True:
                try:
                    updates = await self._get_updates()
                    
                    if updates:
                        log_info(f"Получено {len(updates)} обновлений", {})
                    
                    for update in updates:
                        await self._handle_update(update)
                
                except asyncio.TimeoutError:
                    log_warning("Таймаут при получении обновлений", {})
                    await asyncio.sleep(poll_interval)
                except Exception as exc:
                    log_error("Ошибка при обработке обновлений", exception=exc, context={})
                    await asyncio.sleep(poll_interval)
                
                await asyncio.sleep(poll_interval)
        
        finally:
            await self.client.aclose()
    
    async def _get_updates(self) -> list[dict]:
        """Получить обновления от Telegram."""
        try:
            response = await self.client.post(
                f"https://api.telegram.org/bot{self.token}/getUpdates",
                json={"offset": self.last_update_id + 1, "timeout": 30},
                timeout=35,
            )
            
            if response.status_code != 200:
                log_error(
                    f"Telegram API error: {response.status_code}",
                    context={"response": response.text[:200]}
                )
                return []
            
            data = response.json()
            
            if not data.get("ok"):
                log_error(
                    "Telegram API returned error",
                    context={"error": data.get("description")}
                )
                return []
            
            updates = data.get("result", [])
            
            if updates:
                self.last_update_id = updates[-1]["update_id"]
            
            return updates
        
        except httpx.RequestError as exc:
            log_error("Network error getting updates", exception=exc, context={})
            return []
    
    async def _handle_update(self, update: dict) -> None:
        """Обработать одно обновление."""
        # Обработка текстовых сообщений
        if "message" in update and "text" in update["message"]:
            await self._handle_message(update["message"])
        
        # Обработка нажатий на кнопки
        elif "callback_query" in update:
            await self._handle_callback(update["callback_query"])
    
    async def _handle_message(self, message: dict) -> None:
        """Обработать текстовое сообщение."""
        try:
            user_id = message["from"]["id"]
            chat_id = message["chat"]["id"]
            text = message.get("text", "").strip()
            
            if not text:
                return
            
            # Команды
            if text == "/start":
                await handle_start(self.bot, chat_id)
            
            elif text == "/help":
                await handle_help(self.bot, chat_id)
            
            elif text == "/status":
                await handle_status(self.bot, chat_id, user_id)
            
            elif text == "/watch":
                await handle_watch_start(self.bot, user_id, chat_id)
            
            elif text == "/pause":
                await handle_pause(self.bot, user_id, chat_id)
            
            elif text == "/resume":
                await handle_resume(self.bot, user_id, chat_id)
            
            elif text == "/stop":
                await handle_stop(self.bot, user_id, chat_id)
            
            elif text == "/change":
                await handle_change(self.bot, user_id, chat_id)
            
            elif text == "/sleep":
                await handle_sleep(self.bot, user_id, chat_id)
            
            # Текстовые ответы в процессе разговора
            else:
                ctx = get_conversation_context(user_id, chat_id)
                
                if ctx.state == ConversationState.WATCH_TITLE:
                    await handle_watch_title_received(self.bot, user_id, chat_id, text)
                
                elif ctx.state == ConversationState.WATCH_DATE_RANGE:
                    if text.lower() in ("skip", "пропустить"):
                        await handle_skip_date_range(self.bot, user_id, chat_id)
                    else:
                        await handle_watch_date_range_received(self.bot, user_id, chat_id, text)
                
                elif ctx.state == ConversationState.WATCH_SEAT_COUNT:
                    await handle_watch_seat_count_received(self.bot, user_id, chat_id, text)
        
        except KeyError as exc:
            log_error("Invalid message structure", exception=exc, context={"message": str(message)[:100]})
        except Exception as exc:
            log_error("Error handling message", exception=exc, context={})
    
    async def _handle_callback(self, callback: dict) -> None:
        """Обработать нажатие на кнопку."""
        try:
            user_id = callback["from"]["id"]
            chat_id = callback["message"]["chat"]["id"]
            callback_data = callback.get("data", "")
            
            log_info(
                f"Callback: {callback_data}",
                {"user_id": user_id, "chat_id": chat_id}
            )
            
            # Обработка callbacks
            if callback_data == "watch_confirm_yes":
                ctx = get_conversation_context(user_id, chat_id)
                watch_title = ctx.title
                watch_seat_count = ctx.seat_count
                watch_date_range = ctx.date_range
                await handle_watch_confirm_yes(self.bot, user_id, chat_id)
                if watch_title and watch_seat_count:
                    await self._start_watch_task(
                        user_id=user_id,
                        chat_id=chat_id,
                        title=watch_title,
                        seats_wanted=watch_seat_count,
                        date_range=watch_date_range,
                    )
            
            elif callback_data == "watch_confirm_no":
                await handle_watch_confirm_no(self.bot, user_id, chat_id)
            
            elif callback_data == "confirm_yes":
                await handle_booking_confirm_yes(self.bot, user_id, chat_id)
            
            elif callback_data == "confirm_no":
                await handle_booking_confirm_no(self.bot, user_id, chat_id)
            
            elif callback_data == "problem_bot_error":
                await handle_problem_bot_error(self.bot, user_id, chat_id)
            
            elif callback_data == "problem_user_timeout":
                await handle_problem_user_timeout(self.bot, user_id, chat_id)
            
            # Ответить на callback query (убрать загрузку)
            await self._answer_callback_query(callback["id"])
        
        except KeyError as exc:
            log_error("Invalid callback structure", exception=exc, context={})
        except Exception as exc:
            log_error("Error handling callback", exception=exc, context={})
    
    async def _answer_callback_query(self, callback_query_id: str) -> None:
        """Ответить на callback query (убрать загрузку)."""
        try:
            response = await self.client.post(
                f"https://api.telegram.org/bot{self.token}/answerCallbackQuery",
                json={"callback_query_id": callback_query_id},
                timeout=10,
            )
            
            if response.status_code != 200:
                log_warning(
                    f"Failed to answer callback query: {response.status_code}",
                    {"response": response.text[:200]}
                )
        
        except Exception as exc:
            log_error("Error answering callback query", exception=exc, context={})
    
    async def _start_watch_task(
        self,
        *,
        user_id: int,
        chat_id: int,
        title: str,
        seats_wanted: int,
        date_range: str | None,
    ) -> None:
        existing = self.watch_tasks.get(user_id)
        if existing and not existing.done():
            log_info("Мониторинг уже запущен, повторный запуск пропущен", {"user_id": user_id})
            return

        async def _runner() -> None:
            try:
                await self.watcher.watch(
                    user_id=user_id,
                    chat_id=chat_id,
                    title=title,
                    seats_wanted=seats_wanted,
                    date_range=date_range,
                    contacts=None,
                    artifact_dir=Path("./artifacts"),
                    headful=(os.getenv("HEADFUL", "false").lower() != "false"),
                    max_sessions=None,
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log_error(
                    "Managed watcher crashed",
                    exception=exc,
                    context={"user_id": user_id, "chat_id": chat_id, "title": title},
                )
            finally:
                self.watch_tasks.pop(user_id, None)

        self.watch_tasks[user_id] = asyncio.create_task(_runner())
        log_info(
            "Фоновый мониторинг запущен",
            {"user_id": user_id, "chat_id": chat_id, "title": title, "seats": seats_wanted},
        )

    async def close(self) -> None:
        """Закрыть приложение."""
        tasks = list(self.watch_tasks.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        await self.client.aclose()


async def run_bot(token: str) -> None:
    """Запустить Telegram бота."""
    log_info("Инициализация бота...", {})
    
    storage = SQLiteStorage()
    app = TelegramBotApp(token, storage)
    
    try:
        await app.start_polling()
    
    except KeyboardInterrupt:
        log_info("Бот остановлен пользователем", {})
    
    except Exception as exc:
        log_error("Fatal error in bot", exception=exc, context={})
    
    finally:
        await app.close()


if __name__ == "__main__":
    import os
    
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not token:
        log_error("TELEGRAM_BOT_TOKEN не установлен", context={})
        exit(1)
    
    asyncio.run(run_bot(token))
