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
from ogatt_booker.watcher import watch


def _make_session(qt_id: int, title: str = "Тест") -> Session:
    return Session(
        title=title,
        show_date=date(2026, 6, 1),
        show_time=time(19, 0),
        scene="Большая сцена",
        qt_session_id=qt_id,
        qt_url=f"https://quicktickets.ru/orel-teatr-turgeneva/s{qt_id}",
    )


def _make_result(session: Session, status: str = "ready_for_payment") -> BookingResult:
    r = BookingResult(session=session)
    r.status = status
    r.order_url = f"https://quicktickets.ru/ordering/anytickets/{session.qt_session_id}"
    return r


@pytest.mark.asyncio
async def test_watch_books_new_session():
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
            return []          # первый цикл — пусто
        return [session]       # второй цикл — сеанс появился

    async def fake_sleep(secs):
        if iteration >= 2 and call_count >= 1:
            raise asyncio.CancelledError  # останавливаем цикл после бронирования

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

    assert call_count == 1, "Бронирование должно быть вызвано ровно один раз"


@pytest.mark.asyncio
async def test_watch_no_duplicate_booking():
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
        return [session]  # сеанс присутствует всегда

    async def fake_sleep(secs):
        if iteration >= 3:
            raise asyncio.CancelledError

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

    assert call_count == 1, "Повторное бронирование одного сеанса недопустимо"


@pytest.mark.asyncio
async def test_watch_retries_on_booking_error():
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

    assert call_count == 2, "После ошибки должна быть повторная попытка"
