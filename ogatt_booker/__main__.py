"""CLI-точка входа: ``python -m ogatt_booker …``

Использование::

    python -m ogatt_booker list
    python -m ogatt_booker book --title "Земля Эльзы" --date 2026-04-29 --seats 2
    python -m ogatt_booker book --qt-id 1325 --seats 1 --mode auto

По умолчанию включается режим ``semiauto``: открывается окно браузера,
пользователь сам выбирает места, а скрипт перехватывает переход на
страницу оплаты, подставляет контакты и шлёт уведомление.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from datetime import date, datetime, time
from pathlib import Path

from dotenv import load_dotenv
from rich.console import Console
from rich.logging import RichHandler
from rich.table import Table

from .afisha import load_afisha, find_sessions, parse_user_datetime
from .models import Session
from .notifier import notify_console, notify_telegram
from .qt import BookingOptions, QuickticketsDriver, load_contacts_from_env
from .watcher import watch as _watch


console = Console()


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(message)s",
        datefmt="[%X]",
        handlers=[RichHandler(console=console, rich_tracebacks=True, show_path=False)],
    )


def _print_sessions(sessions: list[Session]) -> None:
    table = Table(title="Афиша ОГАТ им. Тургенева", show_lines=False)
    table.add_column("Дата", no_wrap=True)
    table.add_column("Время", no_wrap=True)
    table.add_column("Сцена")
    table.add_column("Спектакль")
    table.add_column("Возраст", no_wrap=True)
    table.add_column("QT", no_wrap=True, style="cyan")
    for s in sessions:
        table.add_row(
            s.show_date.strftime("%d.%m.%Y"),
            s.show_time.strftime("%H:%M"),
            s.scene,
            s.title,
            s.age_rating or "",
            f"s{s.qt_session_id}",
        )
    console.print(table)


def _pick_session(args: argparse.Namespace) -> Session:
    """Находит сеанс по QT-ID либо по названию + дате/времени."""
    if args.qt_id:
        if not args.no_afisha:
            all_sessions = load_afisha()
            matches = [s for s in all_sessions if s.qt_session_id == args.qt_id]
        else:
            matches = []
        if not matches:
            # Не нашли в текущей афише — всё равно позволим продолжить, собрав
            # минимальный Session вручную (некоторые далёкие даты афиша не
            # показывает, но страница /s<ID> уже работает).
            console.print(
                f"[yellow]QT-ID s{args.qt_id} не найден в текущей афише, "
                "будет собран минимальный объект сеанса.[/yellow]"
            )
            return Session(
                title=f"Session s{args.qt_id}",
                show_date=date.today(),
                show_time=time(0, 0),
                scene="",
                qt_session_id=args.qt_id,
                qt_url=f"https://quicktickets.ru/orel-teatr-turgeneva/s{args.qt_id}",
            )
        return matches[0]

    if not (args.title or args.date):
        raise SystemExit(
            "Нужен либо --qt-id, либо связка --title и/или --date для поиска."
        )

    on_date: date | None = None
    at_time: time | None = None
    if args.date:
        on_date, at_time = parse_user_datetime(args.date)
    if args.time and not at_time:
        hh, mm = args.time.split(":")
        at_time = time(int(hh), int(mm))

    all_sessions = load_afisha()
    matches = find_sessions(
        all_sessions,
        title=args.title,
        on_date=on_date,
        at_time=at_time,
        scene=args.scene,
    )
    if not matches:
        console.print("[red]Под критерии не подошёл ни один сеанс.[/red]")
        _print_sessions(all_sessions[:20])
        raise SystemExit(1)
    if len(matches) > 1 and not args.pick_first:
        console.print(
            f"[yellow]Найдено {len(matches)} сеанс(-ов), уточните --date/--time "
            "или добавьте --pick-first:[/yellow]"
        )
        _print_sessions(matches)
        raise SystemExit(2)
    return matches[0]


def cmd_list(args: argparse.Namespace) -> int:
    sessions = load_afisha()
    date_from = None
    date_to = None
    if getattr(args, "date_from", None):
        date_from, _ = parse_user_datetime(args.date_from)
    if getattr(args, "date_to", None):
        date_to, _ = parse_user_datetime(args.date_to)
    if args.title or args.scene or date_from or date_to:
        sessions = find_sessions(
            sessions,
            title=args.title,
            scene=args.scene,
            date_from=date_from,
            date_to=date_to,
        )
    _print_sessions(sessions)
    return 0


def cmd_book(args: argparse.Namespace) -> int:
    session = _pick_session(args)
    console.print(
        f"[green]Бронирую:[/green] {session.title} · "
        f"{session.show_date:%d.%m.%Y} {session.show_time:%H:%M} · "
        f"{session.scene} (s{session.qt_session_id})"
    )

    contacts = load_contacts_from_env()
    if not contacts:
        console.print(
            "[yellow]Нет QT_NAME/QT_EMAIL/QT_PHONE в окружении — форма контактов не будет заполнена автоматически.[/yellow]"
        )

    opts = BookingOptions(
        session=session,
        seats_wanted=args.seats,
        budget_per_seat=args.budget,
        mode=args.mode,
        contacts=contacts,
        headful=(os.getenv("HEADFUL", "true").lower() != "false"),
        manual_timeout=args.manual_timeout,
        artifact_dir=Path(args.artifact_dir),
    )

    async def _go() -> None:
        async with QuickticketsDriver(opts) as drv:
            result = await drv.run()
        notify_console(result)
        notify_telegram(result)

    asyncio.run(_go())
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="ogatt_booker",
        description="Автоматизация бронирования билетов на спектакли ОГАТ им. Тургенева.",
    )
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)

    pl = sub.add_parser("list", help="Показать текущую афишу")
    pl.add_argument("--title", help="Фильтр по названию (подстрока)")
    pl.add_argument("--scene", help="Фильтр по сцене")
    pl.add_argument("--date-from", dest="date_from", help="Показать сеансы начиная с даты YYYY-MM-DD")
    pl.add_argument("--date-to", dest="date_to", help="Показать сеансы до даты YYYY-MM-DD (включительно)")
    pl.set_defaults(func=cmd_list)

    pb = sub.add_parser("book", help="Открыть выбор мест и довести до оплаты")
    pb.add_argument("--qt-id", type=int, help="Прямой ID сеанса в QuickTickets (из ссылки /s<ID>)")
    pb.add_argument("--no-afisha", action="store_true", help="Не загружать афишу при использовании --qt-id (быстрее)")
    pb.add_argument("--title", help="Название спектакля (подстрока)")
    pb.add_argument("--date", help="Дата ISO 'YYYY-MM-DD' или 'YYYY-MM-DD HH:MM'")
    pb.add_argument("--time", help="Время 'HH:MM' (если не указано в --date)")
    pb.add_argument("--scene", help="Сцена")
    pb.add_argument("--seats", type=int, default=1, help="Сколько мест")
    pb.add_argument(
        "--mode",
        choices=("semiauto", "auto"),
        default="semiauto",
        help="semiauto: места выбирает человек; auto: эвристика 'ближе к центру' (экспериментально)",
    )
    pb.add_argument("--budget", type=float, help="Бюджет на одно место (справочно)")
    pb.add_argument("--pick-first", action="store_true", help="Брать первый, если найдено несколько")
    pb.add_argument("--manual-timeout", type=float, default=300.0, help="Сколько ждать ручного выбора, сек.")
    pb.add_argument("--artifact-dir", default="./artifacts", help="Куда класть скрины и json отчётов")
    pb.set_defaults(func=cmd_book)

    pw = sub.add_parser(
        "watch",
        help="Следить за появлением билетов и автоматически бронировать",
    )
    pw.add_argument("--title", required=True, help="Название спектакля (подстрока)")
    pw.add_argument("--seats", type=int, default=2, help="Сколько мест бронировать (по умолчанию 2)")
    pw.add_argument("--interval", type=float, default=120.0, help="Интервал проверки афиши, сек (по умолчанию 120)")
    pw.add_argument("--artifact-dir", default="./artifacts", help="Куда класть скрины и JSON")
    pw.add_argument("--max-sessions", type=int, default=None, dest="max_sessions", help="Остановиться после N успешных бронирований")
    pw.set_defaults(func=cmd_watch)

    return p


def cmd_watch(args: argparse.Namespace) -> int:
    contacts = load_contacts_from_env()
    if not contacts:
        console.print(
            "[yellow]Нет QT_NAME/QT_EMAIL/QT_PHONE — форма контактов не будет заполнена.[/yellow]"
        )
    tg_token = os.getenv("TG_BOT_TOKEN")
    tg_chat_id = os.getenv("TG_CHAT_ID")
    if not (tg_token and tg_chat_id):
        console.print(
            "[yellow]TG_BOT_TOKEN/TG_CHAT_ID не заданы — Telegram-уведомления отключены.[/yellow]"
        )

    async def _go() -> None:
        await _watch(
            title=args.title,
            seats_wanted=args.seats,
            interval=args.interval,
            contacts=contacts,
            artifact_dir=Path(args.artifact_dir),
            headful=(os.getenv("HEADFUL", "false").lower() != "false"),
            tg_token=tg_token,
            tg_chat_id=tg_chat_id,
            max_sessions=args.max_sessions,
        )

    try:
        asyncio.run(_go())
    except KeyboardInterrupt:
        console.print("\n[yellow]Мониторинг остановлен.[/yellow]")
    return 0


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    parser = build_parser()
    args = parser.parse_args(argv)
    _setup_logging(args.verbose)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
