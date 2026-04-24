"""Мониторинг появления билетов на спектакль и автоматическое бронирование.

Логика:
1. Каждые ``interval`` секунд загружает афишу и ищет сеансы по названию.
2. При появлении нового сеанса (которого раньше не было) запускает
   ``QuickticketsDriver`` в режиме ``auto`` и бронирует ``seats_wanted`` мест.
3. Отправляет ссылку на оплату в Telegram.
4. Помечает сеанс как обработанный, чтобы не бронировать повторно.
"""
from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path

from .afisha import AfishaFetcher, find_sessions, parse_afisha
from .models import BookingResult, Session
from .notifier import notify_console, notify_telegram, write_handoff_artifact
from .qt import BookingOptions, Contacts, QuickticketsDriver

log = logging.getLogger(__name__)


async def _book_session(
    session: Session,
    seats_wanted: int,
    contacts: Contacts | None,
    artifact_dir: Path,
    headful: bool,
) -> BookingResult:
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


async def watch(
    title: str,
    *,
    seats_wanted: int = 2,
    interval: float = 120.0,
    contacts: Contacts | None = None,
    artifact_dir: Path = Path("./artifacts"),
    headful: bool = False,
    tg_token: str | None = None,
    tg_chat_id: str | None = None,
    max_sessions: int | None = None,
) -> None:
    """Основной цикл мониторинга.

    Args:
        title: Подстрока названия спектакля (регистр не важен).
        seats_wanted: Сколько мест бронировать.
        interval: Пауза между проверками афиши (секунды).
        contacts: Контактные данные для формы заказа.
        artifact_dir: Куда сохранять скрины и JSON.
        headful: Показывать браузер (по умолчанию headless для watch).
        tg_token: Токен Telegram-бота.
        tg_chat_id: ID чата для уведомлений.
        max_sessions: Остановиться после бронирования N сеансов (None = бесконечно).
    """
    fetcher = AfishaFetcher()
    booked_ids: set[int] = set()
    booked_count = 0

    log.info(
        "Слежу за спектаклем %r, проверка каждые %.0f сек. Ctrl+C для остановки.",
        title, interval,
    )

    while True:
        try:
            html = fetcher.fetch()
            sessions = parse_afisha(html)
            matches = find_sessions(sessions, title=title)
        except Exception as exc:  # noqa: BLE001
            log.warning("Ошибка загрузки афиши: %s", exc)
            await asyncio.sleep(interval)
            continue

        new_sessions = [s for s in matches if s.qt_session_id not in booked_ids]

        if not new_sessions:
            log.info("Новых сеансов '%s' не найдено (%d в афише). Жду…", title, len(matches))
        else:
            # Берём ближайший по дате/времени
            target = sorted(new_sessions, key=lambda s: (s.show_date, s.show_time))[0]
            log.info(
                "Найден сеанс: %s %s %s (s%d) — начинаю бронирование %d мест…",
                target.title, target.show_date, target.show_time,
                target.qt_session_id, seats_wanted,
            )
            booked_ids.add(target.qt_session_id)

            try:
                result = await _book_session(
                    target, seats_wanted, contacts, artifact_dir, headful
                )
            except Exception as exc:  # noqa: BLE001
                log.error("Ошибка бронирования: %s", exc)
                # Убираем из booked_ids, чтобы попробовать снова
                booked_ids.discard(target.qt_session_id)
                await asyncio.sleep(interval)
                continue

            handoff_path = write_handoff_artifact(result)
            if handoff_path:
                result.message = (result.message or "") + f"\nHandoff: {handoff_path}"

            notify_console(result)
            notify_telegram(result, token=tg_token, chat_id=tg_chat_id)

            if result.status == "ready_for_payment":
                booked_count += 1
                log.info("Успешно забронировано. Всего сеансов: %d", booked_count)
                if max_sessions and booked_count >= max_sessions:
                    log.info("Достигнут лимит max_sessions=%d, останавливаюсь.", max_sessions)
                    return
            else:
                log.warning("Бронирование завершилось со статусом '%s': %s", result.status, result.message)
                booked_ids.discard(target.qt_session_id)

        await asyncio.sleep(interval)
