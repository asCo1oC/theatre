"""Обработчики команд и callback кнопок для Telegram бота."""
from __future__ import annotations

import json
from typing import TYPE_CHECKING, Optional

from .errors import log_error, log_info
from .states import CommandType, ConversationContext, ConversationState
from .storage import SQLiteStorage

if TYPE_CHECKING:
    from .telegram_bot import TelegramBot

# Глобальное хранилище контекстов разговоров
_conversation_contexts: dict[tuple[int, int], ConversationContext] = {}


def get_conversation_context(user_id: int, chat_id: int) -> ConversationContext:
    """Получить контекст разговора."""
    key = (user_id, chat_id)
    if key not in _conversation_contexts:
        _conversation_contexts[key] = ConversationContext(
            user_id=user_id,
            chat_id=chat_id,
            state=ConversationState.START,
        )
    return _conversation_contexts[key]


def clear_conversation_context(user_id: int, chat_id: int) -> None:
    """Очистить контекст разговора."""
    key = (user_id, chat_id)
    if key in _conversation_contexts:
        del _conversation_contexts[key]


# ============ КОМАНДЫ ============


async def handle_watch_start(
    bot: TelegramBot,
    user_id: int,
    chat_id: int,
) -> None:
    """Обработчик /watch команды - начать выбор названия спектакля."""
    ctx = get_conversation_context(user_id, chat_id)
    ctx.command = CommandType.WATCH
    ctx.state = ConversationState.WATCH_TITLE
    
    storage = bot.storage
    existing = storage.get_watching_state(user_id)
    
    if existing and existing.status == "active":
        await bot.send_message(
            chat_id,
            "⚠️ <b>Мониторинг уже активен</b>\n\n"
            f"Спектакль: {existing.title}\n"
            f"Статус: {existing.status}\n\n"
            "Используйте /change для изменения параметров или /stop для остановки.",
            parse_mode="HTML"
        )
        return
    
    # Показываем список популярных спектаклей или форму ввода
    await bot.send_message(
        chat_id,
        "🎭 <b>Конфигурирование мониторинга</b>\n\n"
        "Введите название спектакля для поиска:\n\n"
        "<i>Примеры:</i>\n"
        "• Война и мир\n"
        "• Евгений Онегин\n"
        "• Щелкунчик",
        parse_mode="HTML",
        reply_markup=bot.get_main_menu_keyboard(),
    )
    
    log_info(f"Начало конфигурирования для пользователя {user_id}", {"chat_id": chat_id})


async def handle_watch_title_received(
    bot: TelegramBot,
    user_id: int,
    chat_id: int,
    title: str,
) -> None:
    """Обработчик ввода названия спектакля."""
    ctx = get_conversation_context(user_id, chat_id)
    
    if ctx.state != ConversationState.WATCH_TITLE:
        return
    
    ctx.title = title.strip()
    ctx.state = ConversationState.WATCH_DATE_RANGE
    
    await bot.send_message(
        chat_id,
        "📅 <b>Диапазон дат</b>\n\n"
        "Введите диапазон дат (опционально):\n\n"
        "<i>Примеры:</i>\n"
        "• 01.01 - 15.01\n"
        "• 2024-02-01 - 2024-02-28\n"
        "• все (или просто нажмите /skip)",
        parse_mode="HTML",
        reply_markup=bot.get_main_menu_keyboard(),
    )
    
    log_info(f"Спектакль выбран: {title}", {"user_id": user_id})


async def handle_skip_date_range(
    bot: TelegramBot,
    user_id: int,
    chat_id: int,
) -> None:
    """Пропустить выбор диапазона дат."""
    ctx = get_conversation_context(user_id, chat_id)
    
    if ctx.state != ConversationState.WATCH_DATE_RANGE:
        return
    
    ctx.date_range = "все даты"
    ctx.state = ConversationState.WATCH_SEAT_COUNT
    
    await bot.send_message(
        chat_id,
        "🎫 <b>Количество мест</b>\n\n"
        "Сколько билетов нужно забронировать?\n\n"
        "Введите число (1, 2, 3 и т.д.):",
        parse_mode="HTML",
        reply_markup=bot.get_main_menu_keyboard(),
    )


async def handle_watch_date_range_received(
    bot: TelegramBot,
    user_id: int,
    chat_id: int,
    date_range: str,
) -> None:
    """Обработчик ввода диапазона дат."""
    ctx = get_conversation_context(user_id, chat_id)
    
    if ctx.state != ConversationState.WATCH_DATE_RANGE:
        return
    
    ctx.date_range = date_range.strip()
    ctx.state = ConversationState.WATCH_SEAT_COUNT
    
    await bot.send_message(
        chat_id,
        "🎫 <b>Количество мест</b>\n\n"
        "Сколько билетов нужно забронировать?\n\n"
        "Введите число (1, 2, 3 и т.д.):",
        parse_mode="HTML",
        reply_markup=bot.get_main_menu_keyboard(),
    )


async def handle_watch_seat_count_received(
    bot: TelegramBot,
    user_id: int,
    chat_id: int,
    seat_count: str,
) -> None:
    """Обработчик ввода количества мест."""
    ctx = get_conversation_context(user_id, chat_id)
    
    if ctx.state != ConversationState.WATCH_SEAT_COUNT:
        return
    
    try:
        count = int(seat_count.strip())
        if count < 1 or count > 10:
            await bot.send_message(
                chat_id,
                "❌ Введите число от 1 до 10",
                parse_mode="HTML"
            )
            return
        
        ctx.seat_count = count
        ctx.state = ConversationState.WATCH_CONFIRM
        
        # Показываем подтверждение
        confirmation_text = (
            f"✅ <b>Подтвердите параметры</b>\n\n"
            f"🎭 Спектакль: <code>{ctx.title}</code>\n"
            f"📅 Даты: <code>{ctx.date_range}</code>\n"
            f"🎫 Мест: <code>{ctx.seat_count}</code>\n\n"
            "Параметры верны?"
        )
        
        # Кнопки подтверждения
        await bot.send_message(
            chat_id,
            confirmation_text,
            parse_mode="HTML",
            reply_markup={
                "inline_keyboard": [
                    [
                        {"text": "✅ Да, начать", "callback_data": "watch_confirm_yes"},
                        {"text": "❌ Нет, отменить", "callback_data": "watch_confirm_no"},
                    ]
                ]
            }
        )
        
        log_info(
            f"Подтверждение параметров: {ctx.title}, {count} мест",
            {"user_id": user_id}
        )
    
    except ValueError:
        await bot.send_message(
            chat_id,
            "❌ Введите корректное число",
            parse_mode="HTML"
        )


# ============ CALLBACK ОБРАБОТЧИКИ ============


async def handle_watch_confirm_yes(
    bot: TelegramBot,
    user_id: int,
    chat_id: int,
) -> None:
    """Подтверждение параметров мониторинга."""
    ctx = get_conversation_context(user_id, chat_id)
    
    if not all([ctx.title, ctx.date_range, ctx.seat_count]):
        await bot.send_message(
            chat_id,
            "❌ Ошибка: неполные параметры",
            parse_mode="HTML"
        )
        clear_conversation_context(user_id, chat_id)
        return
    
    # Сохраняем в базу
    bot.storage.set_watching_state(
        user_id=user_id,
        status="active",
        title=ctx.title,
        date_range=ctx.date_range,
        seats_count=ctx.seat_count,
    )
    
    ctx.state = ConversationState.DONE
    
    await bot.send_message(
        chat_id,
        "🚀 <b>Мониторинг активирован!</b>\n\n"
        "Я буду следить за появлением билетов и пришлю вам уведомление.\n\n"
        "<b>Доступные команды:</b>\n"
        "/pause — приостановить\n"
        "/resume — возобновить\n"
        "/change — изменить параметры\n"
        "/stop — полностью остановить\n"
        "/status — статус мониторинга\n"
        "/help — полная справка",
        parse_mode="HTML",
        reply_markup=bot.get_main_menu_keyboard(),
    )
    
    log_info(
        f"Мониторинг активирован: {ctx.title}",
        {"user_id": user_id, "seats": ctx.seat_count}
    )
    
    clear_conversation_context(user_id, chat_id)


async def handle_watch_confirm_no(
    bot: TelegramBot,
    user_id: int,
    chat_id: int,
) -> None:
    """Отмена конфигурирования."""
    clear_conversation_context(user_id, chat_id)
    
    await bot.send_message(
        chat_id,
        "❌ Конфигурирование отменено.",
        parse_mode="HTML",
        reply_markup=bot.get_main_menu_keyboard(),
    )


async def handle_pause(
    bot: TelegramBot,
    user_id: int,
    chat_id: int,
) -> None:
    """Обработчик /pause команды."""
    storage = bot.storage
    state = storage.get_watching_state(user_id)
    
    if not state or state.status == "sleeping":
        await bot.send_message(
            chat_id,
            "⚠️ Мониторинг не активен",
            parse_mode="HTML"
        )
        return
    
    if state.status == "paused":
        await bot.send_message(
            chat_id,
            "⏸️ Мониторинг уже приостановлен",
            parse_mode="HTML"
        )
        return
    
    storage.set_watching_state(
        user_id=user_id,
        status="paused",
        title=state.title,
        date_range=state.date_range,
        seats_count=state.seats_count,
    )
    
    await bot.send_message(
        chat_id,
        "⏸️ <b>Мониторинг приостановлен</b>\n\n"
        "Используйте /resume для возобновления.",
        parse_mode="HTML",
        reply_markup=bot.get_main_menu_keyboard(),
    )
    
    log_info("Мониторинг приостановлен", {"user_id": user_id})


async def handle_resume(
    bot: TelegramBot,
    user_id: int,
    chat_id: int,
) -> None:
    """Обработчик /resume команды."""
    storage = bot.storage
    state = storage.get_watching_state(user_id)
    
    if not state or state.status == "sleeping":
        await bot.send_message(
            chat_id,
            "⚠️ Мониторинг не был приостановлен",
            parse_mode="HTML"
        )
        return
    
    if state.status == "active":
        await bot.send_message(
            chat_id,
            "✅ Мониторинг уже активен",
            parse_mode="HTML"
        )
        return
    
    storage.set_watching_state(
        user_id=user_id,
        status="active",
        title=state.title,
        date_range=state.date_range,
        seats_count=state.seats_count,
    )
    
    await bot.send_message(
        chat_id,
        "▶️ <b>Мониторинг возобновлен</b>\n\n"
        "Я продолжу следить за билетами.",
        parse_mode="HTML",
        reply_markup=bot.get_main_menu_keyboard(),
    )
    
    log_info("Мониторинг возобновлен", {"user_id": user_id})


async def handle_stop(
    bot: TelegramBot,
    user_id: int,
    chat_id: int,
) -> None:
    """Обработчик /stop команды."""
    storage = bot.storage
    state = storage.get_watching_state(user_id)
    
    if not state or state.status == "sleeping":
        await bot.send_message(
            chat_id,
            "⚠️ Мониторинг не активен",
            parse_mode="HTML"
        )
        return
    
    storage.set_watching_state(
        user_id=user_id,
        status="sleeping",
        title=state.title,
        date_range=state.date_range,
        seats_count=state.seats_count,
    )
    
    await bot.send_message(
        chat_id,
        "😴 <b>Мониторинг остановлен</b>\n\n"
        "Введите /watch для нового мониторинга.",
        parse_mode="HTML",
        reply_markup=bot.get_main_menu_keyboard(),
    )
    
    log_info("Мониторинг остановлен", {"user_id": user_id})


async def handle_change(
    bot: TelegramBot,
    user_id: int,
    chat_id: int,
) -> None:
    """Обработчик /change команды - перезапустить форму."""
    await handle_watch_start(bot, user_id, chat_id)


async def handle_sleep(
    bot: TelegramBot,
    user_id: int,
    chat_id: int,
) -> None:
    """Обработчик /sleep команды."""
    storage = bot.storage
    state = storage.get_watching_state(user_id)
    
    if not state:
        await bot.send_message(
            chat_id,
            "⚠️ Нечего переводить в режим сна",
            parse_mode="HTML"
        )
        return
    
    storage.set_watching_state(
        user_id=user_id,
        status="sleeping",
        title=state.title,
        date_range=state.date_range,
        seats_count=state.seats_count,
    )
    
    await bot.send_message(
        chat_id,
        "💤 <b>Режим сна активирован</b>\n\n"
        "Мониторинг полностью остановлен.",
        parse_mode="HTML"
    )
    
    log_info("Режим сна активирован", {"user_id": user_id})


# ============ CALLBACK ОБРАБОТЧИКИ ДЛЯ БРОНИРОВАНИЯ ============


async def handle_booking_confirm_yes(
    bot: TelegramBot,
    user_id: int,
    chat_id: int,
) -> None:
    """Подтверждение успешного бронирования."""
    storage = bot.storage
    booking = storage.get_last_booking(user_id)
    
    if booking:
        storage.confirm_booking(user_id, confirmed=True)
    
    await bot.send_message(
        chat_id,
        "✅ <b>Спасибо за подтверждение!</b>\n\n"
        "Бронирование завершено. Не забудьте оплатить в течение 15 минут.",
        parse_mode="HTML"
    )
    
    log_info("Бронирование подтверждено", {"user_id": user_id})


async def handle_booking_confirm_no(
    bot: TelegramBot,
    user_id: int,
    chat_id: int,
) -> None:
    """Ошибка при бронировании - выбор типа проблемы."""
    ctx = get_conversation_context(user_id, chat_id)
    ctx.state = ConversationState.BOOKING_PROBLEM
    
    storage = bot.storage
    booking = storage.get_last_booking(user_id)
    
    if booking:
        storage.confirm_booking(user_id, confirmed=False)
    
    await bot.send_message(
        chat_id,
        "❌ <b>Что случилось?</b>\n\n"
        "Помогите определить причину проблемы:",
        parse_mode="HTML",
        reply_markup={
            "inline_keyboard": [
                [
                    {"text": "🤖 Ошибка бота", "callback_data": "problem_bot_error"},
                ],
                [
                    {"text": "⏱️ Сеанс упущен", "callback_data": "problem_user_timeout"},
                ],
            ]
        }
    )
    
    log_info("Начало диагностики проблемы", {"user_id": user_id})


async def handle_problem_bot_error(
    bot: TelegramBot,
    user_id: int,
    chat_id: int,
) -> None:
    """Ошибка бота - отправляем в поддержку."""
    await bot.send_message(
        chat_id,
        "😞 <b>Спасибо за отчет об ошибке</b>\n\n"
        "Пожалуйста, свяжитесь с разработчиком:\n\n"
        "@askollok\n\n"
        "Укажите время ошибки и название спектакля.",
        parse_mode="HTML"
    )
    
    log_info("Отчет об ошибке бота", {"user_id": user_id})
    clear_conversation_context(user_id, chat_id)


async def handle_problem_user_timeout(
    bot: TelegramBot,
    user_id: int,
    chat_id: int,
) -> None:
    """Сеанс упущен - пробуем заново."""
    storage = bot.storage
    state = storage.get_watching_state(user_id)
    
    if state:
        await bot.send_message(
            chat_id,
            "🔄 <b>Попробуем заново</b>\n\n"
            "Мониторинг продолжается...",
            parse_mode="HTML"
        )
    
    log_info("Повторная попытка после таймаута", {"user_id": user_id})
    clear_conversation_context(user_id, chat_id)
