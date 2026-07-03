import asyncio
import logging
from datetime import datetime, timedelta
import aiohttp
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.fsm.state import StatesGroup, State
from aiogram.fsm.context import FSMContext
from aiogram.types import BufferedInputFile
from aiogram.utils.keyboard import ReplyKeyboardBuilder, InlineKeyboardBuilder

from config import TELEGRAM_TOKEN
import maps
from database import (
    init_db, add_user_if_not_exists, add_trip,
    get_active_trips, get_past_trips, delete_trip,
    get_all_active_trips_with_buffer, update_trip_status, update_user_buffer,
    get_trip_by_id, update_trip_field,
    update_user_location, get_user_location,
)

logging.basicConfig(level=logging.INFO)

bot = Bot(token=TELEGRAM_TOKEN)
dp = Dispatcher()


# Состояния для формы добавления поездки
class TripForm(StatesGroup):
    waiting_for_destination = State()
    waiting_for_arrival_time = State()
    waiting_for_transport = State()
    waiting_for_reminder = State()


# Состояние для формы изменения настроек
class SettingsForm(StatesGroup):
    waiting_for_buffer = State()


# Состояния для редактирования поездки
class EditTripForm(StatesGroup):
    waiting_for_field_choice = State()
    waiting_for_new_value = State()
    waiting_for_confirmation = State()


def get_main_menu_keyboard():
    builder = ReplyKeyboardBuilder()
    builder.button(text="➕ Добавить поездку")
    builder.button(text="📅 Мои поездки")
    builder.button(text="🗂 Архив поездок")
    builder.button(text="📍 Геолокация")
    builder.button(text="⚙️ Настройки")
    builder.adjust(2, 2, 1)
    return builder.as_markup(resize_keyboard=True)


def get_cancel_keyboard():
    builder = ReplyKeyboardBuilder()
    builder.button(text="🔙 Назад")
    return builder.as_markup(resize_keyboard=True)


def get_location_request_keyboard():
    builder = ReplyKeyboardBuilder()
    builder.button(text="📍 Отправить геолокацию", request_location=True)
    builder.button(text="🔙 Назад")
    builder.adjust(1)
    return builder.as_markup(resize_keyboard=True)


def get_destination_input_keyboard():
    """Клавиатура для шага ввода места назначения.

    Кнопки request_location в Telegram всегда отправляют реальную GPS-геопозицию
    пользователя, а не дают выбрать произвольную точку — поэтому здесь такой
    кнопки нет. Выбрать точку на карте можно через встроенное меню вложений
    Telegram (см. подсказку в тексте сообщения), а обычная геолокация (если
    пользователь действительно стоит в нужном месте) тоже подойдёт и сработает
    точно так же.
    """
    builder = ReplyKeyboardBuilder()
    builder.button(text="🔙 Назад")
    builder.adjust(1)
    return builder.as_markup(resize_keyboard=True)


async def send_route_map(user_id: int, destination: str, dest_lat, dest_lon, transport_type: str):
    """Отправляет пользователю картинку карты с маршрутом и кнопку для открытия
    построенного маршрута в 2ГИС. Если координат назначения нет — ничего не делает."""
    if dest_lat is None or dest_lon is None:
        return

    user_lat, user_lon = get_user_location(user_id)
    map_url = maps.build_static_map_url(dest_lat, dest_lon, user_lat, user_lon)
    route_url = maps.build_route_link(dest_lat, dest_lon, user_lat, user_lon, transport_type)

    caption = f"🗺 **Маршрут до:** {destination}\n🚦 **Транспорт:** {transport_type}"
    if user_lat is None:
        caption += "\n\n💡 Поделись геолокацией — тогда я покажу маршрут целиком, а не только точку."

    kb = InlineKeyboardBuilder()
    kb.button(text="🧭 Открыть маршрут в 2ГИС", url=route_url)

    # Сами скачиваем картинку карты вместо того, чтобы отдавать голый URL в Telegram —
    # так мы видим реальный ответ 2ГИС (в т.ч. текст ошибки), если что-то пошло не так,
    # а не общее "failed to get HTTP URL content" от серверов Telegram.
    photo_bytes = None
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as session:
            async with session.get(map_url) as resp:
                if resp.status == 200 and resp.content_type.startswith("image/"):
                    photo_bytes = await resp.read()
                else:
                    body = await resp.text()
                    logging.error(f"2ГИС Static Map вернул статус {resp.status}: {body}")
    except Exception as e:
        logging.error(f"Ошибка скачивания карты у 2ГИС: {e}")

    try:
        if photo_bytes:
            await bot.send_photo(
                chat_id=user_id,
                photo=BufferedInputFile(photo_bytes, filename="map.png"),
                caption=caption,
                parse_mode="Markdown",
                reply_markup=kb.as_markup(),
            )
        else:
            raise RuntimeError("нет байтов карты (см. лог выше)")
    except Exception as e:
        # Например, Static API временно недоступен или не включён на ключе —
        # не роняем сценарий, а хотя бы присылаем кнопку с маршрутом отдельно
        logging.error(f"Не удалось отправить карту пользователю {user_id}: {e}")
        try:
            await bot.send_message(
                chat_id=user_id, text=caption, parse_mode="Markdown", reply_markup=kb.as_markup()
            )
        except Exception as e2:
            logging.error(f"Не удалось отправить даже кнопку маршрута пользователю {user_id}: {e2}")


@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    add_user_if_not_exists(message.from_user.id)
    welcome_text = (
        "🧭 **Привет! Я твой бот-штурман.**\n\n"
        "Я помогу тебе рассчитать точное время выезда с учетом пробок и реального маршрута (2ГИС).\n"
        "Используй меню кнопок внизу, чтобы управлять поездками!\n\n"
        "📍 Не забудь поделиться геолокацией (кнопка «Геолокация» в меню) — "
        "без неё я буду считать время в пути только приблизительно."
    )
    await message.answer(welcome_text, parse_mode="Markdown", reply_markup=get_main_menu_keyboard())


# --- ГЕОЛОКАЦИЯ ---
@dp.message(Command("location"))
@dp.message(F.text == "📍 Геолокация")
async def cmd_location(message: types.Message):
    await message.answer(
        "Отправь свою геолокацию.\n\n"
        "Лучше всего — **Live-геолокацию** (в Telegram: 📎 → Геопозиция → "
        "«Транслировать геопозицию»). Тогда я буду видеть, где ты находишься, "
        "и точнее пересчитывать время в пути, пока трансляция активна.\n\n"
        "Обычная (разовая) геолокация тоже подойдёт, но перестанет быть актуальной, "
        "как только ты начнёшь двигаться.",
        parse_mode="Markdown",
        reply_markup=get_location_request_keyboard()
    )


async def is_not_destination_pick(message: types.Message, state: FSMContext) -> bool:
    """Фильтр для общего обработчика геолокации: пропускает сообщение дальше
    (возвращает False, т.е. "это не общая геолокация"), если пользователь сейчас
    находится на шаге выбора места назначения (создание или редактирование
    поездки) — там точку на карте нужно превратить в адрес, а не запомнить как
    геолокацию пользователя."""
    current_state = await state.get_state()
    if current_state == TripForm.waiting_for_destination.state:
        return False
    if current_state == EditTripForm.waiting_for_new_value.state:
        data = await state.get_data()
        if data.get("chosen_field") == "destination":
            return False
    return True


@dp.message(F.location, is_not_destination_pick)
async def handle_location(message: types.Message):
    loc = message.location
    update_user_location(message.from_user.id, loc.latitude, loc.longitude)
    if loc.live_period:
        text = (
            "📍 **Live-геолокация подключена!**\n"
            "Пока ты её транслируешь, я буду видеть твоё перемещение и точнее "
            "считать время в пути до места назначения."
        )
    else:
        text = (
            "📍 Геолокация сохранена. Если ты потом сменишь место, не забудь "
            "отправить её заново (или включи Live-геолокацию, чтобы не думать об этом)."
        )
    await message.answer(text, parse_mode="Markdown", reply_markup=get_main_menu_keyboard())


@dp.edited_message(F.location)
async def handle_location_live_update(message: types.Message):
    # Telegram присылает обновления live-геолокации как edited_message —
    # молча обновляем координаты, не отвечая на каждое обновление
    loc = message.location
    update_user_location(message.from_user.id, loc.latitude, loc.longitude)


# --- НАСТРОЙКИ ЧЕРЕЗ FSM ---
@dp.message(Command("settings"))
@dp.message(F.text == "⚙️ Настройки")
async def cmd_settings(message: types.Message, state: FSMContext):
    await message.answer(
        "⚙️ **Настройки запаса времени**\n\n"
        "Введи число процентов (от 0 до 100), которое ты хочешь заложить на случай пробок.\n"
        "По умолчанию используется: **10%**.",
        reply_markup=get_cancel_keyboard()
    )
    await state.set_state(SettingsForm.waiting_for_buffer)


@dp.message(SettingsForm.waiting_for_buffer)
async def process_setting_buffer(message: types.Message, state: FSMContext):
    if message.text == "🔙 Назад":
        await message.answer("Ввод отменен. Возвращаемся в главное меню.", reply_markup=get_main_menu_keyboard())
        await state.clear()
        return
    try:
        new_buffer = int(message.text.strip())
        if new_buffer < 0 or new_buffer > 100:
            await message.answer("❌ Процент запаса должен быть строго в диапазоне от 0 до 100! Попробуй еще раз:")
            return
        update_user_buffer(message.from_user.id, new_buffer)
        await message.answer(f"✅ Настройки успешно сохранены! Твой личный запас времени: **{new_buffer}%**",
                             parse_mode="Markdown", reply_markup=get_main_menu_keyboard())
        await state.clear()
    except ValueError:
        await message.answer("❌ Ошибка! Введи целое число процентов цифрами или нажми «🔙 Назад»:")


# --- СЦЕНАРИЙ ДОБАВЛЕНИЯ ПОЕЗДКИ С ВАЛИДАЦИЕЙ (FSM) ---
@dp.message(Command("add_trip"))
@dp.message(F.text == "➕ Добавить поездку")
async def start_trip_form(message: types.Message, state: FSMContext):
    await message.answer(
        "📍 Введи место назначения (адрес или название места).\n\n"
        "Либо пришли геопозицию: нажми на скрепку 📎 → «Геопозиция», сдвинь маркер "
        "на нужное место на карте и отправь его кнопкой «Отправить эту геопозицию» "
        "— адрес я определю сам. (Кнопка «Отправить мою геопозицию» в том же меню "
        "отправит именно твоё текущее местоположение, а не выбранную точку.)",
        reply_markup=get_destination_input_keyboard()
    )
    await state.set_state(TripForm.waiting_for_destination)


@dp.message(TripForm.waiting_for_destination, F.text == "🔙 Назад")
async def cancel_trip_form(message: types.Message, state: FSMContext):
    await message.answer("Добавление поездки отменено.", reply_markup=get_main_menu_keyboard())
    await state.clear()


@dp.message(TripForm.waiting_for_destination, F.location)
async def process_destination_by_location(message: types.Message, state: FSMContext):
    loc = message.location
    searching_msg = await message.answer("🔎 Определяю адрес по точке на карте...")
    address = await maps.reverse_geocode(loc.latitude, loc.longitude)
    await searching_msg.delete()

    destination = address or f"Точка на карте ({loc.latitude:.5f}, {loc.longitude:.5f})"
    await state.update_data(destination=destination, dest_lat=loc.latitude, dest_lon=loc.longitude)
    await message.answer(
        f"📍 Место определено: **{destination}**\n\n"
        "⏱ Когда тебе нужно быть там? Введи дату и время в формате "
        "ДД.ММ.ГГГГ ЧЧ:ММ (например, 25.12.2026 18:30):",
        parse_mode="Markdown",
        reply_markup=types.ReplyKeyboardRemove()
    )
    await state.set_state(TripForm.waiting_for_arrival_time)


@dp.message(TripForm.waiting_for_destination)
async def process_destination(message: types.Message, state: FSMContext):
    if not message.text:
        await message.answer("❌ Введи адрес текстом, либо пришли точку на карте через скрепку 📎 → «Геопозиция»:")
        return

    destination = message.text.strip()
    if len(destination) < 2:
        await message.answer("❌ Название слишком короткое (минимум 2 символа):")
        return
    if len(destination) > 100:
        await message.answer("❌ Название слишком длинное (максимум 100 символов):")
        return

    searching_msg = await message.answer("🔎 Ищу это место на карте 2ГИС...")
    coords = await maps.geocode_address(destination)
    await searching_msg.delete()

    if coords is None:
        await message.answer(
            "❌ Не нашёл такое место на карте 2ГИС. Попробуй указать адрес точнее "
            "(например, добавь название города), либо пришли точку на карте через скрепку 📎 → «Геопозиция»:"
        )
        return

    dest_lat, dest_lon = coords
    await state.update_data(destination=destination, dest_lat=dest_lat, dest_lon=dest_lon)
    await message.answer(
        "⏱ Когда тебе нужно быть там? Введи дату и время в формате ДД.ММ.ГГГГ ЧЧ:ММ (например, 25.12.2026 18:30):",
        reply_markup=types.ReplyKeyboardRemove()
    )
    await state.set_state(TripForm.waiting_for_arrival_time)


@dp.message(TripForm.waiting_for_arrival_time)
async def process_arrival_time(message: types.Message, state: FSMContext):
    time_input = message.text.strip()
    try:
        parsed = datetime.strptime(time_input, "%d.%m.%Y %H:%M")
    except ValueError:
        await message.answer(
            "❌ Неверный формат! Введи строго ДД.ММ.ГГГГ ЧЧ:ММ (например: 25.12.2026 08:00):"
        )
        return

    if parsed <= datetime.now():
        await message.answer(
            "❌ Это время уже прошло. Укажи дату и время в будущем (ДД.ММ.ГГГГ ЧЧ:ММ):"
        )
        return

    await state.update_data(arrival_time=time_input)

    builder = ReplyKeyboardBuilder()
    builder.button(text="🚗 На авто")
    builder.button(text="🚌 Общественный транспорт")
    builder.button(text="🚶 Пешком")
    builder.adjust(1)
    await message.answer("Выбери способ передвижения:",
                         reply_markup=builder.as_markup(resize_keyboard=True, one_time_keyboard=True))
    await state.set_state(TripForm.waiting_for_transport)


@dp.message(TripForm.waiting_for_transport)
async def process_transport(message: types.Message, state: FSMContext):
    transport = message.text.strip()
    allowed_transports = ["🚗 На авто", "🚌 Общественный транспорт", "🚶 Пешком"]
    if transport not in allowed_transports:
        await message.answer("❌ Пожалуйста, нажми на одну из кнопок на клавиатуре 👇:")
        return
    await state.update_data(transport_type=transport)
    await message.answer("🔔 За сколько минут до выхода тебя предупредить? (например: 15):",
                         reply_markup=types.ReplyKeyboardRemove())
    await state.set_state(TripForm.waiting_for_reminder)


@dp.message(TripForm.waiting_for_reminder)
async def process_reminder(message: types.Message, state: FSMContext):
    try:
        reminder_minutes = int(message.text.strip())
        if reminder_minutes < 0 or reminder_minutes > 180:
            await message.answer("❌ Введи число минут от 0 до 180:")
            return
    except ValueError:
        await message.answer("❌ Нужно ввести только целое число минут цифрами:")
        return

    user_data = await state.get_data()
    add_trip(
        user_id=message.from_user.id,
        destination=user_data['destination'],
        arrival_time=user_data['arrival_time'],
        transport_type=user_data['transport_type'],
        reminder_minutes=reminder_minutes,
        dest_lat=user_data.get('dest_lat'),
        dest_lon=user_data.get('dest_lon'),
    )

    summary = (
        "🎉 **Поездка успешно запланирована!**\n\n"
        f"📍 Место: {user_data['destination']}\n"
        f"⏱ Время прибытия: {user_data['arrival_time']}\n"
        f"🚗 Транспорт: {user_data['transport_type']}\n"
        f"🔔 Напоминание: за {reminder_minutes} минут"
    )
    await message.answer(summary, reply_markup=get_main_menu_keyboard(), parse_mode="Markdown")
    await state.clear()

    # Показываем карту с точкой (и маршрутом, если уже знаем геолокацию) + кнопку в 2ГИС
    await send_route_map(
        message.from_user.id, user_data['destination'],
        user_data.get('dest_lat'), user_data.get('dest_lon'),
        user_data['transport_type']
    )

    # Если у нас ещё нет геолокации пользователя — попросим её,
    # иначе расчёт маршрута будет только приблизительным
    user_lat, _ = get_user_location(message.from_user.id)
    if user_lat is None:
        await message.answer(
            "Чтобы я мог правильно посчитать время выезда с учётом реального маршрута, "
            "поделись, пожалуйста, своей геолокацией (лучше — Live).",
            reply_markup=get_location_request_keyboard()
        )


# --- СЦЕНАРИЙ ПРОСМОТРА, УДАЛЕНИЯ И РЕДАКТИРОВАНИЯ ПОЕЗДОК ---
@dp.message(Command("my_trips"))
@dp.message(F.text == "📅 Мои поездки")
async def list_trips(message: types.Message):
    trips = get_active_trips(message.from_user.id)
    if not trips:
        await message.answer("📅 У тебя пока нет запланированных поездок.", reply_markup=get_main_menu_keyboard())
        return

    for trip in trips:
        trip_id, dest, arr_time, transport, remind, dest_lat, dest_lon = trip
        text = (
            f"📍 **Место:** {dest}\n"
            f"⏱ **Прибытие:** {arr_time}\n"
            f"🚗 **Транспорт:** {transport}\n"
            f"🔔 **Напомнить за:** {remind} мин."
        )
        inline_builder = InlineKeyboardBuilder()
        row = [types.InlineKeyboardButton(text="✏️ Редактировать", callback_data=f"edit_{trip_id}")]
        if dest_lat is not None and dest_lon is not None:
            row.append(types.InlineKeyboardButton(text="🗺 Маршрут", callback_data=f"map_{trip_id}"))
        inline_builder.row(*row)
        inline_builder.row(
            types.InlineKeyboardButton(text="❌ Удалить", callback_data=f"del_{trip_id}")
        )
        await message.answer(text, reply_markup=inline_builder.as_markup(), parse_mode="Markdown")


@dp.callback_query(lambda c: c.data.startswith("del_"))
async def process_delete_trip(callback_query: types.CallbackQuery):
    trip_id = int(callback_query.data.split("_")[1])
    delete_trip(trip_id)
    await callback_query.answer("Поездка удалена!")
    await callback_query.message.edit_text("🗑 *Эта поездка была удалена.*", parse_mode="Markdown")


@dp.callback_query(lambda c: c.data.startswith("map_"))
async def process_show_trip_map(callback_query: types.CallbackQuery):
    trip_id = int(callback_query.data.split("_")[1])
    trip = get_trip_by_id(trip_id)
    if not trip:
        await callback_query.answer("Поездка не найдена.", show_alert=True)
        return

    _, destination, _arrival_time, transport_type, _remind, dest_lat, dest_lon = trip
    if dest_lat is None or dest_lon is None:
        await callback_query.answer("Для этой поездки нет координат места назначения.", show_alert=True)
        return

    await callback_query.answer()
    await send_route_map(callback_query.from_user.id, destination, dest_lat, dest_lon, transport_type)


# --- АРХИВ ПРОШЕДШИХ ПОЕЗДОК (ТОЛЬКО ПРОСМОТР) ---
@dp.message(Command("history"))
@dp.message(F.text == "🗂 Архив поездок")
async def list_past_trips(message: types.Message):
    trips = get_past_trips(message.from_user.id)
    if not trips:
        await message.answer("🗂 Архив пуст — прошедших поездок пока нет.", reply_markup=get_main_menu_keyboard())
        return

    await message.answer("🗂 **Прошедшие поездки** (только просмотр):", parse_mode="Markdown")
    for trip in trips:
        trip_id, dest, arr_time, transport, remind, status = trip
        text = (
            f"📍 **Место:** {dest}\n"
            f"⏱ **Прибытие было в:** {arr_time}\n"
            f"🚗 **Транспорт:** {transport}\n"
            f"🔔 **Напоминание было за:** {remind} мин.\n"
            f"✅ *Поездка завершена*"
        )
        await message.answer(text, parse_mode="Markdown")
    await message.answer("Это все прошедшие поездки.", reply_markup=get_main_menu_keyboard())


# --- ВСПОРМОГАТЕЛЬНАЯ ФУНКЦИЯ ДЛЯ ОТРИСОВКИ МЕНЮ ВЫБОРА ПОЛЕЙ ---
async def show_field_selection_menu(message: types.Message):
    inline_builder = InlineKeyboardBuilder()
    inline_builder.row(
        types.InlineKeyboardButton(text="📍 Место", callback_data="field_destination"),
        types.InlineKeyboardButton(text="⏱ Время", callback_data="field_arrival_time")
    )
    inline_builder.row(
        types.InlineKeyboardButton(text="🚗 Транспорт", callback_data="field_transport_type"),
        types.InlineKeyboardButton(text="🔔 Напоминание", callback_data="field_reminder_minutes")
    )
    inline_builder.row(
        types.InlineKeyboardButton(text="❌ Отменить редактирование", callback_data="edit_cancel")
    )
    await message.answer(
        "🛠 **Что ты хочешь изменить в этой поездке?**",
        reply_markup=inline_builder.as_markup(),
        parse_mode="Markdown"
    )


# 1. Нажатие кнопки "Редактировать" под поездкой
@dp.callback_query(lambda c: c.data.startswith("edit_"))
async def process_edit_trip_start(callback_query: types.CallbackQuery, state: FSMContext):
    trip_id = int(callback_query.data.split("_")[1])
    trip = get_trip_by_id(trip_id)

    if not trip:
        await callback_query.answer("Ошибка! Поездка не найдена.")
        return

    await state.update_data(
        trip_id=trip[0],
        destination=trip[1],
        arrival_time=trip[2],
        transport_type=trip[3],
        reminder_minutes=trip[4],
        dest_lat=trip[5],
        dest_lon=trip[6],
    )

    await show_field_selection_menu(callback_query.message)
    await state.set_state(EditTripForm.waiting_for_field_choice)
    await callback_query.answer()


# Отмена редактирования
@dp.callback_query(EditTripForm.waiting_for_field_choice, F.data == "edit_cancel")
async def process_edit_cancel(callback_query: types.CallbackQuery, state: FSMContext):
    await callback_query.message.edit_text("🚫 Редактирование отменено.")
    await state.clear()
    await callback_query.answer()


# 2. Выбор поля для редактирования
@dp.callback_query(EditTripForm.waiting_for_field_choice, F.data.startswith("field_"))
async def process_field_choice(callback_query: types.CallbackQuery, state: FSMContext):
    chosen_field = callback_query.data.replace("field_", "")
    await state.update_data(chosen_field=chosen_field)

    field_prompts = {
        "destination": (
            "📍 Введи новое место назначения текстом.\n\n"
            "Либо пришли геопозицию: нажми на скрепку 📎 → «Геопозиция», сдвинь маркер "
            "на нужное место и отправь его кнопкой «Отправить эту геопозицию» — адрес "
            "определю сам."
        ),
        "arrival_time": "⏱ Введи новую дату и время прибытия в формате ДД.ММ.ГГГГ ЧЧ:ММ (например, 25.12.2026 15:45):",
        "transport_type": "Выбери новый способ передвижения на кнопках ниже:",
        "reminder_minutes": "🔔 За сколько минут до выхода тебя предупредить? Введи число минут цифрами:"
    }

    if chosen_field == "transport_type":
        builder = ReplyKeyboardBuilder()
        builder.button(text="🚗 На авто")
        builder.button(text="🚌 Общественный транспорт")
        builder.button(text="🚶 Пешком")
        builder.adjust(1)
        await callback_query.message.reply(field_prompts[chosen_field],
                                           reply_markup=builder.as_markup(resize_keyboard=True, one_time_keyboard=True))
    else:
        await callback_query.message.reply(field_prompts[chosen_field], reply_markup=types.ReplyKeyboardRemove())

    await state.set_state(EditTripForm.waiting_for_new_value)
    await callback_query.answer()


# 3. Прием нового значения, валидация и генерация превью
@dp.message(EditTripForm.waiting_for_new_value)
async def process_new_value(message: types.Message, state: FSMContext):
    user_data = await state.get_data()
    field = user_data['chosen_field']

    if field == "destination" and message.location:
        loc = message.location
        searching_msg = await message.answer("🔎 Определяю адрес по точке на карте...")
        address = await maps.reverse_geocode(loc.latitude, loc.longitude)
        await searching_msg.delete()
        destination = address or f"Точка на карте ({loc.latitude:.5f}, {loc.longitude:.5f})"
        await state.update_data(destination=destination, dest_lat=loc.latitude, dest_lon=loc.longitude)
        text_input = None  # значение уже установлено, дальше просто идём к формированию превью
    elif not message.text:
        await message.answer("❌ Введи новое значение текстом (или, если меняешь место, пришли точку на карте через скрепку 📎 → «Геопозиция»):")
        return
    else:
        text_input = message.text.strip()

    if field == "destination" and text_input is not None:
        if len(text_input) < 2 or len(text_input) > 100:
            await message.answer("❌ Название должно быть от 2 до 100 символов. Введи корректно:")
            return

        searching_msg = await message.answer("🔎 Ищу это место на карте 2ГИС...")
        coords = await maps.geocode_address(text_input)
        await searching_msg.delete()

        if coords is None:
            await message.answer("❌ Не нашёл такое место на карте 2ГИС. Уточни адрес (например, добавь город):")
            return

        dest_lat, dest_lon = coords
        await state.update_data(destination=text_input, dest_lat=dest_lat, dest_lon=dest_lon)

    elif field == "arrival_time":
        try:
            parsed = datetime.strptime(text_input, "%d.%m.%Y %H:%M")
        except ValueError:
            await message.answer(
                "❌ Неверный формат! Используй строго ДД.ММ.ГГГГ ЧЧ:ММ (например, 25.12.2026 19:00):"
            )
            return
        if parsed <= datetime.now():
            await message.answer(
                "❌ Это время уже прошло. Укажи дату и время в будущем (ДД.ММ.ГГГГ ЧЧ:ММ):"
            )
            return
        await state.update_data(arrival_time=text_input)

    elif field == "transport_type":
        allowed = ["🚗 На авто", "🚌 Общественный транспорт", "🚶 Пешком"]
        if text_input not in allowed:
            await message.answer("❌ Выбери транспорт кнопкой на клавиатуре 👇:")
            return
        await state.update_data(transport_type=text_input)

    elif field == "reminder_minutes":
        try:
            val = int(text_input)
            if val < 0 or val > 180:
                await message.answer("❌ Число минут должно быть в диапазоне от 0 до 180. Попробуй еще раз:")
                return
            await state.update_data(reminder_minutes=val)
        except ValueError:
            await message.answer("❌ Введи корректное целое число минут:")
            return

    # ОБНОВЛЕНО: Получаем самые свежие данные для превью
    updated_data = await state.get_data()
    preview_text = (
        "📝 **Предварительный просмотр измененной поездки:**\n\n"
        f"📍 Место: {updated_data['destination']}\n"
        f"⏱ Время прибытия: {updated_data['arrival_time']}\n"
        f"🚗 Транспорт: {updated_data['transport_type']}\n"
        f"🔔 Напоминание: за {updated_data['reminder_minutes']} минут\n\n"
        "Все верно? Выбери действие ниже 👇"
    )

    # ОБНОВЛЕНО: Две кнопки в один ряд (Сохранить и Продолжить редактирование)
    inline_confirm = InlineKeyboardBuilder()
    inline_confirm.row(
        types.InlineKeyboardButton(text="💾 Сохранить", callback_data="save_edit"),
        types.InlineKeyboardButton(text="🔄 Изменить что-то еще", callback_data="continue_edit")
    )

    await message.answer(preview_text, reply_markup=inline_confirm.as_markup(), parse_mode="Markdown")
    await state.set_state(EditTripForm.waiting_for_confirmation)


# НОВОЕ: Обработчик кнопки "Изменить что-то еще"
@dp.callback_query(EditTripForm.waiting_for_confirmation, F.data == "continue_edit")
async def process_continue_editing(callback_query: types.CallbackQuery, state: FSMContext):
    # Убираем старый текст превью
    await callback_query.message.delete()
    # Заново выводим меню выбора полей
    await show_field_selection_menu(callback_query.message)
    # Возвращаем состояние ожидания выбора поля
    await state.set_state(EditTripForm.waiting_for_field_choice)
    await callback_query.answer()


# 4. Финальное сохранение изменений в БД
@dp.callback_query(EditTripForm.waiting_for_confirmation, F.data == "save_edit")
async def process_save_edit(callback_query: types.CallbackQuery, state: FSMContext):
    user_data = await state.get_data()
    trip_id = user_data['trip_id']

    # Последовательно перезаписываем все поля в БД на случай множественного изменения
    update_trip_field(trip_id, "destination", user_data['destination'])
    update_trip_field(trip_id, "arrival_time", user_data['arrival_time'])
    update_trip_field(trip_id, "transport_type", user_data['transport_type'])
    update_trip_field(trip_id, "reminder_minutes", user_data['reminder_minutes'])
    if user_data.get('dest_lat') is not None and user_data.get('dest_lon') is not None:
        update_trip_field(trip_id, "dest_lat", user_data['dest_lat'])
        update_trip_field(trip_id, "dest_lon", user_data['dest_lon'])

    await callback_query.message.edit_text("✅ **Поездка сохранена!** Изменения успешно применены.",
                                           parse_mode="Markdown")
    await callback_query.message.answer("Возврат в меню:", reply_markup=get_main_menu_keyboard())
    await state.clear()
    await callback_query.answer()


# --- ОБЩИЙ ФОЛБЭК "НАЗАД" ВНЕ FSM (например, из экрана геолокации) ---
@dp.message(F.text == "🔙 Назад")
async def fallback_back_to_menu(message: types.Message):
    await message.answer("Возвращаемся в главное меню.", reply_markup=get_main_menu_keyboard())


# --- ЛОГИКА ФОНОВЫХ НАПОМИНАНИЙ ---
def fallback_travel_estimate(transport_type: str) -> int:
    """Грубая оценка на случай, если нет координат пользователя или 2ГИС недоступен"""
    if "авто" in transport_type.lower():
        return 20
    elif "общественн" in transport_type.lower():
        return 40
    else:
        return 55


# Кэш посчитанных маршрутов, чтобы не дёргать 2ГИС на каждой итерации планировщика (раз в 30 сек)
_ROUTE_CACHE_TTL = timedelta(minutes=3)
_route_cache: dict[int, tuple[datetime, int, bool]] = {}


async def get_cached_travel_time(trip_id, user_lat, user_lon, dest_lat, dest_lon, transport_type):
    """Возвращает (минуты_в_пути, является_ли_приблизительной_оценкой)"""
    now = datetime.now()
    cached = _route_cache.get(trip_id)
    if cached and now - cached[0] < _ROUTE_CACHE_TTL:
        return cached[1], cached[2]

    minutes = None
    logging.info(
        f"[DEBUG] trip_id={trip_id} user=({user_lat},{user_lon}) dest=({dest_lat},{dest_lon})"
    )
    if user_lat is not None and user_lon is not None and dest_lat is not None and dest_lon is not None:
        minutes = await maps.get_travel_time_minutes(user_lat, user_lon, dest_lat, dest_lon, transport_type)
        logging.info(f"[DEBUG] trip_id={trip_id} routing result minutes={minutes}")

    is_estimated = minutes is None
    if minutes is None:
        minutes = fallback_travel_estimate(transport_type)

    _route_cache[trip_id] = (now, minutes, is_estimated)
    return minutes, is_estimated


async def check_and_send_reminders():
    now = datetime.now()
    active_trips = get_all_active_trips_with_buffer()

    for trip in active_trips:
        (trip_id, user_id, destination, arrival_time_str, transport_type,
         reminder_minutes, buffer_percentage, dest_lat, dest_lon, user_lat, user_lon) = trip

        try:
            target_time = datetime.strptime(arrival_time_str, "%d.%m.%Y %H:%M")
        except ValueError:
            continue

        if now > target_time:
            update_trip_status(trip_id, "expired")
            continue

        travel_time, is_estimated = await get_cached_travel_time(
            trip_id, user_lat, user_lon, dest_lat, dest_lon, transport_type
        )
        extra_buffer = int(travel_time * (buffer_percentage / 100))
        total_lead_time_minutes = travel_time + reminder_minutes + extra_buffer
        time_to_remind = target_time - timedelta(minutes=total_lead_time_minutes)

        if now >= time_to_remind:
            route_note = (
                "⚠️ приблизительно, нет точных данных о геолокации/маршруте"
                if is_estimated else "по данным 2ГИС в реальном времени"
            )
            msg = (
                f"⏰ **ПОРА СОБИРАТЬСЯ В ДОРОГУ!**\n\n"
                f"📍 **Пункт назначения:** {destination}\n"
                f"🏁 **Нужно быть на месте в:** {arrival_time_str}\n"
                f"🚗 **Способ:** {transport_type}\n\n"
                f"📊 **Расчет штурмана:**\n"
                f"• В пути: {travel_time} мин. ({route_note})\n"
                f"• Твой запас к выходу: {reminder_minutes} мин.\n"
                f"• Запас на пробки ({buffer_percentage}%): +{extra_buffer} мин.\n"
                f"👉 **Итоговый выезд за:** {total_lead_time_minutes} мин. до цели!"
            )
            try:
                await bot.send_message(chat_id=user_id, text=msg, parse_mode="Markdown")
                await send_route_map(user_id, destination, dest_lat, dest_lon, transport_type)
                update_trip_status(trip_id, "notified")
            except Exception as e:
                logging.error(f"Не удалось отправить сообщение {user_id}: {e}")


async def reminder_scheduler():
    while True:
        try:
            await check_and_send_reminders()
        except asyncio.CancelledError:
            break
        except Exception as e:
            logging.error(f"Ошибка в планировщике: {e}")
        await asyncio.sleep(30)


async def main():
    init_db()
    await bot.delete_webhook(drop_pending_updates=True)
    asyncio.create_task(reminder_scheduler())
    print("Бот успешно запущен!")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
