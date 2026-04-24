"""Парсер афиши ``ogatt.ru``.

Почему парсим HTML, а не API:
    у сайта нет публичного JSON-API. Страница ``/poster/`` рендерится на
    сервере (XSLT) и содержит блоки ``.afisha-item`` со всеми нужными
    полями и прямой ссылкой на сеанс в QuickTickets
    (``https://quicktickets.ru/orel-teatr-turgeneva/s<ID>``).
"""
from __future__ import annotations

import logging
import re
import time as _time
from dataclasses import dataclass
from datetime import date, datetime, time
from typing import Iterable

import httpx
from bs4 import BeautifulSoup, Tag

log = logging.getLogger(__name__)

from .models import Session

BASE_URL = "https://ogatt.ru"
POSTER_URL = f"{BASE_URL}/poster/"
QT_SESSION_RE = re.compile(r"/orel-teatr-turgeneva/s(\d+)", re.IGNORECASE)

# Русские названия месяцев в родительном падеже, как на сайте (afisha-month).
MONTHS_GENITIVE = {
    "января": 1,
    "февраля": 2,
    "марта": 3,
    "апреля": 4,
    "мая": 5,
    "июня": 6,
    "июля": 7,
    "августа": 8,
    "сентября": 9,
    "октября": 10,
    "ноября": 11,
    "декабря": 12,
}

DEFAULT_UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)


@dataclass
class AfishaFetcher:
    """Скачивает и кэширует HTML страниц афиши."""

    timeout: float = 15.0
    user_agent: str = DEFAULT_UA
    retries: int = 3
    retry_delay: float = 2.0

    def fetch(self, url: str = POSTER_URL) -> str:
        last_exc: Exception | None = None
        for attempt in range(1, self.retries + 1):
            try:
                with httpx.Client(
                    headers={"User-Agent": self.user_agent},
                    timeout=self.timeout,
                    follow_redirects=True,
                ) as client:
                    resp = client.get(url)
                    resp.raise_for_status()
                    return resp.text
            except httpx.HTTPError as exc:
                last_exc = exc
                if attempt < self.retries:
                    log.warning(
                        "Попытка %d/%d не удалась (%s), повтор через %.1f сек…",
                        attempt, self.retries, exc, self.retry_delay,
                    )
                    _time.sleep(self.retry_delay)
        raise last_exc  # type: ignore[misc]


def _text(tag: Tag | None) -> str:
    return tag.get_text(strip=True) if tag else ""


def _resolve_date(day: int, month: int, today: date | None = None) -> date:
    """Восстанавливаем год. На афише указаны только день и месяц,
    поэтому берём ближайшую будущую (или сегодняшнюю) дату: если месяц
    в этом году уже прошёл — перенос на следующий.
    """
    today = today or date.today()
    year = today.year
    try:
        candidate = date(year, month, day)
    except ValueError:
        # например, 29 февраля в невисокосный год — просто переносим на год
        candidate = date(year + 1, month, day)
    if candidate < today:
        candidate = candidate.replace(year=year + 1)
    return candidate


def parse_afisha(html: str, today: date | None = None) -> list[Session]:
    """Извлекает все сеансы со страницы ``/poster/``."""
    soup = BeautifulSoup(html, "lxml")
    sessions: list[Session] = []
    for item in soup.select(".afisha-item"):
        day_txt = _text(item.select_one(".afisha-day"))
        month_txt = _text(item.select_one(".afisha-month")).lower()
        time_txt = _text(item.select_one(".afisha-time"))
        scene = _text(item.select_one(".afisha-scene"))
        title = _text(item.select_one(".afisha-title"))
        age = _text(item.select_one(".afisha-age")) or None
        detail_a = item.select_one("a.afisha-img") or item.select_one(
            "a.afisha-right__top"
        )
        detail_href = detail_a.get("href") if detail_a else None
        if detail_href and detail_href.startswith("/"):
            detail_href = BASE_URL + detail_href

        qt_link_tag = item.select_one("a.a_quicktickets[href*='quicktickets.ru']")
        if not qt_link_tag:
            continue
        qt_href = qt_link_tag.get("href", "")
        m = QT_SESSION_RE.search(qt_href)
        if not m:
            continue
        qt_id = int(m.group(1))

        try:
            day_i = int(day_txt)
        except ValueError:
            continue
        month_i = MONTHS_GENITIVE.get(month_txt)
        if not month_i:
            continue
        try:
            hh, mm = time_txt.split(":")
            show_time = time(int(hh), int(mm))
        except (ValueError, AttributeError):
            continue

        show_date = _resolve_date(day_i, month_i, today=today)
        sessions.append(
            Session(
                title=title,
                show_date=show_date,
                show_time=show_time,
                scene=scene,
                qt_session_id=qt_id,
                qt_url=qt_href,
                detail_url=detail_href,
                age_rating=age,
            )
        )
    return sessions


def find_sessions(
    sessions: Iterable[Session],
    *,
    title: str | None = None,
    on_date: date | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
    at_time: time | None = None,
    scene: str | None = None,
) -> list[Session]:
    """Ищет сеансы по мягким критериям. Все параметры комбинируются по ``AND``.

    ``title`` сравнивается без учёта регистра и как подстрока.
    ``on_date`` — точное совпадение даты.
    ``date_from`` / ``date_to`` — диапазон дат (включительно); совместимы с ``on_date``.
    """
    def _norm(s: str) -> str:
        return re.sub(r"\s+", " ", s.strip().lower())

    needle = _norm(title) if title else None
    result: list[Session] = []
    for s in sessions:
        if needle and needle not in _norm(s.title):
            continue
        if on_date and s.show_date != on_date:
            continue
        if date_from and s.show_date < date_from:
            continue
        if date_to and s.show_date > date_to:
            continue
        if at_time and s.show_time != at_time:
            continue
        if scene and scene.lower() not in s.scene.lower():
            continue
        result.append(s)
    result.sort(key=lambda s: (s.show_date, s.show_time))
    return result


def load_afisha(fetcher: AfishaFetcher | None = None) -> list[Session]:
    """Быстрый путь: скачать и распарсить текущую афишу."""
    fetcher = fetcher or AfishaFetcher()
    return parse_afisha(fetcher.fetch())


def parse_user_datetime(value: str) -> tuple[date, time | None]:
    """Поддержка форматов ``2026-04-28`` и ``2026-04-28 19:00``."""
    value = value.strip()
    fmts = ("%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M", "%Y-%m-%d")
    for fmt in fmts:
        try:
            dt = datetime.strptime(value, fmt)
            return dt.date(), (dt.time() if "H" in fmt else None)
        except ValueError:
            continue
    raise ValueError(f"Не удалось распознать дату/время: {value!r}")
