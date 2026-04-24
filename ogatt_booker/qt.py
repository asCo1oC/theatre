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
from pathlib import Path

from playwright.async_api import (
    Browser,
    BrowserContext,
    Frame,
    Page,
    Playwright,
    TimeoutError as PWTimeout,
    async_playwright,
)

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


async def _auto_pick_seats(frame: Frame, wanted: int) -> list[str]:
    """Эвристический автовыбор: берёт доступные места, сортирует по
    близости к визуальному центру зала и кликает ``wanted`` штук.

    Координаты берём из CSS-свойств ``left``/``top`` (стиль inline),
    т.к. ``getBoundingClientRect`` возвращает нули для скрытых элементов
    в headless-режиме.

    Возвращает текстовое описание выбранных мест (для отчёта).
    """
    sel = await _first_matching(frame, SEAT_SELECTORS_AVAILABLE, timeout=15.0)
    if not sel:
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
        raise RuntimeError("В зале нет доступных мест (возможно, всё раскуплено).")

    xs = [s["x"] for s in seats_info]
    ys = [s["y"] for s in seats_info]
    cx = (min(xs) + max(xs)) / 2
    # Сцена обычно сверху, поэтому "лучше" = ближе к центру по X
    # и в средней части зала по Y.
    y_min, y_max = min(ys), max(ys)
    y_target = y_min + (y_max - y_min) * 0.35

    def score(s):
        return abs(s["x"] - cx) + 0.5 * abs(s["y"] - y_target)

    ranked = sorted(seats_info, key=score)
    picked_titles: list[str] = []
    idxs = [s["idx"] for s in ranked[:wanted]]
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
    return picked_titles


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
    """Управляет Playwright-сессией и доводит заказ до страницы оплаты."""

    def __init__(self, opts: BookingOptions):
        self.opts = opts
        self._pw: Playwright | None = None
        self._browser: Browser | None = None
        self._context: BrowserContext | None = None
        self._page: Page | None = None

    async def __aenter__(self) -> "QuickticketsDriver":
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
                result.seats = await _auto_pick_seats(hall, opts.seats_wanted)
            else:
                result.seats = await _wait_manual_selection(
                    hall, opts.seats_wanted, opts.manual_timeout
                )
        except Exception as exc:  # noqa: BLE001
            result.status = "error"
            result.message = f"Не удалось выбрать места: {exc}"
            return result

        log.info("Выбраны места: %s", result.seats)

        # Нажимаем «Купить/Оформить» — сначала ищем в iframe зала, затем в родителе.
        buy_clicked = await self._click_buy(hall) or await self._click_buy(self._page)
        if not buy_clicked:
            result.status = "error"
            result.message = "Не нашли кнопку перехода к оплате после выбора мест."
            return result

        # После клика QuickTickets может обновить URL с задержкой.
        try:
            await self._page.wait_for_url("**/ordering/**", timeout=90_000)
        except PWTimeout:
            result.status = "error"
            result.message = "Нет перехода на страницу оформления заказа."
            await self._save_artifacts(result, tag="no-ordering")
            return result
        await self._page.wait_for_load_state("domcontentloaded")
        log.info("Страница заказа: %s", self._page.url)

        if opts.contacts:
            await self._fill_contacts(opts.contacts)

        result.order_url = self._page.url
        result.total_price = await self._try_extract_total()
        await self._save_artifacts(result, tag="ordering")
        result.status = "ready_for_payment"
        result.message = (
            "Готово: форма контактов заполнена, осталось вручную проверить данные "
            "и подтвердить оплату на этой странице."
        )
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
        png = self.opts.artifact_dir / f"{stem}.png"
        js = self.opts.artifact_dir / f"{stem}.json"
        try:
            await self._page.screenshot(path=str(png), full_page=True)
            result.screenshot_path = str(png)
        except Exception as exc:  # noqa: BLE001
            log.warning("Скрин не сохранён: %s", exc)
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
