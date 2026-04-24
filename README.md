# ogatt-booker

Автоматизация бронирования билетов на спектакли **Орловского государственного академического театра им. И. С. Тургенева** (<https://ogatt.ru>).

## Как устроен сайт

Театр сам билеты не продаёт — все кнопки «Купить билет» ведут на внешнюю кассу [QuickTickets](https://quicktickets.ru):

```
https://quicktickets.ru/orel-teatr-turgeneva/s<ID>
```

где `<ID>` — идентификатор конкретного сеанса (связка спектакль + дата + время + сцена).

Цепочка покупки:

1. Афиша `ogatt.ru/poster/` (обычный HTML, блоки `.afisha-item`) → достаём `<ID>` и метаданные.
2. Страница сеанса `quicktickets.ru/.../s<ID>` — оболочка с iframe `qt_hall`.
3. Iframe `hall.quicktickets.ru` — React‑SPA, рендерит схему зала, общается с родителем через `postMessage`.
4. После выбора мест инициируется `POST /ordering/initAnytickets` → переход на `POST /ordering/anytickets` (страница заказа).
5. На странице заказа заполняется гостевая форма, затем эквайринг.

Из‑за закрытого React‑бандла в зале и защиты ddos-guard + Yandex SmartCaptcha чистый HTTP‑клиент будет хрупким, поэтому используем **Playwright**.

> ⚠️ Скрипт **принципиально не проводит платёж**. Он останавливается на форме оплаты, заполняет контактные данные и присылает ссылку — вы решаете, подтверждать ли оплату, вручную.

## Установка

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python -m playwright install chromium
cp .env.example .env
# отредактируйте .env: QT_NAME / QT_EMAIL / QT_PHONE, при желании TG_BOT_TOKEN/TG_CHAT_ID
```

## Использование

Просмотр текущей афиши:

```bash
python -m ogatt_booker list
python -m ogatt_booker list --title "онегин"
```

Бронирование места (интерактивный режим — вы сами кликаете места, скрипт заполняет форму и ловит ссылку оплаты):

```bash
python -m ogatt_booker book --title "Земля Эльзы" --date 2026-04-29 --seats 2
```

Бронирование по прямому QT‑ID (полезно, когда афиша на сайте ещё не появилась, но ссылка `s<ID>` уже открывается):

```bash
python -m ogatt_booker book --qt-id 1325 --seats 1
```

Экспериментальный автовыбор мест (эвристика «ближе к центру зала»):

```bash
python -m ogatt_booker book --qt-id 1325 --seats 2 --mode auto
```

Headless‑режим:

```bash
HEADFUL=false python -m ogatt_booker book --qt-id 1325 --seats 1
```

Мониторинг появления билетов и автоматическое бронирование (команда `watch`):

```bash
# Следить за «Гамлетом», бронировать 2 места, проверять каждые 2 минуты
python -m ogatt_booker watch --title "Гамлет" --seats 2

# Остановиться после первого успешного бронирования
python -m ogatt_booker watch --title "Гамлет" --seats 2 --max-sessions 1

# Более частые проверки (каждые 30 сек)
python -m ogatt_booker watch --title "Гамлет" --interval 30
```

`watch` работает в headless‑режиме по умолчанию (`HEADFUL=false`). При появлении нового сеанса автоматически открывает браузер, выбирает места эвристикой «ближе к центру», доходит до страницы оформления заказа и сохраняет handoff-артефакт. В Telegram приходит ссылка на локальную страницу handoff, которую можно открыть с телефона и использовать как точку входа в тот же серверный браузер.

Фильтрация афиши по диапазону дат:

```bash
python -m ogatt_booker list --date-from 2026-05-01 --date-to 2026-05-31
```

## Что получаете на выходе

- Окно браузера, открытое на странице оформления заказа на сервере.
- `artifacts/order_<QT_ID>_ordering.png` — полноэкранный скриншот страницы оформления.
- `artifacts/order_<QT_ID>_ordering.json` — JSON с выбранными местами, суммой, URL.
- `artifacts/handoff/handoff_s<QT_ID>.html` — HTML-страница для передачи пользователю на телефон.
- Сообщение в консоли и (если настроен) в Telegram со ссылкой на handoff-страницу.

## Ограничения

- Автовыбор мест эвристический: `hall.quicktickets.ru` — React‑SPA без публичного API, поэтому при изменении разметки селекторы могут потребовать правки (см. константы `SEAT_SELECTORS_*` в `ogatt_booker/qt.py`).
- QuickTickets может показывать SmartCaptcha при подозрительной активности — пройдите её вручную в окне браузера, скрипт продолжит работу.
- Максимум мест за раз у QuickTickets — 8 (ограничение кассы).
- Handoff-страница сама по себе не является полноценным remote desktop; для реального управления той же сессией нужен внешний VNC/noVNC/remote-browser слой на сервере.
- Парсер афиши берёт только текущую опубликованную афишу (ближайшие 1–2 месяца). Для далёких сеансов используйте прямой `--qt-id`.

## Тесты

Оффлайн‑тест парсера (не ходит в сеть, использует сохранённую страницу в `research/`):

```bash
pip install pytest
pytest -q
```

## Структура проекта

```
ogatt_booker/
  __init__.py
  __main__.py      # CLI (list / book / watch)
  afisha.py        # парсер ogatt.ru/poster/
  qt.py            # Playwright-драйвер quicktickets.ru
  watcher.py       # цикл мониторинга афиши и автобронирования
  notifier.py      # console + Telegram-уведомления
  models.py        # dataclass Session / BookingResult
tests/
  test_afisha.py   # оффлайн-тест парсера
  test_watcher.py  # юнит-тесты цикла мониторинга (без сети)
research/          # сохранённые HTML-фикстуры для исследования и тестов
```

## Этика и ToS

Скрипт предназначен для личного использования — помочь купить билет в один клик, а не для массового скальпинга или перепродажи. Не запускайте его в несколько потоков и учитывайте правила <https://quicktickets.ru>.
