"""SQLite хранилище для состояния мониторинга."""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional


@dataclass
class WatchingState:
    """Состояние отслеживания."""
    
    user_id: int
    status: str  # 'active', 'paused', 'sleeping'
    title: Optional[str] = None
    date_range: Optional[str] = None
    seats_count: Optional[int] = None
    started_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


class SQLiteStorage:
    """Хранилище состояния в SQLite."""
    
    def __init__(self, db_path: str | Path = "./data/watching.db"):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()
    
    def _init_db(self):
        """Инициализировать базу данных."""
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS watching_state (
                    user_id INTEGER PRIMARY KEY,
                    status TEXT DEFAULT 'sleeping',
                    title TEXT,
                    date_range TEXT,
                    seats_count INTEGER,
                    started_at TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            
            conn.execute("""
                CREATE TABLE IF NOT EXISTS bookings (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER,
                    title TEXT,
                    date TEXT,
                    time TEXT,
                    seats TEXT,
                    screenshot_path TEXT,
                    session_url TEXT,
                    status TEXT,
                    booked_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    reserved_at TIMESTAMP,
                    unfreeze_at TIMESTAMP,
                    user_confirmed BOOLEAN,
                    confirmed_at TIMESTAMP
                )
            """)
            
            columns = {
                row[1]
                for row in conn.execute("PRAGMA table_info(bookings)")
            }
            if "reserved_at" not in columns:
                conn.execute("ALTER TABLE bookings ADD COLUMN reserved_at TIMESTAMP")
            if "unfreeze_at" not in columns:
                conn.execute("ALTER TABLE bookings ADD COLUMN unfreeze_at TIMESTAMP")
            
            conn.commit()
    
    def get_watching_state(self, user_id: int) -> Optional[WatchingState]:
        """Получить состояние отслеживания пользователя."""
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.execute(
                "SELECT * FROM watching_state WHERE user_id = ?",
                (user_id,)
            )
            row = cursor.fetchone()
        
        if not row:
            return None
        
        return WatchingState(
            user_id=row[0],
            status=row[1],
            title=row[2],
            date_range=row[3],
            seats_count=row[4],
            started_at=datetime.fromisoformat(row[5]) if row[5] else None,
            updated_at=datetime.fromisoformat(row[6]) if row[6] else None,
        )
    
    def set_watching_state(
        self,
        user_id: int,
        status: str,
        title: Optional[str] = None,
        date_range: Optional[str] = None,
        seats_count: Optional[int] = None,
    ):
        """Установить или обновить состояние отслеживания."""
        now = datetime.now().isoformat()
        started_at = now if status == 'active' else None
        
        with sqlite3.connect(self.db_path) as conn:
            # Проверяем, существует ли запись
            cursor = conn.execute(
                "SELECT 1 FROM watching_state WHERE user_id = ?",
                (user_id,)
            )
            exists = cursor.fetchone() is not None
            
            if exists:
                conn.execute(
                    """UPDATE watching_state 
                       SET status = ?, title = ?, date_range = ?, seats_count = ?, updated_at = ?
                       WHERE user_id = ?""",
                    (status, title, date_range, seats_count, now, user_id)
                )
            else:
                conn.execute(
                    """INSERT INTO watching_state 
                       (user_id, status, title, date_range, seats_count, started_at, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (user_id, status, title, date_range, seats_count, started_at, now)
                )
            
            conn.commit()
    
    def save_booking(
        self,
        user_id: int,
        title: str,
        date: str,
        time: str,
        seats: str,
        screenshot_path: Optional[str] = None,
        session_url: Optional[str] = None,
        reserved_at: Optional[str] = None,
        unfreeze_at: Optional[str] = None,
    ):
        """Сохранить информацию о бронировании."""
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """INSERT INTO bookings
                   (user_id, title, date, time, seats, screenshot_path, session_url, status, reserved_at, unfreeze_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    user_id,
                    title,
                    date,
                    time,
                    seats,
                    screenshot_path,
                    session_url,
                    "booked",
                    reserved_at,
                    unfreeze_at,
                )
            )
            conn.commit()
    
    def confirm_booking(self, user_id: int, confirmed: bool):
        """Подтвердить успешность бронирования."""
        now = datetime.now().isoformat()
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """UPDATE bookings 
                   SET user_confirmed = ?, confirmed_at = ?
                   WHERE user_id = ? AND user_confirmed IS NULL
                   ORDER BY booked_at DESC LIMIT 1""",
                (confirmed, now if confirmed else now, user_id)
            )
            conn.commit()
    
    def get_last_booking(self, user_id: int) -> Optional[dict]:
        """Получить последнее бронирование пользователя."""
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.execute(
                """SELECT title, date, time, seats, screenshot_path, session_url, booked_at, reserved_at, unfreeze_at
                   FROM bookings
                   WHERE user_id = ? AND status = 'booked'
                   ORDER BY booked_at DESC LIMIT 1""",
                (user_id,)
            )
            row = cursor.fetchone()
        
        if not row:
            return None
        
        return {
            "title": row[0],
            "date": row[1],
            "time": row[2],
            "seats": row[3],
            "screenshot_path": row[4],
            "session_url": row[5],
            "booked_at": row[6],
            "reserved_at": row[7],
            "unfreeze_at": row[8],
        }
