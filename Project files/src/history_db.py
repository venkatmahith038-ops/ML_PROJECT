"""
history_db.py
=============
Lightweight SQLite storage for prediction history.
No server setup needed -- creates a single file: models/history.db
"""

import os
import sqlite3
from datetime import datetime, timedelta

BASE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
DB_PATH = os.path.join(BASE_DIR, "models", "history.db")


def _connect():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = _connect()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS prediction_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            logged_at TEXT NOT NULL,
            lot TEXT NOT NULL,
            requested_datetime TEXT NOT NULL,
            capacity INTEGER,
            predicted_occupied_pct REAL,
            predicted_available_pct REAL,
            status TEXT
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_history_lot ON prediction_history(lot)
    """)
    conn.commit()
    conn.close()


def log_prediction(result: dict):
    """Save one prediction result (the dict returned by predict_availability)."""
    conn = _connect()
    conn.execute(
        """INSERT INTO prediction_history
           (logged_at, lot, requested_datetime, capacity,
            predicted_occupied_pct, predicted_available_pct, status)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (
            datetime.now().isoformat(timespec="seconds"),
            result["lot"],
            result["datetime"],
            result.get("capacity"),
            result.get("predicted_occupied_pct"),
            result.get("predicted_available_pct"),
            result.get("status"),
        ),
    )
    conn.commit()
    conn.close()


def get_history(lot: str = None, limit: int = 50) -> list:
    """Most recent logged predictions, optionally filtered by lot."""
    conn = _connect()
    if lot:
        rows = conn.execute(
            """SELECT * FROM prediction_history WHERE lot = ?
               ORDER BY id DESC LIMIT ?""",
            (lot, limit),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM prediction_history ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def clear_old(days: int = 30):
    """Optional housekeeping: delete entries older than N days."""
    cutoff = (datetime.now() - timedelta(days=days)).isoformat(timespec="seconds")
    conn = _connect()
    conn.execute("DELETE FROM prediction_history WHERE logged_at < ?", (cutoff,))
    conn.commit()
    conn.close()


init_db()
