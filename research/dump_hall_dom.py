"""Дамп живого DOM iframe зала для анализа селекторов мест.

Запуск:
    .venv/bin/python research/dump_hall_dom.py
"""
import asyncio
from pathlib import Path
from playwright.async_api import async_playwright

QT_URL = "https://quicktickets.ru/orel-teatr-turgeneva/s1360"
HALL_ORIGIN = "https://hall.quicktickets.ru"
OUT = Path(__file__).parent / "hall_live_dom.html"


async def main():
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=True,
            args=["--disable-blink-features=AutomationControlled"],
        )
        ctx = await browser.new_context(
            locale="ru-RU",
            viewport={"width": 1366, "height": 900},
            user_agent=(
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
            ),
        )
        page = await ctx.new_page()

        # Перехватываем postMessage из родителя в iframe
        messages = []
        await page.expose_function("__captureMsg", lambda m: messages.append(m))
        await page.add_init_script("""
            const orig = window.postMessage.bind(window);
            window.postMessage = function(data, origin, transfer) {
                try { window.__captureMsg(JSON.stringify(data)); } catch(e) {}
                return orig(data, origin, transfer);
            };
        """)

        await page.goto(QT_URL, wait_until="domcontentloaded", timeout=30000)

        # Ждём iframe зала
        hall = None
        for _ in range(120):
            for f in page.frames:
                if f.name == "qt_hall" or (f.url and HALL_ORIGIN in f.url):
                    hall = f
                    break
            if hall:
                break
            await asyncio.sleep(0.5)

        if not hall:
            print("Iframe не найден!")
            await browser.close()
            return

        print(f"Iframe URL: {hall.url}")

        # Ждём пока React отрендерит зал (исчезнет .loading или появятся места)
        print("Ждём рендера зала (до 30 сек)…")
        for _ in range(60):
            classes = await hall.evaluate("""() => {
                const el = document.querySelector('#qt_hall');
                if (!el) return [];
                const all = el.querySelectorAll('*');
                const cls = new Set();
                all.forEach(e => e.classList.forEach(c => cls.add(c)));
                return [...cls].sort();
            }""")
            # Если есть что-то кроме loading/spinner — зал загрузился
            non_loading = [c for c in classes if c not in ('loading', 'logo', 'spinner', 'text', '')]
            if non_loading:
                print(f"Зал загружен, классы: {classes}")
                break
            await asyncio.sleep(0.5)
        else:
            print(f"Зал не загрузился за 30 сек. Классы: {classes}")

        # Дополнительная пауза
        await asyncio.sleep(2)

        html = await hall.content()
        OUT.write_text(html, encoding="utf-8")
        print(f"DOM сохранён: {OUT} ({len(html)} байт)")

        # Все уникальные классы
        all_classes = await hall.evaluate("""() => {
            const all = document.querySelectorAll('*');
            const cls = new Set();
            all.forEach(e => e.classList.forEach(c => cls.add(c)));
            return [...cls].sort();
        }""")
        print("Все классы в iframe:", all_classes)

        # Пробуем найти кликабельные места
        for sel in [
            "svg g[class]", "svg circle", "svg rect", "svg path[class]",
            "[class*='seat']", "[class*='place']", "[class*='Seat']", "[class*='Place']",
            "[class*='cell']", "[class*='ticket']", "[class*='row']",
            "g", "circle", "rect",
        ]:
            count = await hall.evaluate(f"() => document.querySelectorAll({sel!r}).length")
            if count:
                print(f"  {sel!r}: {count} элементов")

        if messages:
            print("postMessage перехвачены:", messages[:5])

        await browser.close()


asyncio.run(main())
