"""Минимальный веб-хэнд-офф для переданной в Telegram сессии бронирования.

Идея:
- Playwright-браузер остаётся на сервере живым после перехода на страницу заказа.
- Скрипт сохраняет артефакты сессии: screenshot, order_url, JSON.
- В Telegram отправляется ссылка на этот хэнд-офф, а не на QuickTickets напрямую.

Это не полноценный remote desktop, а лёгкая точка входа: пользователь видит
состояние заказа и может перейти к управлению серверной сессией через внешний
remote browser / VNC, если такой доступ поднят отдельно.
"""
from __future__ import annotations

import html
import json
from dataclasses import asdict
from pathlib import Path
from urllib.parse import quote

from .models import BookingResult


def build_handoff_html(result: BookingResult) -> str:
    """Собирает простую HTML-страницу для передачи пользователю."""
    s = result.session
    seats = ", ".join(result.seats) if result.seats else "—"
    total = f"{result.total_price:.0f} ₽" if result.total_price is not None else "—"
    order_url = html.escape(result.order_url or "")
    screenshot = html.escape(str(result.screenshot_path or ""))
    payload = html.escape(json.dumps(asdict(result), ensure_ascii=False, default=str, indent=2))

    return f"""<!doctype html>
<html lang=\"ru\">
<head>
  <meta charset=\"utf-8\" />
  <meta name=\"viewport\" content=\"width=device-width, initial-scale=1\" />
  <title>ogatt-booker handoff</title>
  <style>
    body {{ font-family: system-ui, -apple-system, BlinkMacSystemFont, sans-serif; margin: 24px; background: #f6f7fb; color: #111; }}
    .card {{ max-width: 900px; margin: 0 auto; background: #fff; border-radius: 16px; padding: 24px; box-shadow: 0 8px 30px rgba(0,0,0,.08); }}
    .meta {{ display: grid; grid-template-columns: 1fr 1fr; gap: 12px; margin: 16px 0 20px; }}
    .item {{ padding: 12px 14px; background: #f7f8fa; border-radius: 12px; }}
    .label {{ font-size: 12px; color: #666; text-transform: uppercase; letter-spacing: .04em; margin-bottom: 6px; }}
    .value {{ font-size: 16px; word-break: break-word; }}
    a.btn {{ display: inline-block; padding: 12px 18px; border-radius: 10px; text-decoration: none; background: #d11a2a; color: #fff; font-weight: 700; margin-right: 10px; }}
    a.btn.secondary {{ background: #2b6cb0; }}
    img {{ max-width: 100%; border-radius: 12px; border: 1px solid #e5e7eb; }}
    pre {{ background: #0f172a; color: #e2e8f0; padding: 16px; border-radius: 12px; overflow: auto; }}
    .hint {{ color: #555; line-height: 1.5; }}
  </style>
</head>
<body>
  <div class=\"card\">
    <h1>Передача заказа в ручное подтверждение</h1>
    <p class=\"hint\">Браузерная сессия сохранена на сервере. Откройте этот экран с телефона и используйте удалённый доступ к той же сессии браузера, если он поднят отдельно.</p>

    <div class=\"meta\">
      <div class=\"item\"><div class=\"label\">Спектакль</div><div class=\"value\">{html.escape(s.title)}</div></div>
      <div class=\"item\"><div class=\"label\">Дата и время</div><div class=\"value\">{s.show_date:%d.%m.%Y} {s.show_time:%H:%M}</div></div>
      <div class=\"item\"><div class=\"label\">Места</div><div class=\"value\">{html.escape(seats)}</div></div>
      <div class=\"item\"><div class=\"label\">К оплате</div><div class=\"value\">{html.escape(total)}</div></div>
    </div>

    <p>
      <a class=\"btn\" href=\"{order_url}\" target=\"_blank\" rel=\"noopener noreferrer\">Открыть страницу оформления</a>
      <a class=\"btn secondary\" href=\"{screenshot}\" target=\"_blank\" rel=\"noopener noreferrer\">Скриншот</a>
    </p>

    <h2>JSON</h2>
    <pre>{payload}</pre>
  </div>
</body>
</html>"""


def handoff_filename(token: str) -> str:
    return f"handoff_{quote(token, safe='')}.html"
