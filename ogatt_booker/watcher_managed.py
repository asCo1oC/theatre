"""Обновленный watcher с поддержкой управления через Telegram бота."""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Optional

from .afisha import AfishaFetcher, find_sessions, parse_afisha
from .errors import ErrorHandler, RetryConfig, log_error, log_warning, log_info
from .models import BookingResult, Session
from .qt import BookingOptions, Contacts, QuickticketsDriver
from .storage import SQLiteStorage
from .telegram_bot import TelegramBot

log = logging.getLogger(__name__)


class ManagedWatcher:
    """Watcher с управлением через Telegram бот."""
    
    def __init__(
        self,
        tg_bot: Optional[TelegramBot] = None,
        storage: Optional[SQLiteStorage] = None,
    ):
        self.tg_bot = tg_bot
        self.storage = storage or SQLiteStorage()
        self.fetcher = AfishaFetcher()
        self.retry_config = RetryConfig(max_retries=5, initial_delay=2.0)
    
    async def watch(
        self,
        user_id: int,
        chat_id: int,
        title: str,
        seats_wanted: int = 2,
        date_range: Optional[str] = None,
        contacts: Optional[Contacts] = None,
        artifact_dir: Path = Path("./artifacts"),
        headful: bool = False,
        max_sessions: Optional[int] = None,
    ) -> None:
        """Основной цикл мониторинга с поддержкой управления.
        
        Args:
            user_id: ID пользователя
            chat_id: ID чата для уведомлений
            title: Название спектакля
            seats_wanted: Количество мест
            date_range: Диапазон дат (опционально)
            contacts: Контактные данные
            artifact_dir: Директория для артефактов
            headful: Показывать браузер
            max_sessions: Максимум сеансов для бронирования
        """
        # Сохраняем состояние как активное
        self.storage.set_watching_state(
            user_id=user_id,
            status="active",
            title=title,
            date_range=date_range or "все даты",
            seats_count=seats_wanted,
        )
        
        booked_ids: set[int] = set()
        booked_count = 0
        
        log_info(
            f"Мониторинг запущен: {title}, {seats_wanted} мест",
            {"user_id": user_id, "chat_id": chat_id}
        )
        
        await self.tg_bot.send_message(
            chat_id,
            f"🔍 <b>Мониторинг запущен</b>\n\n"
            f"🎭 {title}\n"
            f"🎫 {seats_wanted} билет(ов)\n\n"
            f"Вы получите уведомление когда найдутся билеты.",
            parse_mode="HTML"
        )
        
        while True:
            try:
                # Проверяем статус в хранилище
                state = self.storage.get_watching_state(user_id)
                
                if state and state.status == 'sleeping':
                    log_info("Режим сна активирован, остановка мониторинга", {"user_id": user_id})
                    await self.tg_bot.send_message(
                        chat_id,
                        "😴 Мониторинг остановлен (режим сна).\n\n"
                        "Введите /watch для возобновления.",
                        parse_mode="HTML"
                    )
                    break
                
                if state and state.status == 'paused':
                    log_info("Мониторинг приостановлен", {"user_id": user_id})
                    await asyncio.sleep(10)  # Проверяем статус каждые 10 сек
                    continue
                
                # Загружаем афишу с повторными попытками
                async def _fetch_afisha():
                    html = await asyncio.to_thread(self.fetcher.fetch)
                    return parse_afisha(html)
                
                sessions = await ErrorHandler.with_retry(
                    _fetch_afisha,
                    config=self.retry_config,
                    on_retry=lambda attempt, exc: log_warning(
                        f"Ошибка загрузки афиши (попытка {attempt}): {exc}",
                        {"user_id": user_id}
                    )
                )
                
                matches = find_sessions(sessions, title=title)
                new_sessions = [s for s in matches if s.qt_session_id not in booked_ids]
                
                if not new_sessions:
                    await asyncio.sleep(30)  # Проверяем каждые 30 сек
                    continue
                
                # Новый сеанс найден
                target = sorted(new_sessions, key=lambda s: (s.show_date, s.show_time))[0]
                booked_ids.add(target.qt_session_id)
                
                log_info(
                    f"Найден сеанс: {target.title} {target.show_date} {target.show_time}",
                    {"user_id": user_id, "session_id": target.qt_session_id}
                )
                
                # Выполняем бронирование
                try:
                    result = await self._book_session(
                        target, seats_wanted, contacts, artifact_dir, headful
                    )
                    
                    if result.status in {"ready_for_payment", "seats_reserved"}:
                        # Сохраняем бронирование
                        booking_payload = {
                            "title": result.session.title,
                            "show_date": result.session.show_date,
                            "show_time": result.session.show_time,
                            "seats": result.seats,
                            "seats_adjacent": result.seats_adjacent,
                            "total_price": result.total_price,
                            "session_url": result.session_url,
                            "screenshot_path": result.screenshot_path,
                            "reserved_at": result.reserved_at.isoformat() if result.reserved_at else None,
                            "unfreeze_at": result.unfreeze_at.isoformat() if result.unfreeze_at else None,
                        }

                        self.storage.save_booking(
                            user_id=user_id,
                            title=result.session.title,
                            date=str(result.session.show_date),
                            time=str(result.session.show_time),
                            seats=", ".join(result.seats),
                            screenshot_path=result.screenshot_path,
                            session_url=result.session_url,
                            reserved_at=booking_payload["reserved_at"],
                            unfreeze_at=booking_payload["unfreeze_at"],
                        )
                        
                        # Отправляем уведомление
                        await self.tg_bot.send_booking_notification(chat_id, booking_payload)
                        
                        # Спросим подтверждение к моменту разморозки в отдельной задаче
                        asyncio.create_task(self.tg_bot.ask_booking_confirmation(chat_id, booking_payload))
                        
                        booked_count += 1
                        log_info(
                            f"Успешное бронирование ({booked_count}/{max_sessions or '∞'})",
                            {"user_id": user_id}
                        )
                        
                        if max_sessions and booked_count >= max_sessions:
                            log_info("Достигнут лимит сеансов", {"user_id": user_id})
                            await self.tg_bot.send_message(
                                chat_id,
                                "✅ Достигнут лимит бронирований.\n\n"
                                "Введите /watch для нового мониторинга.",
                                parse_mode="HTML"
                            )
                            break
                    elif result.status == "sold_out":
                        log_info(
                            f"На найденном сеансе сейчас нет доступных мест: {result.message}",
                            {"user_id": user_id, "status": result.status, "session_id": target.qt_session_id}
                        )
                        booked_ids.discard(target.qt_session_id)
                    else:
                        log_error(
                            f"Ошибка бронирования: {result.message}",
                            context={"user_id": user_id, "status": result.status}
                        )
                        booked_ids.discard(target.qt_session_id)
                
                except Exception as exc:
                    log_error(
                        "Критическая ошибка при бронировании",
                        exception=exc,
                        context={"user_id": user_id, "session_id": target.qt_session_id}
                    )
                    booked_ids.discard(target.qt_session_id)
                    await self.tg_bot.send_message(
                        chat_id,
                        "❌ Ошибка при бронировании. Проверьте логи.\n\n"
                        "Попробуем заново...",
                        parse_mode="HTML"
                    )
                
                await asyncio.sleep(10)
            
            except Exception as exc:
                log_error(
                    "Критическая ошибка в цикле мониторинга",
                    exception=exc,
                    context={"user_id": user_id}
                )
                await asyncio.sleep(30)
    
    async def _book_session(
        self,
        session: Session,
        seats_wanted: int,
        contacts: Optional[Contacts],
        artifact_dir: Path,
        headful: bool,
    ) -> BookingResult:
        """Забронировать сеанс."""
        opts = BookingOptions(
            session=session,
            seats_wanted=seats_wanted,
            mode="auto",
            contacts=contacts,
            headful=headful,
            artifact_dir=artifact_dir,
        )
        async with QuickticketsDriver(opts) as drv:
            return await drv.run()
