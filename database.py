import sqlite3
import os

# Автоматически определяем папку, в которой лежит этот файл (database.py)
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
# Формируем абсолютный путь к файлу базы данных в этой же папке
DB_PATH = os.path.join(BASE_DIR, 'trips.db')


def _add_column_if_missing(cursor, table, column, definition):
    """Добавляет колонку в таблицу, если её ещё нет (простая миграция для SQLite)."""
    cursor.execute(f"PRAGMA table_info({table})")
    existing_columns = {row[1] for row in cursor.fetchall()}
    if column not in existing_columns:
        cursor.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def init_db():
    """Инициализация базы данных, создание таблиц и миграция схемы"""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    # Таблица пользователей
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            buffer_percentage INTEGER DEFAULT 10
        )
    ''')

    # Таблица поездок
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS trips (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            destination TEXT,
            arrival_time TEXT,
            transport_type TEXT,
            reminder_minutes INTEGER,
            status TEXT DEFAULT 'active',
            FOREIGN KEY (user_id) REFERENCES users (user_id)
        )
    ''')

    # Миграция: последняя известная геолокация пользователя (для расчёта маршрута)
    _add_column_if_missing(cursor, "users", "last_lat", "REAL")
    _add_column_if_missing(cursor, "users", "last_lon", "REAL")
    _add_column_if_missing(cursor, "users", "last_location_at", "TEXT")

    # Миграция: геокодированные координаты места назначения поездки
    _add_column_if_missing(cursor, "trips", "dest_lat", "REAL")
    _add_column_if_missing(cursor, "trips", "dest_lon", "REAL")

    conn.commit()
    conn.close()
    print("База данных успешно инициализирована!")


def add_user_if_not_exists(user_id):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute('INSERT OR IGNORE INTO users (user_id) VALUES (?)', (user_id,))
    conn.commit()
    conn.close()


def update_user_buffer(user_id, buffer_percentage):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute('UPDATE users SET buffer_percentage = ? WHERE user_id = ?', (buffer_percentage, user_id))
    conn.commit()
    conn.close()


def update_user_location(user_id, lat, lon):
    """Сохраняет последнюю известную геолокацию пользователя (в т.ч. live-геолокацию)"""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute('''
        UPDATE users
        SET last_lat = ?, last_lon = ?, last_location_at = datetime('now')
        WHERE user_id = ?
    ''', (lat, lon, user_id))
    conn.commit()
    conn.close()


def get_user_location(user_id):
    """Возвращает (lat, lon) последней известной геолокации пользователя или (None, None)"""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute('SELECT last_lat, last_lon FROM users WHERE user_id = ?', (user_id,))
    row = cursor.fetchone()
    conn.close()
    if not row:
        return None, None
    return row[0], row[1]


def add_trip(user_id, destination, arrival_time, transport_type, reminder_minutes, dest_lat=None, dest_lon=None):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute('''
        INSERT INTO trips (user_id, destination, arrival_time, transport_type, reminder_minutes, dest_lat, dest_lon)
        VALUES (?, ?, ?, ?, ?, ?, ?)
    ''', (user_id, destination, arrival_time, transport_type, reminder_minutes, dest_lat, dest_lon))
    conn.commit()
    conn.close()


def get_active_trips(user_id):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute('''
        SELECT id, destination, arrival_time, transport_type, reminder_minutes, dest_lat, dest_lon
        FROM trips 
        WHERE user_id = ? AND status IN ('active', 'notified')
    ''', (user_id,))
    trips = cursor.fetchall()
    conn.close()
    return trips


def get_past_trips(user_id, limit=20):
    """Прошедшие поездки пользователя (только для просмотра, без редактирования)"""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute('''
        SELECT id, destination, arrival_time, transport_type, reminder_minutes, status
        FROM trips
        WHERE user_id = ? AND status = 'expired'
        ORDER BY id DESC
        LIMIT ?
    ''', (user_id, limit))
    trips = cursor.fetchall()
    conn.close()
    return trips


def delete_trip(trip_id):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute('DELETE FROM trips WHERE id = ?', (trip_id,))
    conn.commit()
    conn.close()


def get_all_active_trips_with_buffer():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute('''
        SELECT t.id, t.user_id, t.destination, t.arrival_time, t.transport_type, t.reminder_minutes,
               u.buffer_percentage, t.dest_lat, t.dest_lon, u.last_lat, u.last_lon
        FROM trips t
        JOIN users u ON t.user_id = u.user_id
        WHERE t.status = 'active'
    ''')
    trips = cursor.fetchall()
    conn.close()
    return trips


def update_trip_status(trip_id, status):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute('UPDATE trips SET status = ? WHERE id = ?', (status, trip_id))
    conn.commit()
    conn.close()


def get_trip_by_id(trip_id):
    """Получает данные конкретной поездки по её ID"""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute('''
        SELECT id, destination, arrival_time, transport_type, reminder_minutes, dest_lat, dest_lon
        FROM trips 
        WHERE id = ?
    ''', (trip_id,))
    trip = cursor.fetchone()
    conn.close()
    return trip


def update_trip_field(trip_id, field_name, new_value):
    """Обновляет конкретное поле поездки в БД и возвращает статус active"""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    # Белый список полей для защиты от SQL-инъекций
    allowed_fields = ["destination", "arrival_time", "transport_type", "reminder_minutes", "dest_lat", "dest_lon"]
    if field_name not in allowed_fields:
        conn.close()
        raise ValueError(f"Недопустимое имя поля: {field_name}")

    query = f"UPDATE trips SET {field_name} = ?, status = 'active' WHERE id = ?"
    cursor.execute(query, (new_value, trip_id))
    conn.commit()
    conn.close()
