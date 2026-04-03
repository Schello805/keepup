from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional


BASE_DIR = Path(__file__).resolve().parent
DATABASE_URL = BASE_DIR / "keepup.db"
HISTORY_LIMIT = 30

DEFAULT_SETTINGS = {
    "app_name": "KeepUp",
    "refresh_interval": 10,
    "app_timezone": "UTC",
    "telegram_enabled": False,
    "telegram_bot_token": "",
    "telegram_chat_id": "",
    "smtp_enabled": False,
    "smtp_host": "",
    "smtp_port": 587,
    "smtp_username": "",
    "smtp_password": "",
    "smtp_from_email": "",
    "smtp_to_email": "",
    "smtp_use_tls": True,
    "smtp_use_ssl": False,
}


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DATABASE_URL)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db() -> None:
    with closing(get_db()) as conn:
        cursor = conn.cursor()
        cursor.executescript(
            """
            CREATE TABLE IF NOT EXISTS monitors (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                type TEXT NOT NULL CHECK(type IN ('http', 'ping')),
                target TEXT NOT NULL,
                interval INTEGER NOT NULL DEFAULT 60,
                timeout INTEGER NOT NULL DEFAULT 10,
                enabled INTEGER NOT NULL DEFAULT 1,
                status TEXT NOT NULL DEFAULT 'unknown',
                last_error TEXT,
                last_response_time REAL,
                last_checked_at TEXT,
                last_change_at TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS checks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                monitor_id INTEGER NOT NULL,
                status TEXT NOT NULL,
                response_time REAL,
                error_msg TEXT,
                checked_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (monitor_id) REFERENCES monitors(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            """
        )

        _ensure_monitor_columns(cursor)
        _seed_default_settings(cursor)
        conn.commit()


def _ensure_monitor_columns(cursor: sqlite3.Cursor) -> None:
    cursor.execute("PRAGMA table_info(monitors)")
    existing_columns = {row["name"] for row in cursor.fetchall()}
    required_columns = {
        "timeout": "INTEGER NOT NULL DEFAULT 10",
        "enabled": "INTEGER NOT NULL DEFAULT 1",
        "last_error": "TEXT",
        "last_response_time": "REAL",
        "last_checked_at": "TEXT",
        "last_change_at": "TEXT",
        "updated_at": "TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP",
    }
    for column_name, column_type in required_columns.items():
        if column_name not in existing_columns:
            cursor.execute(f"ALTER TABLE monitors ADD COLUMN {column_name} {column_type}")


def _seed_default_settings(cursor: sqlite3.Cursor) -> None:
    for key, value in DEFAULT_SETTINGS.items():
        cursor.execute(
            "INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)",
            (key, json.dumps(value)),
        )


def row_to_dict(row: Optional[sqlite3.Row]) -> Optional[dict[str, Any]]:
    return dict(row) if row is not None else None


def deserialize_setting(value: str) -> Any:
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return value


def serialize_setting(value: Any) -> str:
    return json.dumps(value)


def get_settings() -> dict[str, Any]:
    with closing(get_db()) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT key, value FROM settings")
        settings = {row["key"]: deserialize_setting(row["value"]) for row in cursor.fetchall()}
    merged = DEFAULT_SETTINGS.copy()
    merged.update(settings)
    return merged


def update_settings(payload: dict[str, Any]) -> dict[str, Any]:
    allowed_keys = set(DEFAULT_SETTINGS.keys())
    filtered_payload = {key: value for key, value in payload.items() if key in allowed_keys}

    with closing(get_db()) as conn:
        cursor = conn.cursor()
        for key, value in filtered_payload.items():
            cursor.execute(
                "INSERT INTO settings (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, serialize_setting(value)),
            )
        conn.commit()

    return get_settings()


def get_monitor(monitor_id: int) -> Optional[dict[str, Any]]:
    with closing(get_db()) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM monitors WHERE id = ?", (monitor_id,))
        monitor = row_to_dict(cursor.fetchone())
    return monitor


def list_monitors() -> list[dict[str, Any]]:
    with closing(get_db()) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM monitors ORDER BY id DESC")
        monitors = [dict(row) for row in cursor.fetchall()]

        for monitor in monitors:
            cursor.execute(
                """
                SELECT status
                FROM checks
                WHERE monitor_id = ?
                ORDER BY checked_at DESC, id DESC
                LIMIT ?
                """,
                (monitor["id"], HISTORY_LIMIT),
            )
            monitor["history"] = [row["status"] for row in reversed(cursor.fetchall())]
            cursor.execute(
                """
                SELECT status
                FROM checks
                WHERE monitor_id = ?
                ORDER BY checked_at DESC
                LIMIT ?
                """,
                (monitor["id"], HISTORY_LIMIT),
            )
            recent_statuses = [row["status"] for row in cursor.fetchall()]
            total = len(recent_statuses)
            up_count = sum(1 for status in recent_statuses if status == "up")
            monitor["uptime_percentage"] = round((up_count / total) * 100, 1) if total else None

    return monitors


def create_monitor(
    name: str,
    monitor_type: str,
    target: str,
    interval: int,
    timeout: int,
    enabled: bool = True,
) -> int:
    now = utc_now()
    with closing(get_db()) as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            INSERT INTO monitors (
                name, type, target, interval, timeout, enabled, status, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, 'unknown', ?, ?)
            """,
            (name.strip(), monitor_type, target.strip(), interval, timeout, int(enabled), now, now),
        )
        monitor_id = cursor.lastrowid
        conn.commit()
    return monitor_id


def update_monitor(
    monitor_id: int,
    name: str,
    monitor_type: str,
    target: str,
    interval: int,
    timeout: int,
) -> None:
    now = utc_now()
    with closing(get_db()) as conn:
        conn.execute(
            """
            UPDATE monitors
            SET name = ?,
                type = ?,
                target = ?,
                interval = ?,
                timeout = ?,
                updated_at = ?
            WHERE id = ?
            """,
            (name.strip(), monitor_type, target.strip(), interval, timeout, now, monitor_id),
        )
        conn.commit()


def set_monitor_enabled(monitor_id: int, enabled: bool) -> None:
    now = utc_now()
    with closing(get_db()) as conn:
        conn.execute(
            """
            UPDATE monitors
            SET enabled = ?,
                updated_at = ?
            WHERE id = ?
            """,
            (int(enabled), now, monitor_id),
        )
        conn.commit()


def delete_monitor(monitor_id: int) -> None:
    with closing(get_db()) as conn:
        conn.execute("DELETE FROM monitors WHERE id = ?", (monitor_id,))
        conn.commit()


def get_recent_logs(monitor_id: int, limit: int = 8) -> list[dict[str, Any]]:
    with closing(get_db()) as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT id, status, response_time, error_msg, checked_at
            FROM checks
            WHERE monitor_id = ?
            ORDER BY checked_at DESC, id DESC
            LIMIT ?
            """,
            (monitor_id, limit),
        )
        return [dict(row) for row in cursor.fetchall()]


def log_check_result(
    monitor_id: int,
    new_status: str,
    response_time: Optional[float],
    error_msg: Optional[str],
    checked_at: Optional[str] = None,
) -> dict[str, Any]:
    timestamp = checked_at or utc_now()
    with closing(get_db()) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT status FROM monitors WHERE id = ?", (monitor_id,))
        current_row = cursor.fetchone()
        previous_status = current_row["status"] if current_row else "unknown"
        status_changed = previous_status != new_status and previous_status != "unknown"

        cursor.execute(
            """
            INSERT INTO checks (monitor_id, status, response_time, error_msg, checked_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (monitor_id, new_status, response_time, error_msg, timestamp),
        )
        cursor.execute(
            """
            UPDATE monitors
            SET status = ?,
                last_error = ?,
                last_response_time = ?,
                last_checked_at = ?,
                last_change_at = CASE
                    WHEN status <> ? THEN ?
                    ELSE last_change_at
                END,
                updated_at = ?
            WHERE id = ?
            """,
            (new_status, error_msg, response_time, timestamp, new_status, timestamp, timestamp, monitor_id),
        )
        conn.commit()

    return {
        "monitor_id": monitor_id,
        "previous_status": previous_status,
        "status": new_status,
        "status_changed": status_changed,
        "checked_at": timestamp,
        "error_msg": error_msg,
        "response_time": response_time,
    }


def export_backup() -> dict[str, Any]:
    with closing(get_db()) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM monitors ORDER BY id ASC")
        monitors = [dict(row) for row in cursor.fetchall()]
        cursor.execute("SELECT * FROM checks ORDER BY id ASC")
        checks = [dict(row) for row in cursor.fetchall()]
        cursor.execute("SELECT key, value FROM settings ORDER BY key ASC")
        settings = {row["key"]: deserialize_setting(row["value"]) for row in cursor.fetchall()}

    return {
        "version": 1,
        "exported_at": utc_now(),
        "monitors": monitors,
        "checks": checks,
        "settings": settings,
    }


def import_backup(payload: dict[str, Any]) -> None:
    monitors = payload.get("monitors", [])
    checks = payload.get("checks", [])
    settings = payload.get("settings", {})

    with closing(get_db()) as conn:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM checks")
        cursor.execute("DELETE FROM monitors")
        cursor.execute("DELETE FROM settings")
        _seed_default_settings(cursor)

        for key, value in settings.items():
            if key in DEFAULT_SETTINGS:
                cursor.execute(
                    "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
                    (key, serialize_setting(value)),
                )

        for monitor in monitors:
            cursor.execute(
                """
                INSERT INTO monitors (
                    id, name, type, target, interval, timeout, enabled, status, last_error,
                    last_response_time, last_checked_at, last_change_at, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    monitor.get("id"),
                    monitor["name"],
                    monitor["type"],
                    monitor["target"],
                    monitor.get("interval", 60),
                    monitor.get("timeout", 10),
                    int(bool(monitor.get("enabled", True))),
                    monitor.get("status", "unknown"),
                    monitor.get("last_error"),
                    monitor.get("last_response_time"),
                    monitor.get("last_checked_at"),
                    monitor.get("last_change_at"),
                    monitor.get("created_at", utc_now()),
                    monitor.get("updated_at", utc_now()),
                ),
            )

        for check in checks:
            cursor.execute(
                """
                INSERT INTO checks (id, monitor_id, status, response_time, error_msg, checked_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    check.get("id"),
                    check["monitor_id"],
                    check["status"],
                    check.get("response_time"),
                    check.get("error_msg"),
                    check.get("checked_at", utc_now()),
                ),
            )

        conn.commit()
