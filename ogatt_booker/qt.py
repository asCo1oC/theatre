"""Драйвер ``quicktickets.ru`` на Playwright.

Архитектура страницы покупки:

1. ``https://quicktickets.ru/orel-teatr-turgeneva/s<ID>`` — оболочка с
   iframe ``name="qt_hall"``, загружающая SPA ``hall.quicktickets.ru``.
2. Родитель передаёт в iframe через ``postMessage`` токен и id сеанса.
3. После выбора мест пользователем клик «Купить» инициирует
   ``POST /ordering/initAnytickets`` и переход на ``/ordering/anytickets``,
   где заполняется форма контактов и затем идёт эквайринг.

Наши задачи:
* Устойчиво открыть страницу (ddos-guard).
* Дождаться схемы зала.
* В режиме ``semiauto`` — ждать, пока пользователь выберет места сам.
* В режиме ``auto`` — попытаться ткнуть в доступные места «в центре зала»
  (эвристика, т.к. это React SPA без публичного API).
* Поймать переход на страницу оформления заказа, заполнить контакты
  и **остановиться** до нажатия кнопки оплаты.

Скрипт принципиально не выполняет сам платёж — это снимает юридические
риски и не требует вводить данные карты в автоматизированный браузер.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

try:
    from playwright.async_api import (
        Browser,
        BrowserContext,
        Frame,
        Page,
        Playwright,
        TimeoutError as PWTimeout,
        async_playwright,
    )
    PLAYWRIGHT_AVAILABLE = True
except ModuleNotFoundError:  # pragma: no cover - fallback for test/import environments
    Browser = BrowserContext = Frame = Page = Playwright = object

    class PWTimeout(Exception):
        pass

    async def async_playwright():
        raise ModuleNotFoundError("playwright is not installed")

    PLAYWRIGHT_AVAILABLE = False

from .models import BookingResult, Session

log = logging.getLogger(__name__)

QT_ORIGIN = "https://quicktickets.ru"
HALL_ORIGIN = "https://hall.quicktickets.ru"

# Селекторы для SPA hall.quicktickets.ru (на основе анализа живого DOM).
# Свободное место: div.hallPlace.free
# Выбранное место: div.hallPlace.t1 или div.hallPlace.t2
# Номер места: span.place_text_only внутри div.hallPlace
SEAT_SELECTORS_AVAILABLE = [
    "div.hallPlace.free",
    ".hallPlace.free",
    ".free.rectangle",
    ".free.hallPlace",
]
SEAT_SELECTORS_SELECTED = [
    "div.hallPlace.t1",
    "div.hallPlace.t2",
    ".hallPlace.t1",
    ".hallPlace.t2",
    ".hallPlace.selected",
]
BUY_BUTTON_SELECTORS = [
    # Кнопка «Купить» в зале / на странице сеанса может быть как button,
    # так и a/div/span с классами buttonMiddle/button и текстом внутри.
    "button.buttonMiddle:not(.button_disabled)",
    "a.buttonMiddle:not(.button_disabled)",
    "div.buttonMiddle:not(.button_disabled)",
    "button:has-text('Купить')",
    "button:has-text('Оплатить')",
    "button:has-text('Оформить')",
    "a:has-text('Купить')",
    "a:has-text('Оформить')",
    "div:has-text('Купить')",
    "div:has-text('Оформить')",
]
FORM_FIELD_SELECTORS = {
    "name": [
        "input[name='name']",
        "input[name='firstName']",
        "input[name='customerName']",
    ],
    "email": [
        "input[type='email']",
        "input[name='email']",
    ],
    "phone": [
        "input[type='tel']",
        "input[name='phone']",
        "input[name='customerPhone']",
    ],
}


@dataclass
class Contacts:
    name: str
    email: str
    phone: str


@dataclass
class BookingOptions:
    session: Session
    seats_wanted: int = 1
    budget_per_seat: float | None = None
    mode: str = "semiauto"  # 'semiauto' | 'auto'
    contacts: Contacts | None = None
    headful: bool = True
    manual_timeout: float = 300.0  # сколько ждём ручного выбора мест в semiauto
    artifact_dir: Path = Path("./artifacts")


# ---------------------------------------------------------------------------
# Вспомогательные функции работы с iframe зала
# ---------------------------------------------------------------------------


async def _dismiss_modal(frame: Frame) -> None:
    """Закрывает информационную модалку зала, если она открыта.

    На странице зала QuickTickets иногда появляется Bootstrap-модалка с
    крестиком закрытия. Она может быть вложена глубоко в DOM, поэтому ищем
    именно элементы закрытия внутри `.modal.show` и кликаем по ним явно.
    """

    modal_selectors = [".modal.show", "div.modal.show", ".modal.fade.show"]
    close_selectors = [
        ".modal.show .close",
        ".modal.show button[data-dismiss='modal']",
        ".modal.show .modal-header .close",
        ".modal.show .modal-header button",
        ".modal.show .btn-close",
        ".modal.show [aria-label='Close']",
        ".modal.show [title*='закры']",
        ".modal.show [class*='close']",
    ]

    try:
        modal = None
        for sel in modal_selectors:
            loc = frame.locator(sel).first
            if await loc.count() and await loc.is_visible():
                modal = loc
                break
        if modal is None:
            return

        log.debug("Обнаружена модалка зала, закрываю…")

        # 1) Сначала ищем явный крестик/кнопку закрытия внутри модалки.
        for sel in close_selectors:
            loc = frame.locator(sel).first
            try:
                if await loc.count() and await loc.is_visible():
                    try:
                        await loc.click(timeout=2_000, force=True)
                    except Exception:  # noqa: BLE001
                        await loc.evaluate("el => el.click()")
                    await frame.wait_for_timeout(500)
                    try:
                        await frame.wait_for_selector(
                            ".modal.show, .modal.fade.show, div.modal.show",
                            state="hidden",
                            timeout=3_000,
                        )
                    except Exception:  # noqa: BLE001
                        pass
                    log.info("Модальное окно закрыто (%s).", sel)
                    return
            except Exception:  # noqa: BLE001
                continue

        # 2) Если крестик не нашли, кликаем по первой видимой кнопке внутри modal.
        try:
            buttons = modal.locator("button, a, [role='button']")
            total = await buttons.count()
            for i in range(total):
                btn = buttons.nth(i)
                try:
                    if not await btn.is_visible():
                        continue
                    txt = ((await btn.text_content()) or "").strip().lower()
                    if not txt and not (await btn.get_attribute("class") or ""):
                        continue
                    if any(word in txt for word in ("закры", "close", "крест", "x", "ок")):
                        try:
                            await btn.click(timeout=2_000, force=True)
                        except Exception:  # noqa: BLE001
                            await btn.evaluate("el => el.click()")
                        await frame.wait_for_timeout(500)
                        log.info("Модальное окно закрыто по кнопке с текстом %r.", txt)
                        return
                except Exception:  # noqa: BLE001
                    continue
        except Exception:  # noqa: BLE001
            pass

        # 3) Последний шанс — Escape.
        try:
            await frame.press("body", "Escape")
        except Exception:  # noqa: BLE001
            pass
        await frame.wait_for_timeout(500)
        log.debug("Отправлен Escape для закрытия модалки.")
    except Exception as exc:  # noqa: BLE001
        log.debug("_dismiss_modal: %s", exc)


async def _first_matching(frame: Frame, selectors: list[str], timeout: float = 5.0):
    """Ждём первый из селекторов, который появится в ``frame``."""
    for sel in selectors:
        try:
            await frame.wait_for_selector(sel, timeout=timeout * 1000 / len(selectors))
            return sel
        except PWTimeout:
            continue
    return None


async def _get_hall_frame(page: Page, timeout: float = 30.0) -> Frame:
    """Находит iframe ``qt_hall`` и ждёт, пока React отрендерит зал.

    Признак готовности — появление хотя бы одного ``div.hallPlace``
    (свободного или занятого места).
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    hall: Frame | None = None

    # Шаг 1: найти сам iframe
    while loop.time() < deadline:
        for f in page.frames:
            if f.name == "qt_hall" or (f.url and HALL_ORIGIN in f.url):
                hall = f
                break
        if hall:
            break
        await asyncio.sleep(0.3)

    if not hall:
        raise TimeoutError("Не дождались iframe hall.quicktickets.ru")

    # Шаг 2: дождаться рендера зала (div.hallPlace появляется после postMessage)
    log.debug("Iframe найден (%s), ждём рендера зала…", hall.url)
    remaining = deadline - loop.time()
    try:
        await hall.wait_for_selector("div.hallPlace", timeout=max(remaining * 1000, 5000))
    except PWTimeout:
        raise TimeoutError(
            "Iframe зала загрузился, но div.hallPlace не появился — "
            "возможно, сеанс недоступен или сработала защита."
        )
    return hall


def _find_adjacent_seats(seats_info: list[dict], wanted: int) -> tuple[list[int], bool]:
    """Ищет лучший набор мест с приоритетом соседних мест в одном ряду."""
    if not seats_info or wanted <= 0:
        return [], False

    center_x = sum(seat["x"] for seat in seats_info) / len(seats_info)
    center_y = sum(seat["y"] for seat in seats_info) / len(seats_info)

    if wanted == 1:
        best = min(
            seats_info,
            key=lambda s: abs(s["x"] - center_x) + 0.5 * abs(s["y"] - center_y),
        )
        return [best["idx"]], True

    sorted_by_y = sorted(seats_info, key=lambda s: (s["y"], s["x"]))
    unique_y = sorted({seat["y"] for seat in seats_info})
    y_gaps = [
        unique_y[i + 1] - unique_y[i]
        for i in range(len(unique_y) - 1)
        if unique_y[i + 1] - unique_y[i] > 0
    ]
    row_y_threshold = max(12.0, min(y_gaps) / 3.0) if y_gaps else 20.0

    rows: list[list[dict]] = []
    current_row: list[dict] = []
    current_row_y: float | None = None
    for seat in sorted_by_y:
        if current_row_y is None or abs(seat["y"] - current_row_y) <= row_y_threshold:
            current_row.append(seat)
            current_row_y = sum(item["y"] for item in current_row) / len(current_row)
            continue
        rows.append(current_row)
        current_row = [seat]
        current_row_y = seat["y"]
    if current_row:
        rows.append(current_row)

    best_adjacent: tuple[float, list[int]] | None = None
    best_fallback: tuple[float, list[int]] | None = None

    for row in rows:
        row_sorted = sorted(row, key=lambda s: s["x"])
        if len(row_sorted) < wanted:
            continue

        x_diffs = [
            row_sorted[i + 1]["x"] - row_sorted[i]["x"]
            for i in range(len(row_sorted) - 1)
            if row_sorted[i + 1]["x"] - row_sorted[i]["x"] > 0
        ]
        typical_gap = min(x_diffs) if x_diffs else None
        max_adjacent_gap = (typical_gap * 1.6) if typical_gap else None

        for start_idx in range(len(row_sorted) - wanted + 1):
            group = row_sorted[start_idx : start_idx + wanted]
            indices = [s["idx"] for s in group]
            group_center_x = sum(s["x"] for s in group) / len(group)
            group_center_y = sum(s["y"] for s in group) / len(group)
            center_score = abs(group_center_x - center_x) + 0.35 * abs(group_center_y - center_y)

            x_gaps = [group[i + 1]["x"] - group[i]["x"] for i in range(len(group) - 1)]
            seat_numbers: list[int] = []
            for seat in group:
                digits = "".join(ch for ch in str(seat.get("title", "")) if ch.isdigit())
                if digits:
                    seat_numbers.append(int(digits))

            is_adjacent = bool(x_gaps) and all(gap > 0 for gap in x_gaps)
            if len(seat_numbers) == len(group):
                number_gaps = [
                    seat_numbers[i + 1] - seat_numbers[i]
                    for i in range(len(seat_numbers) - 1)
                ]
                is_adjacent = is_adjacent and all(gap == 1 for gap in number_gaps)
            else:
                if is_adjacent and max_adjacent_gap is not None:
                    is_adjacent = all(gap <= max_adjacent_gap for gap in x_gaps)
                if is_adjacent and typical_gap is not None:
                    is_adjacent = (max(x_gaps) - min(x_gaps)) <= max(6.0, typical_gap * 0.35)
                if is_adjacent and len(row_sorted) > wanted and typical_gap is not None:
                    is_adjacent = all(gap <= typical_gap * 1.25 for gap in x_gaps)

            if is_adjacent:
                score = center_score
                if best_adjacent is None or score < best_adjacent[0]:
                    best_adjacent = (score, indices)
            else:
                gap_penalty = sum(x_gaps) if x_gaps else 10_000.0
                score = center_score + gap_penalty
                if best_fallback is None or score < best_fallback[0]:
                    best_fallback = (score, indices)

    if best_adjacent is not None:
        return best_adjacent[1], True
    if best_fallback is not None:
        return best_fallback[1], False

    ranked = sorted(
        seats_info,
        key=lambda s: abs(s["x"] - center_x) + 0.5 * abs(s["y"] - center_y),
    )
    return [s["idx"] for s in ranked[:wanted]], False


async def _auto_pick_seats(frame: Frame, wanted: int) -> tuple[list[str], bool]:
    """Эвристический автовыбор: ищет соседние доступные места в центре зала.
    
    Если соседних мест нет, берёт wanted любых доступных мест.
    
    Возвращает (текстовое описание мест, были_ли_места_соседними).
    """
    sel = await _first_matching(frame, SEAT_SELECTORS_AVAILABLE, timeout=15.0)
    if not sel:
        sold_out_markers = [
            ".hallPlace",
            ".hall-place",
            "[class*='hallPlace']",
            "[class*='place']",
            ".hallRow",
            ".scheme",
            "svg",
        ]
        has_hall_markup = False
        for marker in sold_out_markers:
            try:
                if await frame.locator(marker).count() > 0:
                    has_hall_markup = True
                    break
            except Exception:  # noqa: BLE001
                continue

        if has_hall_markup:
            raise RuntimeError("SOLD_OUT: В зале сейчас нет доступных мест (возможно, всё раскуплено).")

        raise RuntimeError(
            "Не удалось определить разметку доступных мест в iframe зала. "
            "Используйте режим semiauto и выберите места вручную."
        )
    seats_info = await frame.evaluate(
        """(selector) => {
            const nodes = Array.from(document.querySelectorAll(selector));
            return nodes.map((el, i) => {
                // Координаты из inline-стиля (left/top в px)
                const style = el.style;
                const x = parseFloat(style.left) || 0;
                const y = parseFloat(style.top) || 0;
                // Текст места из span.place_text_only или textContent
                const span = el.querySelector('.place_text_only');
                const title = (span ? span.textContent : el.textContent || '').trim();
                return {idx: i, x, y, title};
            });
        }""",
        sel,
    )
    if not seats_info:
        raise RuntimeError("SOLD_OUT: В зале нет доступных мест (возможно, всё раскуплено).")

    idxs, is_adjacent = _find_adjacent_seats(seats_info, wanted)
    
    picked_titles: list[str] = []
    handles = await frame.query_selector_all(sel)
    for i in idxs:
        if i >= len(handles):
            continue
        try:
            await handles[i].scroll_into_view_if_needed()
            # Сначала пробуем обычный Playwright-клик; если pointer-events
            # заблокированы (модалка или overlay) — падаем на JS .click().
            try:
                await handles[i].click(timeout=3_000)
            except Exception:  # noqa: BLE001
                await handles[i].evaluate("el => el.click()")
            span = await handles[i].query_selector(".place_text_only")
            title = (await span.text_content() if span else None) or await handles[i].text_content()
            picked_titles.append((title or f"seat#{i}").strip())
            await asyncio.sleep(0.3)
        except Exception as exc:  # noqa: BLE001
            log.warning("Не удалось кликнуть место #%s: %s", i, exc)
    
    return picked_titles, is_adjacent


async def _wait_manual_selection(frame: Frame, wanted: int, timeout: float) -> list[str]:
    """В режиме ``semiauto`` ждём, пока пользователь сам выберет ``wanted``
    мест. Периодически опрашиваем DOM iframe.
    """
    log.info(
        "Выберите %s место(-а) в открытом окне браузера. Ожидание до %.0f сек…",
        wanted,
        timeout,
    )
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    last_count = -1
    while loop.time() < deadline:
        count = 0
        titles: list[str] = []
        for sel in SEAT_SELECTORS_SELECTED:
            try:
                handles = await frame.query_selector_all(sel)
            except Exception:  # noqa: BLE001
                handles = []
            if handles:
                for h in handles:
                    span = await h.query_selector(".place_text_only")
                    t = (await span.text_content() if span else None) or await h.text_content() or ""
                    titles.append(t.strip())
                count = len(handles)
                break
        if count != last_count:
            log.info("Сейчас выбрано мест: %s", count)
            last_count = count
        if count >= wanted:
            return titles
        await asyncio.sleep(1.0)
    raise TimeoutError(
        f"За {timeout:.0f} сек. не было выбрано {wanted} мест(-а)."
    )


# ---------------------------------------------------------------------------
# Главный сценарий
# ---------------------------------------------------------------------------


class QuickticketsDriver:
    """Управляет Playwright-сессией и выбирает места на странице сеанса."""

    def __init__(self, opts: BookingOptions):
        self.opts = opts
        self._pw: Playwright | None = None
        self._browser: Browser | None = None
        self._context: BrowserContext | None = None
        self._page: Page | None = None

    async def __aenter__(self) -> "QuickticketsDriver":
        if not PLAYWRIGHT_AVAILABLE:
            raise ModuleNotFoundError("playwright is not installed")
        self._pw = await async_playwright().start()
        self._browser = await self._pw.chromium.launch(
            headless=not self.opts.headful,
            args=["--disable-blink-features=AutomationControlled"],
        )
        self._context = await self._browser.new_context(
            locale="ru-RU",
            viewport={"width": 1366, "height": 900},
            user_agent=(
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
            ),
        )
        self._page = await self._context.new_page()
        return self

    async def __aexit__(self, exc_type, exc, tb):
        if self._context:
            await self._context.close()
        if self._browser:
            await self._browser.close()
        if self._pw:
            await self._pw.stop()

    async def run(self) -> BookingResult:
        assert self._page is not None
        opts = self.opts
        opts.artifact_dir.mkdir(parents=True, exist_ok=True)
        result = BookingResult(session=opts.session)

        session_url = opts.session.qt_url
        log.info("Открываю страницу сеанса: %s", session_url)
        try:
            await self._page.goto(session_url, wait_until="domcontentloaded", timeout=60_000)
        except PWTimeout:
            log.warning("Первый переход на страницу сеанса не успел за 60 сек., повторяю…")
            await self._page.goto(session_url, wait_until="domcontentloaded", timeout=60_000)

        result.session_url = self._page.url

        try:
            hall = await _get_hall_frame(self._page)
        except TimeoutError as exc:
            result.status = "error"
            result.message = str(exc)
            return result

        log.info("Зал загрузился, iframe: %s", hall.url)

        # Закрываем информационную модалку, если она перекрывает схему зала.
        await _dismiss_modal(hall)
        await _dismiss_modal(self._page)
        await hall.wait_for_timeout(500)
        await _dismiss_modal(hall)
        await _dismiss_modal(self._page)

        try:
            if opts.mode == "auto":
                result.seats, result.seats_adjacent = await _auto_pick_seats(hall, opts.seats_wanted)
            else:
                seats_list = await _wait_manual_selection(
                    hall, opts.seats_wanted, opts.manual_timeout
                )
                result.seats = seats_list
                result.seats_adjacent = True  # В semiauto пользователь выбирает сам
        except Exception as exc:  # noqa: BLE001
            message = str(exc)
            if message.startswith("SOLD_OUT:"):
                result.status = "sold_out"
                result.message = message.removeprefix("SOLD_OUT:").strip()
            else:
                result.status = "error"
                result.message = f"Не удалось выбрать места: {exc}"
            return result

        log.info("Выбраны места: %s (соседние: %s)", result.seats, result.seats_adjacent)

        # Сохраняем исходный скрин выбора мест, затем инициируем реальный
        # сценарий QuickTickets: action из iframe -> initAnytickets -> submit формы.
        await self._save_hall_screenshot(result)
        await self._save_artifacts(result, tag="hall-selected")
        await self._prepare_parent_order_bridge()

        buy_clicked = await self._click_buy(hall)
        if not buy_clicked:
            buy_clicked = await self._click_buy(self._page)
        if not buy_clicked:
            result.status = "error"
            result.message = "Места выбраны, но кнопку «Купить» нажать не удалось."
            return result

        order_ready = await self._wait_for_order_transition()
        if not order_ready:
            log.warning("После клика по «Купить» переход на оформление не произошёл, пробую форсировать parent-flow.")
            forced = await self._force_parent_order_submission()
            if forced:
                order_ready = await self._wait_for_order_transition(timeout_ms=15_000)

        if not order_ready:
            result.status = "error"
            result.message = (
                "Места выбраны, но QuickTickets не перевёл их в резерв: "
                "не удалось завершить initAnytickets/submit order form."
            )
            await self._save_artifacts(result, tag="order-failed")
            return result

        result.session_url = session_url
        result.order_url = self._page.url if "/ordering/" in self._page.url else None
        result.reserved_at = datetime.now()
        result.unfreeze_at = result.reserved_at + timedelta(minutes=3)
        result.status = "ready_for_payment"
        if result.seats_adjacent:
            result.message = (
                "✅ Выбраны соседние места, инициирован заказ и места переведены во временный резерв. "
                "Отправлена ссылка на страницу заказа и скриншот."
            )
        else:
            result.message = (
                "⚠️ Соседних мест не нашлось, выбраны доступные места. "
                "Заказ инициирован, места переведены во временный резерв."
            )
        await self._save_artifacts(result, tag="order-ready")
        return result

    async def _click_buy(self, ctx: Page | Frame) -> bool:
        async def _click_locator(locator, sel: str) -> bool:
            try:
                if await locator.count() == 0:
                    return False
                if not await locator.is_visible():
                    return False
                try:
                    await locator.scroll_into_view_if_needed()
                except Exception:  # noqa: BLE001
                    pass
                # Пробуем несколько способов: обычный click, force click, JS click.
                for click_mode in ("normal", "force", "js"):
                    try:
                        if click_mode == "normal":
                            await locator.click(timeout=3_000)
                        elif click_mode == "force":
                            await locator.click(timeout=3_000, force=True)
                        else:
                            await locator.evaluate("el => el.click()")
                        log.info("Кликнул '%s' (%s)", sel, click_mode)
                        return True
                    except Exception:  # noqa: BLE001
                        continue
                return False
            except Exception:  # noqa: BLE001
                return False

        # 1) Самый точный вариант: видимая кнопка/ссылка с текстом «Купить».
        for sel in [
            "button:has-text('Купить')",
            "a:has-text('Купить')",
            "[role='button']:has-text('Купить')",
            "button:has-text('Оформить')",
            "button:has-text('Оплатить')",
            "a:has-text('Оформить')",
            "div.buttonMiddle:has-text('Купить')",
        ]:
            try:
                locator = ctx.locator(sel).first
                if await _click_locator(locator, sel):
                    return True
            except Exception:  # noqa: BLE001
                continue

        # 2) Кнопки/ссылки с классом buttonMiddle/button.
        for sel in BUY_BUTTON_SELECTORS:
            try:
                locator = ctx.locator(sel).first
                if await _click_locator(locator, sel):
                    return True
            except Exception:  # noqa: BLE001
                continue

        # 3) Если селекторы не сработали, ищем видимый контейнер с текстом.
        try:
            candidates = ctx.locator("button, a, div, span")
            count = await candidates.count()
            for i in range(count):
                loc = candidates.nth(i)
                try:
                    if not await loc.is_visible():
                        continue
                    text = " ".join(((await loc.text_content()) or "").split())
                    if not text or ("Купить" not in text and "Оформ" not in text and "Оплат" not in text):
                        continue
                    cls = (await loc.get_attribute("class")) or ""
                    if "button" not in cls.lower() and "buy" not in cls.lower() and "middle" not in cls.lower():
                        continue
                    # Поднимаемся к ближайшему кликабельному предку, если нашли текстовый узел.
                    target = loc
                    try:
                        target = loc.locator("xpath=ancestor-or-self::*[self::button or self::a or contains(@class,'button') or contains(@class,'buttonMiddle')][1]").first
                    except Exception:  # noqa: BLE001
                        target = loc
                    if await _click_locator(target, f"fallback-text-match:{text}"):
                        return True
                except Exception:  # noqa: BLE001
                    continue
        except Exception:  # noqa: BLE001
            pass

        return False

    async def _prepare_parent_order_bridge(self) -> None:
        assert self._page is not None
        try:
            await self._page.evaluate(
                """() => {
                    if (window.__ogattOrderBridgeInstalled) {
                        return;
                    }
                    window.__ogattOrderBridgeInstalled = true;
                    window.__ogattLastQtAction = null;
                    window.addEventListener('message', (event) => {
                        if (event.origin !== 'https://hall.quicktickets.ru') {
                            return;
                        }
                        if (event.data && event.data.type === 'action') {
                            window.__ogattLastQtAction = event.data;
                        }
                    });
                }"""
            )
        except Exception as exc:  # noqa: BLE001
            log.debug("Не удалось установить bridge для action-событий: %s", exc)

    async def _wait_for_order_transition(self, timeout_ms: int = 10_000) -> bool:
        assert self._page is not None
        try:
            await self._page.wait_for_url("**/ordering/**", timeout=timeout_ms)
            log.info("QuickTickets перевёл страницу на оформление заказа: %s", self._page.url)
            return True
        except Exception:  # noqa: BLE001
            pass

        try:
            await self._page.wait_for_function(
                """() => {
                    const codes = document.querySelector('#order input[name="anyticketsCodes"]');
                    return !!codes && !!codes.value && codes.value.trim().length > 0;
                }""",
                timeout=timeout_ms,
            )
            log.info("QuickTickets заполнил hidden order form, но navigation ещё не завершён.")
            return True
        except Exception:  # noqa: BLE001
            return False

    async def _force_parent_order_submission(self) -> bool:
        assert self._page is not None
        try:
            result = await self._page.evaluate(
                """async () => {
                    const actionData = window.__ogattLastQtAction;
                    if (!actionData || typeof actionData.selectedItemsInfo === 'undefined') {
                        return {ok: false, reason: 'missing_action_data'};
                    }

                    const form = document.querySelector('#order');
                    const codesInput = document.querySelector('#order input[name="anyticketsCodes"]');
                    const countInput = document.querySelector('#order input[name="selectAnyplacesCount"]');
                    const organisationAlias = document.querySelector('#order input[name="organisationAlias"]')?.value;
                    const elemType = document.querySelector('#order input[name="elemType"]')?.value;
                    const elemId = document.querySelector('#order input[name="elemId"]')?.value;

                    if (!form || !codesInput || !countInput || !organisationAlias || !elemType || !elemId) {
                        return {ok: false, reason: 'missing_order_form'};
                    }

                    const payload = {
                        organisationAlias,
                        elemType,
                        elemId: Number(elemId),
                        collectiveSell: actionData.action === 'collectiveSell' ? 1 : 0,
                        ...actionData.selectedItemsInfo,
                    };

                    const response = await fetch('/ordering/initAnytickets', {
                        method: 'POST',
                        credentials: 'same-origin',
                        headers: {
                            'Content-Type': 'application/x-www-form-urlencoded; charset=UTF-8',
                            'X-Requested-With': 'XMLHttpRequest',
                        },
                        body: new URLSearchParams(
                            Object.entries(payload).flatMap(([key, value]) => {
                                if (Array.isArray(value)) {
                                    return value.map((item) => [key + '[]', String(item)]);
                                }
                                if (value === null || typeof value === 'undefined') {
                                    return [];
                                }
                                return [[key, String(value)]];
                            })
                        ).toString(),
                    });

                    const data = await response.json();
                    if (!response.ok || data.result !== 'success') {
                        return {
                            ok: false,
                            reason: 'init_failed',
                            responseStatus: response.status,
                            data,
                        };
                    }

                    codesInput.value = (data.data.anyticketsCodes || []).join(',');
                    countInput.value = String(data.data.selectAnyplacesCount || '');
                    form.submit();
                    return {ok: true};
                }"""
            )
            if result and result.get("ok"):
                log.info("Форсированный parent-flow initAnytickets + submit выполнен успешно.")
                return True
            log.warning("Форсированный parent-flow не удался: %s", result)
            return False
        except Exception as exc:  # noqa: BLE001
            log.warning("Ошибка форсированного parent-flow QuickTickets: %s", exc)
            return False

    async def _fill_contacts(self, c: Contacts) -> None:
        assert self._page is not None
        async def _fill(selectors: list[str], value: str) -> None:
            for sel in selectors:
                loc = self._page.locator(sel).first
                try:
                    if await loc.count() and await loc.is_visible():
                        await loc.fill(value)
                        return
                except Exception:  # noqa: BLE001
                    continue

        await _fill(FORM_FIELD_SELECTORS["name"], c.name)
        await _fill(FORM_FIELD_SELECTORS["email"], c.email)
        await _fill(FORM_FIELD_SELECTORS["phone"], c.phone)
        log.info("Контактные данные заполнены.")

    async def _save_hall_screenshot(self, result: BookingResult) -> None:
        """Сохраняет скриншот зала с выбранными местами."""
        assert self._page is not None
        stem = f"order_{result.session.qt_session_id}_hall_seats"
        png = self.opts.artifact_dir / f"{stem}.png"
        try:
            await self._page.screenshot(path=str(png), full_page=False)
            result.screenshot_path = str(png)
            log.info("Скриншот выбранных мест сохранён: %s", png)
        except Exception as exc:  # noqa: BLE001
            log.warning("Не удалось сохранить скриншот мест: %s", exc)

    async def _try_extract_total(self) -> float | None:
        assert self._page is not None
        try:
            txt = await self._page.evaluate(
                "() => document.body.innerText.match(/(?:Итого|К оплате|Сумма)[^\\d]*(\\d[\\d\\s]*)/i)?.[1]"
            )
            if txt:
                return float(txt.replace(" ", ""))
        except Exception:  # noqa: BLE001
            return None
        return None

    async def _save_artifacts(self, result: BookingResult, *, tag: str) -> None:
        assert self._page is not None
        stem = f"order_{result.session.qt_session_id}_{tag}"
        js = self.opts.artifact_dir / f"{stem}.json"
        try:
            js.write_text(
                json.dumps(result.to_dict(), ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("JSON не сохранён: %s", exc)


def load_contacts_from_env() -> Contacts | None:
    name = os.getenv("QT_NAME")
    email = os.getenv("QT_EMAIL")
    phone = os.getenv("QT_PHONE")
    if not (name and email and phone):
        return None
    return Contacts(name=name, email=email, phone=phone)
