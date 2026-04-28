"""Уведомления о бронировании мест и ссылке на рассадку."""
from __future__ import annotations

import logging
import os
from pathlib import Path

import httpx

from .models import BookingResult

log = logging.getLogger(__name__)


def format_message(result: BookingResult) -> str:
    s = result.session
    lines = [
        f"🎭 {s.title}",
        f"📅 {s.show_date:%d.%m.%Y} {s.show_time:%H:%M} · {s.scene}",
        f"🎟 Забронировано мест: {len(result.seats)}",
    ]
    if result.seats:
        lines.append("  " + ", ".join(result.seats))
    if not result.seats_adjacent:
        lines.append("⚠️  Соседних мест не было, выбраны доступные места.")
    if result.total_price:
        lines.append(f"💰 Сумма: {result.total_price:.0f} ₽")
    if result.session_url:
        lines.append(f"🔗 Рассадка: {result.session_url}")
    if result.message:
        lines.append("")
        lines.append(result.message)
    return "\n".join(lines)

def notify_console(result: BookingResult) -> None:
    print("=" * 60)
    print(format_message(result))
    print("=" * 60)



def notify_telegram(
    result: BookingResult,
    *,
    token: str | None = None,
    chat_id: str | None = None,
) -> bool:
    token = token or os.getenv("TG_BOT_TOKEN")
    chat_id = chat_id or os.getenv("TG_CHAT_ID")
    if not (token and chat_id):
        log.info("Telegram-уведомление пропущено: не заданы TG_BOT_TOKEN/TG_CHAT_ID.")
        return False

    message = format_message(result)
    try:
        with httpx.Client(timeout=15.0) as client:
            resp = client.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                data={"chat_id": chat_id, "text": message, "disable_web_page_preview": "true"},
            )
            resp.raise_for_status()
            
            # 2. Отправляем скриншот забронированных мест, если есть
            if result.screenshot_path and Path(result.screenshot_path).exists():
                with open(result.screenshot_path, "rb") as fh:
                    resp2 = client.post(
                        f"https://api.telegram.org/bot{token}/sendPhoto",
                        data={"chat_id": chat_id, "caption": "📸 Забронированные места"},
                        files={"photo": fh},
                    )
                    resp2.raise_for_status()
    except httpx.HTTPError as exc:
        log.warning("Telegram не доставлен: %s", exc)
        return False
    return True
