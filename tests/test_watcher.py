"""Юнит-тесты модуля watcher (без сети и браузера).

Проверяем логику цикла мониторинга: фильтрацию сеансов, дедупликацию
по ``qt_session_id`` и вызов бронирования при появлении нового сеанса.
"""
from __future__ import annotations

import asyncio
from datetime import date, time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ogatt_booker.models import BookingResult, Session
from ogatt_booker.storage import WatchingState
from ogatt_booker.watcher import watch
from ogatt_booker.watcher_managed import ManagedWatcher


def _make_session(qt_id: int, title: str = "Тест") -> Session:
    return Session(
        title=title,
        show_date=date(2026, 6, 1),
        show_time=time(19, 0),
        scene="Большая сцена",
        qt_session_id=qt_id,
        qt_url=f"https://quicktickets.ru/orel-teatr-turgeneva/s{qt_id}",
    )


def _make_result(session: Session, status: str = "seats_reserved") -> BookingResult:
    from datetime import datetime, timedelta

    r = BookingResult(session=session)
    r.status = status
    r.session_url = session.qt_url
    r.seats = ["Ряд 5, место 7", "Ряд 5, место 8"]
    r.seats_adjacent = True
    r.reserved_at = datetime(2026, 6, 1, 12, 0, 0)
    r.unfreeze_at = r.reserved_at + timedelta(minutes=3)
    return r


def test_watch_books_new_session():
    """При появлении нового сеанса watch должен вызвать бронирование ровно один раз."""
    session = _make_session(9001, "Гамлет")
    result = _make_result(session)

    call_count = 0

    async def fake_book(s, seats, contacts, artifact_dir, headful):
        nonlocal call_count
        call_count += 1
        return result

    iteration = 0

    def fake_fetch():
        return "<html/>"

    def fake_parse(html, today=None):
        nonlocal iteration
        iteration += 1
        if iteration == 1:
            return []
        return [session]

    async def fake_sleep(secs):
        if iteration >= 2 and call_count >= 1:
            raise asyncio.CancelledError

    async def _run():
        with (
            patch("ogatt_booker.watcher.AfishaFetcher") as MockFetcher,
            patch("ogatt_booker.watcher.parse_afisha", side_effect=fake_parse),
            patch("ogatt_booker.watcher._book_session", side_effect=fake_book),
            patch("ogatt_booker.watcher.notify_console"),
            patch("ogatt_booker.watcher.notify_telegram"),
            patch("asyncio.sleep", side_effect=fake_sleep),
        ):
            MockFetcher.return_value.fetch = fake_fetch
            with pytest.raises(asyncio.CancelledError):
                await watch(
                    title="Гамлет",
                    seats_wanted=2,
                    interval=1.0,
                    artifact_dir=Path("/tmp"),
                    headful=False,
                )

    asyncio.run(_run())
    assert call_count == 1, "Бронирование должно быть вызвано ровно один раз"


def test_watch_no_duplicate_booking():
    """Один и тот же qt_session_id не должен бронироваться дважды."""
    session = _make_session(9002, "Чайка")
    result = _make_result(session)

    call_count = 0

    async def fake_book(s, seats, contacts, artifact_dir, headful):
        nonlocal call_count
        call_count += 1
        return result

    iteration = 0

    def fake_parse(html, today=None):
        nonlocal iteration
        iteration += 1
        return [session]

    async def fake_sleep(secs):
        if iteration >= 3:
            raise asyncio.CancelledError

    async def _run():
        with (
            patch("ogatt_booker.watcher.AfishaFetcher") as MockFetcher,
            patch("ogatt_booker.watcher.parse_afisha", side_effect=fake_parse),
            patch("ogatt_booker.watcher._book_session", side_effect=fake_book),
            patch("ogatt_booker.watcher.notify_console"),
            patch("ogatt_booker.watcher.notify_telegram"),
            patch("asyncio.sleep", side_effect=fake_sleep),
        ):
            MockFetcher.return_value.fetch = lambda: "<html/>"
            with pytest.raises(asyncio.CancelledError):
                await watch(
                    title="Чайка",
                    seats_wanted=2,
                    interval=1.0,
                    artifact_dir=Path("/tmp"),
                    headful=False,
                )

    asyncio.run(_run())
    assert call_count == 1, "Повторное бронирование одного сеанса недопустимо"


def test_watch_retries_on_booking_error():
    """При ошибке бронирования сеанс убирается из booked_ids и пробуется снова."""
    session = _make_session(9003, "Вишнёвый сад")
    result = _make_result(session)

    call_count = 0

    async def fake_book(s, seats, contacts, artifact_dir, headful):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise RuntimeError("Временная ошибка")
        return result

    iteration = 0

    def fake_parse(html, today=None):
        nonlocal iteration
        iteration += 1
        return [session]

    async def fake_sleep(secs):
        if call_count >= 2:
            raise asyncio.CancelledError

    async def _run():
        with (
            patch("ogatt_booker.watcher.AfishaFetcher") as MockFetcher,
            patch("ogatt_booker.watcher.parse_afisha", side_effect=fake_parse),
            patch("ogatt_booker.watcher._book_session", side_effect=fake_book),
            patch("ogatt_booker.watcher.notify_console"),
            patch("ogatt_booker.watcher.notify_telegram"),
            patch("asyncio.sleep", side_effect=fake_sleep),
        ):
            MockFetcher.return_value.fetch = lambda: "<html/>"
            with pytest.raises(asyncio.CancelledError):
                await watch(
                    title="Вишнёвый сад",
                    seats_wanted=2,
                    interval=1.0,
                    artifact_dir=Path("/tmp"),
                    headful=False,
                )

    asyncio.run(_run())
    assert call_count == 2, "После ошибки должна быть повторная попытка"


def test_watch_retries_when_session_is_sold_out():
    """Отсутствие билетов на существующем сеансе — штатный сценарий, который должен повторно проверяться."""
    session = _make_session(9004, "Ревизор")
    sold_out = _make_result(session, status="sold_out")
    sold_out.message = "В зале сейчас нет доступных мест (возможно, всё раскуплено)."

    call_count = 0

    async def fake_book(s, seats, contacts, artifact_dir, headful):
        nonlocal call_count
        call_count += 1
        return sold_out

    async def fake_sleep(secs):
        if call_count >= 2:
            raise asyncio.CancelledError

    async def _run():
        with (
            patch("ogatt_booker.watcher.AfishaFetcher") as MockFetcher,
            patch("ogatt_booker.watcher.parse_afisha", return_value=[session]),
            patch("ogatt_booker.watcher._book_session", side_effect=fake_book),
            patch("ogatt_booker.watcher.notify_console"),
            patch("ogatt_booker.watcher.notify_telegram"),
            patch("asyncio.sleep", side_effect=fake_sleep),
        ):
            MockFetcher.return_value.fetch = lambda: "<html/>"
            with pytest.raises(asyncio.CancelledError):
                await watch(
                    title="Ревизор",
                    seats_wanted=2,
                    interval=1.0,
                    artifact_dir=Path("/tmp"),
                    headful=False,
                )

    asyncio.run(_run())
    assert call_count == 2, "При sold_out сеанс должен проверяться повторно, а не считаться фатальной ошибкой"


def test_managed_watch_checks_existing_sessions_immediately():
    """Managed watcher должен проверять уже существующие билеты на первой итерации без стартовой задержки."""
    session = _make_session(9010, "Гамлет")
    result = _make_result(session)

    storage = MagicMock()
    storage.get_watching_state.return_value = WatchingState(
        user_id=1,
        status="active",
        title="Гамлет",
        date_range="все даты",
        seats_count=2,
    )

    tg_bot = MagicMock()
    tg_bot.send_message = AsyncMock()
    tg_bot.send_booking_notification = AsyncMock()
    tg_bot.ask_booking_confirmation = AsyncMock()

    watcher = ManagedWatcher(tg_bot=tg_bot, storage=storage)

    async def fake_book(*args, **kwargs):
        return result

    async def fake_sleep(secs):
        raise asyncio.CancelledError

    async def _run():
        with (
            patch.object(watcher.fetcher, "fetch", return_value="<html/>"),
            patch("ogatt_booker.watcher_managed.parse_afisha", return_value=[session]),
            patch.object(watcher, "_book_session", side_effect=fake_book) as mock_book,
            patch("asyncio.sleep", side_effect=fake_sleep),
        ):
            await watcher.watch(
                user_id=1,
                chat_id=10,
                title="Гамлет",
                seats_wanted=2,
                date_range="все даты",
                artifact_dir=Path("/tmp"),
                headful=False,
                max_sessions=1,
            )
            mock_book.assert_awaited_once()

    asyncio.run(_run())
    tg_bot.send_booking_notification.assert_awaited_once()
    tg_bot.ask_booking_confirmation.assert_called_once()

    payload = tg_bot.send_booking_notification.await_args.args[1]
    assert payload["reserved_at"] == "2026-06-01T12:00:00"
    assert payload["unfreeze_at"] == "2026-06-01T12:03:00"

    storage.save_booking.assert_called_once()
    saved_kwargs = storage.save_booking.call_args.kwargs
    assert saved_kwargs["reserved_at"] == "2026-06-01T12:00:00"
    assert saved_kwargs["unfreeze_at"] == "2026-06-01T12:03:00"
