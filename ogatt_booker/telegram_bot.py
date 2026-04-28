"""Telegram бот для управления бронированием билетов."""
from __future__ import annotations

import asyncio
import os
from datetime import datetime
from typing import Optional

import httpx

from .errors import log_error, log_info, log_warning, ErrorHandler, RetryConfig
from .storage import SQLiteStorage, WatchingState


MAIN_MENU_KEYBOARD = {
    "keyboard": [
        [{"text": "/watch"}, {"text": "/status"}],
        [{"text": "/pause"}, {"text": "/resume"}],
        [{"text": "/change"}, {"text": "/stop"}],
        [{"text": "/sleep"}, {"text": "/help"}],
    ],
    "resize_keyboard": True,
    "persistent": True,
}


class TelegramBot:
    """Основной класс Telegram бота."""
    
    API_URL = "https://api.telegram.org"
    
    def __init__(self, token: str, storage: Optional[SQLiteStorage] = None):
        """
        Args:
            token: Токен Telegram бота
            storage: Хранилище состояния (создаётся автоматически)
        """
        self.token = token
        self.storage = storage or SQLiteStorage()
        self.retry_config = RetryConfig(max_retries=3, initial_delay=1.0)
    
    async def send_message(
        self,
        chat_id: int,
        text: str,
        parse_mode: str = "HTML",
        reply_markup: Optional[dict] = None,
    ) -> bool:
        """Отправить сообщение в Telegram.
        
        Args:
            chat_id: ID чата
            text: Текст сообщения
            parse_mode: Режим разметки (HTML, Markdown)
            reply_markup: Клавиатура (inline/reply)
            
        Returns:
            True если успешно, False иначе
        """
        async def _send():
            payload = {
                "chat_id": chat_id,
                "text": text,
            }
            if parse_mode:
                payload["parse_mode"] = parse_mode
            if reply_markup is not None:
                payload["reply_markup"] = reply_markup
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.post(
                    f"{self.API_URL}/bot{self.token}/sendMessage",
                    json=payload,
                )
                resp.raise_for_status()
                return resp.json()
        
        try:
            result = await ErrorHandler.with_retry(
                _send,
                config=self.retry_config,
                on_retry=lambda attempt, exc: log_warning(
                    f"Ошибка отправки сообщения (попытка {attempt}): {exc}"
                )
            )
            log_info("Сообщение отправлено", {"chat_id": chat_id, "length": len(text)})
            return True
        except Exception as exc:
            log_error("Не удалось отправить сообщение", exception=exc, context={"chat_id": chat_id})
            return False
    
    async def send_photo(
        self,
        chat_id: int,
        photo_path: str,
        caption: str = "",
        parse_mode: str = "HTML",
    ) -> bool:
        """Отправить фото в Telegram.
        
        Args:
            chat_id: ID чата
            photo_path: Путь к фото файлу
            caption: Подпись под фото
            parse_mode: Режим разметки
            
        Returns:
            True если успешно, False иначе
        """
        async def _send():
            async with httpx.AsyncClient(timeout=15.0) as client:
                with open(photo_path, "rb") as f:
                    files = {"photo": f}
                    data = {
                        "chat_id": chat_id,
                        "caption": caption,
                        "parse_mode": parse_mode,
                    }
                    resp = await client.post(
                        f"{self.API_URL}/bot{self.token}/sendPhoto",
                        files=files,
                        data=data,
                    )
                    resp.raise_for_status()
                    return resp.json()
        
        try:
            result = await ErrorHandler.with_retry(
                _send,
                config=self.retry_config,
                on_retry=lambda attempt, exc: log_warning(
                    f"Ошибка отправки фото (попытка {attempt}): {exc}"
                )
            )
            log_info("Фото отправлено", {"chat_id": chat_id})
            return True
        except Exception as exc:
            log_error("Не удалось отправить фото", exception=exc, context={"chat_id": chat_id, "photo": photo_path})
            return False
    
    async def send_booking_notification(
        self,
        chat_id: int,
        booking_data: dict,
    ) -> bool:
        """Отправить уведомление о бронировании.
        
        Args:
            chat_id: ID чата
            booking_data: Данные бронирования из BookingResult
            
        Returns:
            True если успешно, False иначе
        """
        try:
            text = (
                f"🎉 <b>Билеты забронированы!</b>\n\n"
                f"🎭 {booking_data.get('title', 'N/A')}\n"
                f"📅 {booking_data.get('show_date', 'N/A')} {booking_data.get('show_time', 'N/A')}\n"
                f"🪑 Места: {', '.join(booking_data.get('seats', []))}\n"
            )

            if not booking_data.get('seats_adjacent', True):
                text += "⚠️ <i>Соседних мест не было, выбраны доступные.</i>\n"

            if booking_data.get('total_price'):
                text += f"💰 Сумма: {booking_data['total_price']:.0f} ₽\n"

            reserved_at = booking_data.get('reserved_at')
            unfreeze_at = booking_data.get('unfreeze_at')
            if reserved_at and unfreeze_at:
                if isinstance(unfreeze_at, str):
                    unfreeze_dt = datetime.fromisoformat(unfreeze_at)
                else:
                    unfreeze_dt = unfreeze_at
                remaining = max(0, int((unfreeze_dt - datetime.now()).total_seconds()))
                minutes, seconds = divmod(remaining, 60)
                text += (
                    f"⏳ Резерв активен до: {unfreeze_dt.strftime('%H:%M:%S')}\n"
                    f"⌛ До разморозки: {minutes}м {seconds:02d}с\n"
                )

            if booking_data.get('session_url'):
                text += f"🔗 <a href=\"{booking_data['session_url']}\">Открыть рассадку</a>"

            message_ok = await self.send_message(chat_id, text, parse_mode="HTML")
            
            photo_ok = True
            if booking_data.get('screenshot_path'):
                photo_ok = await self.send_photo(
                    chat_id,
                    booking_data['screenshot_path'],
                    caption="📸 Забронированные места"
                )
            
            if message_ok and photo_ok:
                log_info("Уведомление о бронировании отправлено", {"chat_id": chat_id})
                return True
            return False
        
        except Exception as exc:
            log_error("Ошибка отправки уведомления о бронировании", exception=exc, context={"chat_id": chat_id})
            return False
    
    async def ask_booking_confirmation(self, chat_id: int, booking_data: Optional[dict] = None) -> None:
        """Спросить пользователя о статусе бронирования ближе к окончанию резерва."""
        wait_seconds = 180
        if booking_data and booking_data.get("unfreeze_at"):
            try:
                unfreeze_at = booking_data["unfreeze_at"]
                if isinstance(unfreeze_at, str):
                    unfreeze_dt = datetime.fromisoformat(unfreeze_at)
                else:
                    unfreeze_dt = unfreeze_at
                wait_seconds = max(5, int((unfreeze_dt - datetime.now()).total_seconds()))
            except Exception:
                wait_seconds = 180

        await asyncio.sleep(wait_seconds)

        text = (
            "⏳ <b>Статус бронирования</b>\n\n"
            "Временный резерв уже почти закончился или закончился.\n"
            "Получилось ли вам купить билеты?\n\n"
            "Если <b>ДА</b> — отлично! 🎉\n"
            "Если <b>НЕТ</b> — бот сможет продолжить отслеживание новых мест."
        )
        
        keyboard = {
            "inline_keyboard": [
                [
                    {"text": "✅ Да, купил(а)", "callback_data": "confirm_yes"},
                    {"text": "❌ Нет, не купил(а)", "callback_data": "confirm_no"},
                ]
            ]
        }
        
        await self.send_message(chat_id, text, parse_mode="HTML", reply_markup=keyboard)
    
    def get_main_menu_keyboard(self) -> dict:
        """Основная reply-клавиатура бота."""
        return MAIN_MENU_KEYBOARD

    def get_help_text(self) -> str:
        """Полное описание доступных команд и сценариев."""
        return (
            "🤖 <b>Справка по боту</b>\n\n"
            "<b>Основные команды:</b>\n"
            "🔍 <b>/watch</b> — запустить настройку мониторинга спектакля\n"
            "📊 <b>/status</b> — показать текущий статус мониторинга\n"
            "⏸️ <b>/pause</b> — временно приостановить активный мониторинг\n"
            "▶️ <b>/resume</b> — возобновить приостановленный мониторинг\n"
            "✏️ <b>/change</b> — заново настроить спектакль, даты и число мест\n"
            "⏹️ <b>/stop</b> — полностью остановить мониторинг\n"
            "😴 <b>/sleep</b> — перевести бота в спящий режим\n"
            "❓ <b>/help</b> — показать эту справку\n\n"
            "<b>Как это работает:</b>\n"
            "1. Нажмите /watch и укажите спектакль.\n"
            "2. При желании задайте диапазон дат.\n"
            "3. Укажите количество билетов.\n"
            "4. Бот начнёт отслеживание и при появлении билетов попытается выбрать места.\n"
            "5. Если соседние места есть — бот берёт их в приоритет. Если нет — предупредит и выберет доступные.\n"
            "6. После временного резерва бот пришлёт скриншот, ссылку и таймер до разморозки."
        )

    def get_watching_status_text(self, state: Optional[WatchingState]) -> str:
        """Получить текст статуса отслеживания.
        
        Args:
            state: Состояние отслеживания
            
        Returns:
            Форматированный текст статуса
        """
        if not state or state.status == 'sleeping':
            return "😴 <b>Режим сна</b>\n\nБот не отслеживает билеты.\n\nЧтобы начать: /watch"
        
        if state.status == 'paused':
            return "⏸️ <b>Отслеживание приостановлено</b>\n\nДля возобновления: /resume"
        
        if state.status == 'active':
            duration = ""
            if state.started_at:
                import datetime
                elapsed = datetime.datetime.now() - state.started_at
                hours = elapsed.seconds // 3600
                minutes = (elapsed.seconds % 3600) // 60
                duration = f"\n⏱️ Работает: {hours}ч {minutes}м"
            
            return (
                f"🟢 <b>Активно</b>{duration}\n"
                f"🎭 {state.title}\n"
                f"📅 {state.date_range}\n"
                f"🎫 {state.seats_count} билет(ов)\n\n"
                f"Команды:\n"
                f"/pause - приостановить\n"
                f"/stop - остановить\n"
                f"/change - изменить параметры"
            )
        
        return "❓ Неизвестный статус"


# Функции для обработчиков команд
async def handle_start(bot: TelegramBot, chat_id: int) -> None:
    """Обработчик команды /start."""
    text = (
        "👋 <b>Добро пожаловать!</b>\n\n"
        "Я помогу отслеживать спектакли, выбирать места и присылать ссылку на резерв.\n\n"
        "Кнопки меню уже показаны снизу. Для полного списка возможностей используйте /help."
    )
    await bot.send_message(
        chat_id,
        text,
        parse_mode="HTML",
        reply_markup=bot.get_main_menu_keyboard(),
    )
    log_info("Команда /start выполнена", {"chat_id": chat_id})


async def handle_status(bot: TelegramBot, chat_id: int, user_id: int) -> None:
    """Обработчик команды /status."""
    state = bot.storage.get_watching_state(user_id)
    text = bot.get_watching_status_text(state)
    await bot.send_message(
        chat_id,
        text,
        parse_mode="HTML",
        reply_markup=bot.get_main_menu_keyboard(),
    )
    log_info("Команда /status выполнена", {"chat_id": chat_id})


async def handle_help(bot: TelegramBot, chat_id: int) -> None:
    """Обработчик команды /help."""
    await bot.send_message(
        chat_id,
        bot.get_help_text(),
        parse_mode="HTML",
        reply_markup=bot.get_main_menu_keyboard(),
    )
    log_info("Команда /help выполнена", {"chat_id": chat_id})
