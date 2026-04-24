"""Модели домена: сеанс спектакля и результат бронирования."""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from datetime import date, time, datetime
from typing import Any


@dataclass(frozen=True)
class Session:
    """Один сеанс в афише театра.

    ``qt_session_id`` — внутренний идентификатор сеанса в QuickTickets
    (часть URL вида ``/s1344``). Он однозначно идентифицирует связку
    "спектакль + дата + время + сцена".
    """

    title: str
    show_date: date
    show_time: time
    scene: str
    qt_session_id: int
    qt_url: str
    detail_url: str | None = None
    age_rating: str | None = None

    @property
    def when(self) -> datetime:
        return datetime.combine(self.show_date, self.show_time)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["show_date"] = self.show_date.isoformat()
        d["show_time"] = self.show_time.strftime("%H:%M")
        d["when"] = self.when.isoformat()
        return d


@dataclass
class BookingResult:
    """Результат работы драйвера QuickTickets."""

    session: Session
    seats: list[str] = field(default_factory=list)
    total_price: float | None = None
    order_url: str | None = None
    screenshot_path: str | None = None
    status: str = "pending"
    message: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "session": self.session.to_dict(),
            "seats": list(self.seats),
            "total_price": self.total_price,
            "order_url": self.order_url,
            "screenshot_path": self.screenshot_path,
            "status": self.status,
            "message": self.message,
        }
