"""Оффлайн-тест парсера афиши на сохранённом HTML.

Запуск::

    python -m pytest tests/ -q

Зависимости от сети нет — тест читает фикстуру ``research/ogatt_poster.html``.
"""
from __future__ import annotations

from datetime import date
from pathlib import Path

from ogatt_booker.afisha import parse_afisha, find_sessions


FIXTURE = Path(__file__).resolve().parent.parent / "research" / "ogatt_poster.html"


def _load_sessions():
    html = FIXTURE.read_text(encoding="utf-8")
    return parse_afisha(html, today=date(2026, 4, 23))


def test_parses_some_sessions():
    sessions = _load_sessions()
    assert len(sessions) > 5, "Ожидали хотя бы несколько сеансов в афише"
    # Все должны иметь QT-ID и URL
    assert all(s.qt_session_id > 0 for s in sessions)
    assert all("quicktickets.ru/orel-teatr-turgeneva/s" in s.qt_url for s in sessions)


def test_find_by_title_substring():
    sessions = _load_sessions()
    found = find_sessions(sessions, title="бабье лето")
    assert found, "Ожидали хотя бы один 'Бабье лето'"
    assert all("бабье лето" in s.title.lower() for s in found)


def test_dates_in_current_or_next_year():
    sessions = _load_sessions()
    # Раз today=2026-04-23, все даты должны быть >= 2026-04-01
    assert all(s.show_date >= date(2026, 4, 1) for s in sessions)
