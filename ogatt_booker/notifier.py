"""Уведомления о готовом к оплате заказе."""
from __future__ import annotations

import logging
import os
from pathlib import Path

import httpx

from .handoff import build_handoff_html
from .models import BookingResult

log = logging.getLogger(__name__)


def format_message(result: BookingResult) -> str:
    s = result.session
    lines = [
        f"🎭 {s.title}",
        f"📅 {s.show_date:%d.%m.%Y} {s.show_time:%H:%M} · {s.scene}",
        f"🎟 Мест: {len(result.seats)}",
    ]
    if result.seats:
        lines.append("  " + ", ".join(result.seats))
    if result.total_price:
        lines.append(f"💰 К оплате: {result.total_price:.0f} ₽")
    if result.order_url:
        if result.order_url.rstrip("/").endswith("/ordering/anytickets"):
            lines.append("🔗 Оформление заказа открыто в браузере")
        else:
            lines.append(f"🔗 Оплата: {result.order_url}")
    if result.message:
        lines.append("")
        lines.append(result.message)
    return "\n".join(lines)

def notify_console(result: BookingResult) -> None:
    print("=" * 60)
    print(format_message(result))
    print("=" * 60)



def write_handoff_artifact(result: BookingResult, handoff_dir: str | Path = "./artifacts/handoff") -> str | None:
    """Сохраняет HTML-страницу для передачи пользователю на телефон.

    Возвращает относительный путь вида ``artifacts/handoff/...``. Если у вас
    есть публичный HTTP-слой (nginx, Caddy, static file server), этот путь можно
    превратить в кликабельную ссылку на уровне уведомления.
    """
    try:
        out_dir = Path(handoff_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        token = f"s{result.session.qt_session_id}"
        path = out_dir / f"handoff_{token}.html"
        path.write_text(build_handoff_html(result), encoding="utf-8")
        return str(path)
    except Exception:  # noqa: BLE001
        log.exception("Не удалось записать handoff-artifact")
        return None



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
            if result.screenshot_path and Path(result.screenshot_path).exists():
                with open(result.screenshot_path, "rb") as fh:
                    resp2 = client.post(
                        f"https://api.telegram.org/bot{token}/sendPhoto",
                        data={"chat_id": chat_id},
                        files={"photo": fh},
                    )
                    resp2.raise_for_status()
    except httpx.HTTPError as exc:
        log.warning("Telegram не доставлен: %s", exc)
        return False
    return True
