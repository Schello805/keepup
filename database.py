from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional


BASE_DIR = Path(__file__).resolve().parent
DATABASE_URL = BASE_DIR / "keepup.db"
HISTORY_LIMIT = 30

DEFAULT_SETTINGS = {
    "app_name": "KeepUp",
    "refresh_interval": 10,
    "app_timezone": "UTC",
    "default_monitor_interval": 60,
    "global_monitor_interval_override": 0,
    "down_failures_threshold": 3,
    "up_successes_threshold": 1,
    "retention_days": 7,
    "flapping_window_minutes": 15,
    "flapping_transition_threshold": 3,
    "notification_batch_window_seconds": 30,
    "scheduler_jitter_seconds": 10,
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


def _parse_iso(ts: Optional[str]) -> Optional[datetime]:
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _format_duration_seconds(seconds: Optional[float]) -> Optional[str]:
    if seconds is None:
        return None
    seconds = max(0, int(round(seconds)))
    mins, sec = divmod(seconds, 60)
    hrs, mins = divmod(mins, 60)
    days, hrs = divmod(hrs, 24)
    if days:
        return f"{days}d {hrs}h"
    if hrs:
        return f"{hrs}h {mins}m"
    if mins:
        return f"{mins}m"
    return f"{sec}s"


def _compute_sla_window(
    cursor: sqlite3.Cursor,
    monitor_id: int,
    window_days: int,
    now: datetime,
) -> dict[str, Any]:
    window_days = max(1, int(window_days))
    window_start = now - timedelta(days=window_days)
    window_seconds = (now - window_start).total_seconds()

    cursor.execute(
        """
        SELECT started_at, ended_at
        FROM incidents
        WHERE monitor_id = ?
          AND started_at <= ?
          AND (ended_at IS NULL OR ended_at >= ?)
        ORDER BY started_at ASC, id ASC
        """,
        (monitor_id, now.isoformat(), window_start.isoformat()),
    )
    incidents = cursor.fetchall()

    downtime_seconds = 0.0
    incident_count = 0
    mttr_durations: list[float] = []

    for row in incidents:
        started = _parse_iso(row["started_at"]) or window_start
        ended = _parse_iso(row["ended_at"]) or now

        overlap_start = max(started, window_start)
        overlap_end = min(ended, now)
        if overlap_end <= overlap_start:
            continue

        incident_count += 1
        downtime_seconds += (overlap_end - overlap_start).total_seconds()

        if row["ended_at"] is not None:
            mttr_durations.append((ended - started).total_seconds())

    downtime_seconds = min(downtime_seconds, window_seconds)
    uptime_ratio = 1.0 if window_seconds <= 0 else max(0.0, (window_seconds - downtime_seconds) / window_seconds)
    uptime_pct = round(uptime_ratio * 100.0, 3)
    mttr_seconds = (sum(mttr_durations) / len(mttr_durations)) if mttr_durations else None

    return {
        "window_days": window_days,
        "uptime_pct": uptime_pct,
        "incident_count": incident_count,
        "downtime_seconds": int(round(downtime_seconds)),
        "downtime_human": _format_duration_seconds(downtime_seconds),
        "mttr_seconds": int(round(mttr_seconds)) if mttr_seconds is not None else None,
        "mttr_human": _format_duration_seconds(mttr_seconds),
    }


def get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DATABASE_URL, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    conn.execute("PRAGMA busy_timeout = 5000")
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
                http_method TEXT NOT NULL DEFAULT 'GET',
                retry_count INTEGER NOT NULL DEFAULT 2,
                interval INTEGER NOT NULL DEFAULT 60,
                timeout INTEGER NOT NULL DEFAULT 10,
                enabled INTEGER NOT NULL DEFAULT 1,
                status TEXT NOT NULL DEFAULT 'unknown',
                last_error TEXT,
                last_response_time REAL,
                last_checked_at TEXT,
                last_change_at TEXT,
                consecutive_failures INTEGER NOT NULL DEFAULT 0,
                consecutive_successes INTEGER NOT NULL DEFAULT 0,
                expected_text TEXT,
                forbidden_text TEXT,
                last_error_category TEXT,
                is_flapping INTEGER NOT NULL DEFAULT 0,
                flapping_until TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS checks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                monitor_id INTEGER NOT NULL,
                status TEXT NOT NULL,
                response_time REAL,
                error_msg TEXT,
                error_category TEXT,
                checked_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (monitor_id) REFERENCES monitors(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS incidents (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                monitor_id INTEGER NOT NULL,
                started_at TEXT NOT NULL,
                ended_at TEXT,
                start_check_id INTEGER,
                end_check_id INTEGER,
                start_error_msg TEXT,
                end_error_msg TEXT,
                first_failed_at TEXT,
                confirmed_down_at TEXT,
                first_recovered_at TEXT,
                confirmed_up_at TEXT,
                confirmation_attempts INTEGER,
                recovery_attempts INTEGER,
                start_error_category TEXT,
                end_error_category TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (monitor_id) REFERENCES monitors(id) ON DELETE CASCADE,
                FOREIGN KEY (start_check_id) REFERENCES checks(id) ON DELETE SET NULL,
                FOREIGN KEY (end_check_id) REFERENCES checks(id) ON DELETE SET NULL
            );

            CREATE INDEX IF NOT EXISTS idx_checks_monitor_checked
            ON checks (monitor_id, checked_at DESC, id DESC);

            CREATE INDEX IF NOT EXISTS idx_checks_monitor_status_checked
            ON checks (monitor_id, status, checked_at DESC, id DESC);

            CREATE INDEX IF NOT EXISTS idx_incidents_monitor_started
            ON incidents (monitor_id, started_at DESC, ended_at);
            """
        )

        _ensure_monitor_columns(cursor)
        _ensure_check_columns(cursor)
        _ensure_incident_columns(cursor)
        _seed_default_settings(cursor)
        conn.commit()


def _ensure_monitor_columns(cursor: sqlite3.Cursor) -> None:
    cursor.execute("PRAGMA table_info(monitors)")
    existing_columns = {row["name"] for row in cursor.fetchall()}
    required_columns = {
        "http_method": "TEXT NOT NULL DEFAULT 'GET'",
        "retry_count": "INTEGER NOT NULL DEFAULT 2",
        "timeout": "INTEGER NOT NULL DEFAULT 10",
        "enabled": "INTEGER NOT NULL DEFAULT 1",
        "last_error": "TEXT",
        "last_response_time": "REAL",
        "last_checked_at": "TEXT",
        "last_change_at": "TEXT",
        "consecutive_failures": "INTEGER NOT NULL DEFAULT 0",
        "consecutive_successes": "INTEGER NOT NULL DEFAULT 0",
        "expected_text": "TEXT",
        "forbidden_text": "TEXT",
        "last_error_category": "TEXT",
        "is_flapping": "INTEGER NOT NULL DEFAULT 0",
        "flapping_until": "TEXT",
        "updated_at": "TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP",
    }
    for column_name, column_type in required_columns.items():
        if column_name not in existing_columns:
            cursor.execute(f"ALTER TABLE monitors ADD COLUMN {column_name} {column_type}")


def _ensure_check_columns(cursor: sqlite3.Cursor) -> None:
    cursor.execute("PRAGMA table_info(checks)")
    existing_columns = {row["name"] for row in cursor.fetchall()}
    required_columns = {
        "error_category": "TEXT",
    }
    for column_name, column_type in required_columns.items():
        if column_name not in existing_columns:
            cursor.execute(f"ALTER TABLE checks ADD COLUMN {column_name} {column_type}")


def _ensure_incident_columns(cursor: sqlite3.Cursor) -> None:
    cursor.execute("PRAGMA table_info(incidents)")
    existing_columns = {row["name"] for row in cursor.fetchall()}
    required_columns = {
        "first_failed_at": "TEXT",
        "confirmed_down_at": "TEXT",
        "first_recovered_at": "TEXT",
        "confirmed_up_at": "TEXT",
        "confirmation_attempts": "INTEGER",
        "recovery_attempts": "INTEGER",
        "start_error_category": "TEXT",
        "end_error_category": "TEXT",
    }
    for column_name, column_type in required_columns.items():
        if column_name not in existing_columns:
            cursor.execute(f"ALTER TABLE incidents ADD COLUMN {column_name} {column_type}")


def _get_int_setting(cursor: sqlite3.Cursor, key: str, default: int) -> int:
    try:
        cursor.execute("SELECT value FROM settings WHERE key = ?", (key,))
        row = cursor.fetchone()
        if not row:
            return default
        raw = row["value"]
        parsed = deserialize_setting(raw)
        val = int(parsed)
        return val if val > 0 else default
    except Exception:
        return default


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


def list_monitor_options() -> list[dict[str, Any]]:
    with closing(get_db()) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT id, name FROM monitors ORDER BY id DESC")
        return [dict(row) for row in cursor.fetchall()]


def list_monitor_incident_feed_options() -> list[dict[str, Any]]:
    with closing(get_db()) as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT id, name, type, target, created_at
            FROM monitors
            ORDER BY id DESC
            """
        )
        return [dict(row) for row in cursor.fetchall()]


def get_monitor_summary() -> dict[str, int]:
    with closing(get_db()) as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT
                COUNT(*) AS total,
                SUM(CASE WHEN enabled = 1 AND status = 'up' THEN 1 ELSE 0 END) AS up_count,
                SUM(CASE WHEN enabled = 1 AND status = 'down' THEN 1 ELSE 0 END) AS down_count,
                SUM(CASE WHEN enabled = 1 AND status = 'unknown' THEN 1 ELSE 0 END) AS unknown_count,
                SUM(CASE WHEN enabled = 0 THEN 1 ELSE 0 END) AS paused_count
            FROM monitors
            """
        )
        row = cursor.fetchone()
        return {
            "total": int((row["total"] if row and row["total"] is not None else 0) or 0),
            "up": int((row["up_count"] if row and row["up_count"] is not None else 0) or 0),
            "down": int((row["down_count"] if row and row["down_count"] is not None else 0) or 0),
            "unknown": int((row["unknown_count"] if row and row["unknown_count"] is not None else 0) or 0),
            "paused": int((row["paused_count"] if row and row["paused_count"] is not None else 0) or 0),
        }


def list_monitors(
    monitor_ids: Optional[list[int]] = None,
    include_heavy_details: bool = False,
) -> list[dict[str, Any]]:
    with closing(get_db()) as conn:
        cursor = conn.cursor()
        if monitor_ids:
            placeholders = ", ".join("?" for _ in monitor_ids)
            cursor.execute(
                f"SELECT * FROM monitors WHERE id IN ({placeholders}) ORDER BY id DESC",
                tuple(monitor_ids),
            )
        else:
            cursor.execute("SELECT * FROM monitors ORDER BY id DESC")
        monitors = [dict(row) for row in cursor.fetchall()]
        if not monitors:
            return []

        resolved_monitor_ids = [monitor["id"] for monitor in monitors]
        placeholders = ", ".join("?" for _ in resolved_monitor_ids)

        now_dt = datetime.now(timezone.utc).replace(microsecond=0)

        cursor.execute(
            f"""
            SELECT monitor_id, status, response_time, checked_at
            FROM (
                SELECT
                    monitor_id,
                    status,
                    response_time,
                    checked_at,
                    id,
                    ROW_NUMBER() OVER (
                        PARTITION BY monitor_id
                        ORDER BY checked_at DESC, id DESC
                    ) AS rn
                FROM checks
                WHERE monitor_id IN ({placeholders})
            )
            WHERE rn <= ?
            ORDER BY monitor_id, checked_at DESC
            """,
            (*resolved_monitor_ids, HISTORY_LIMIT),
        )
        recent_checks_by_monitor: dict[int, list[sqlite3.Row]] = {monitor_id: [] for monitor_id in resolved_monitor_ids}
        for row in cursor.fetchall():
            recent_checks_by_monitor[row["monitor_id"]].append(row)

        cursor.execute(
            f"""
            SELECT monitor_id, MAX(checked_at) AS last_success_at
            FROM checks
            WHERE status = 'up'
              AND monitor_id IN ({placeholders})
            GROUP BY monitor_id
            """,
            tuple(resolved_monitor_ids),
        )
        last_success_map = {
            row["monitor_id"]: row["last_success_at"]
            for row in cursor.fetchall()
        }

        incident_rows_by_monitor: dict[int, list[sqlite3.Row]] = {}
        if include_heavy_details:
            cursor.execute(
                f"""
                SELECT monitor_id, started_at, ended_at
                FROM incidents
                WHERE monitor_id IN ({placeholders})
                  AND started_at <= ?
                ORDER BY monitor_id ASC, started_at ASC, id ASC
                """,
                (*resolved_monitor_ids, now_dt.isoformat()),
            )
            incident_rows_by_monitor = {monitor_id: [] for monitor_id in resolved_monitor_ids}
            for row in cursor.fetchall():
                incident_rows_by_monitor[row["monitor_id"]].append(row)

        for monitor in monitors:
            rows = list(reversed(recent_checks_by_monitor.get(monitor["id"], [])))
            monitor["history"] = [r["status"] for r in rows]

            recent_statuses = [row["status"] for row in recent_checks_by_monitor.get(monitor["id"], [])]
            total = len(recent_statuses)
            up_count = sum(1 for status in recent_statuses if status == "up")
            monitor["uptime_percentage"] = round((up_count / total) * 100, 1) if total else None

            monitor["last_success_at"] = last_success_map.get(monitor["id"])
            if include_heavy_details:
                monitor["chart_data_json"] = json.dumps([
                    {"x": r["checked_at"], "y": r["response_time"]}
                    for r in rows
                ])
                monitor["sla"] = {
                    "7d": _compute_sla_window_from_rows(incident_rows_by_monitor.get(monitor["id"], []), 7, now_dt),
                    "30d": _compute_sla_window_from_rows(incident_rows_by_monitor.get(monitor["id"], []), 30, now_dt),
                    "90d": _compute_sla_window_from_rows(incident_rows_by_monitor.get(monitor["id"], []), 90, now_dt),
                }

    return monitors


def _compute_sla_window_from_rows(
    incidents: list[sqlite3.Row],
    window_days: int,
    now: datetime,
) -> dict[str, Any]:
    window_days = max(1, int(window_days))
    window_start = now - timedelta(days=window_days)
    window_seconds = (now - window_start).total_seconds()

    downtime_seconds = 0.0
    incident_count = 0
    mttr_durations: list[float] = []

    for row in incidents:
        started = _parse_iso(row["started_at"]) or window_start
        ended = _parse_iso(row["ended_at"]) or now

        overlap_start = max(started, window_start)
        overlap_end = min(ended, now)
        if overlap_end <= overlap_start:
            continue

        incident_count += 1
        downtime_seconds += (overlap_end - overlap_start).total_seconds()

        if row["ended_at"] is not None:
            mttr_durations.append((ended - started).total_seconds())

    downtime_seconds = min(downtime_seconds, window_seconds)
    uptime_ratio = 1.0 if window_seconds <= 0 else max(0.0, (window_seconds - downtime_seconds) / window_seconds)
    uptime_pct = round(uptime_ratio * 100.0, 3)
    mttr_seconds = (sum(mttr_durations) / len(mttr_durations)) if mttr_durations else None

    return {
        "window_days": window_days,
        "uptime_pct": uptime_pct,
        "incident_count": incident_count,
        "downtime_seconds": int(round(downtime_seconds)),
        "downtime_human": _format_duration_seconds(downtime_seconds),
        "mttr_seconds": int(round(mttr_seconds)) if mttr_seconds is not None else None,
        "mttr_human": _format_duration_seconds(mttr_seconds),
    }


def create_monitor(
    name: str,
    monitor_type: str,
    target: str,
    http_method: str,
    retry_count: int,
    interval: int,
    timeout: int,
    expected_text: str = "",
    forbidden_text: str = "",
    enabled: bool = True,
) -> int:
    now = utc_now()
    with closing(get_db()) as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            INSERT INTO monitors (
                name, type, target, http_method, retry_count, interval, timeout,
                expected_text, forbidden_text, enabled, status, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'unknown', ?, ?)
            """,
            (
                name.strip(),
                monitor_type,
                target.strip(),
                http_method.strip().upper(),
                max(0, int(retry_count)),
                interval,
                timeout,
                expected_text.strip() or None,
                forbidden_text.strip() or None,
                int(enabled),
                now,
                now,
            ),
        )
        monitor_id = cursor.lastrowid
        conn.commit()
    return monitor_id


def update_monitor(
    monitor_id: int,
    name: str,
    monitor_type: str,
    target: str,
    http_method: str,
    retry_count: int,
    interval: int,
    timeout: int,
    expected_text: str = "",
    forbidden_text: str = "",
) -> None:
    now = utc_now()
    with closing(get_db()) as conn:
        conn.execute(
            """
            UPDATE monitors
            SET name = ?,
                type = ?,
                target = ?,
                http_method = ?,
                retry_count = ?,
                interval = ?,
                timeout = ?,
                expected_text = ?,
                forbidden_text = ?,
                updated_at = ?
            WHERE id = ?
            """,
            (
                name.strip(),
                monitor_type,
                target.strip(),
                http_method.strip().upper(),
                max(0, int(retry_count)),
                interval,
                timeout,
                expected_text.strip() or None,
                forbidden_text.strip() or None,
                now,
                monitor_id,
            ),
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


def cleanup_old_checks(days: Optional[int] = None) -> None:
    if days is None:
        try:
            settings = get_settings()
            days = max(1, int(settings.get("retention_days", DEFAULT_SETTINGS["retention_days"])))
        except Exception:
            days = int(DEFAULT_SETTINGS["retention_days"])
    with closing(get_db()) as conn:
        conn.execute(
            '''
            DELETE FROM checks 
            WHERE checked_at < datetime('now', ?) 
              AND monitor_id NOT IN (SELECT id FROM monitors WHERE status = 'down')
            ''',
            (f"-{days} days",)
        )
        conn.commit()


def get_recent_logs(monitor_id: int, limit: int = 8) -> list[dict[str, Any]]:
    with closing(get_db()) as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT id, status, response_time, error_msg, error_category, checked_at
            FROM checks
            WHERE monitor_id = ?
            ORDER BY checked_at DESC, id DESC
            LIMIT ?
            """,
            (monitor_id, limit),
        )
        return [dict(row) for row in cursor.fetchall()]


def get_recent_logs_for_monitors(monitor_ids: list[int], limit: int = 8) -> dict[int, list[dict[str, Any]]]:
    if not monitor_ids:
        return {}

    placeholders = ", ".join("?" for _ in monitor_ids)
    grouped_logs: dict[int, list[dict[str, Any]]] = {monitor_id: [] for monitor_id in monitor_ids}

    with closing(get_db()) as conn:
        cursor = conn.cursor()
        cursor.execute(
            f"""
            SELECT monitor_id, id, status, response_time, error_msg, error_category, checked_at
            FROM (
                SELECT
                    monitor_id,
                    id,
                    status,
                    response_time,
                    error_msg,
                    error_category,
                    checked_at,
                    ROW_NUMBER() OVER (
                        PARTITION BY monitor_id
                        ORDER BY checked_at DESC, id DESC
                    ) AS rn
                FROM checks
                WHERE monitor_id IN ({placeholders})
            )
            WHERE rn <= ?
            ORDER BY monitor_id ASC, checked_at DESC, id DESC
            """,
            (*monitor_ids, limit),
        )
        for row in cursor.fetchall():
            grouped_logs[row["monitor_id"]].append(dict(row))

    return grouped_logs


def _get_first_recent_status_at(
    cursor: sqlite3.Cursor,
    monitor_id: int,
    status: str,
    count: int,
    fallback: str,
) -> str:
    if count <= 1:
        return fallback
    cursor.execute(
        """
        SELECT checked_at
        FROM checks
        WHERE monitor_id = ? AND status = ?
        ORDER BY checked_at DESC, id DESC
        LIMIT ?
        """,
        (monitor_id, status, count),
    )
    rows = cursor.fetchall()
    if len(rows) < count:
        return fallback
    return rows[-1]["checked_at"] or fallback


def _detect_flapping(
    cursor: sqlite3.Cursor,
    monitor_id: int,
    timestamp: str,
    window_minutes: int,
    transition_threshold: int,
) -> tuple[bool, Optional[str]]:
    now_dt = _parse_iso(timestamp) or datetime.now(timezone.utc).replace(microsecond=0)
    window_minutes = max(1, int(window_minutes))
    transition_threshold = max(2, int(transition_threshold))
    cutoff = (now_dt - timedelta(minutes=window_minutes)).isoformat()
    cursor.execute(
        """
        SELECT started_at, ended_at
        FROM incidents
        WHERE monitor_id = ?
          AND (started_at >= ? OR ended_at >= ?)
        """,
        (monitor_id, cutoff, cutoff),
    )
    transitions = 0
    for row in cursor.fetchall():
        if row["started_at"] and row["started_at"] >= cutoff:
            transitions += 1
        if row["ended_at"] and row["ended_at"] >= cutoff:
            transitions += 1
    if transitions >= transition_threshold:
        return True, (now_dt + timedelta(minutes=window_minutes)).isoformat()
    return False, None


def log_check_result(
    monitor_id: int,
    new_status: str,
    response_time: Optional[float],
    error_msg: Optional[str],
    error_category: Optional[str] = None,
    checked_at: Optional[str] = None,
) -> dict[str, Any]:
    timestamp = checked_at or utc_now()
    with closing(get_db()) as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT status, consecutive_failures, consecutive_successes, flapping_until
            FROM monitors
            WHERE id = ?
            """,
            (monitor_id,),
        )
        current_row = cursor.fetchone()
        previous_status = current_row["status"] if current_row else "unknown"
        previous_failures = int(current_row["consecutive_failures"] or 0) if current_row else 0
        previous_successes = int(current_row["consecutive_successes"] or 0) if current_row else 0
        down_threshold = _get_int_setting(
            cursor,
            "down_failures_threshold",
            int(DEFAULT_SETTINGS.get("down_failures_threshold", 3)),
        )
        up_threshold = _get_int_setting(
            cursor,
            "up_successes_threshold",
            int(DEFAULT_SETTINGS.get("up_successes_threshold", 1)),
        )
        flapping_window = _get_int_setting(
            cursor,
            "flapping_window_minutes",
            int(DEFAULT_SETTINGS.get("flapping_window_minutes", 15)),
        )
        flapping_threshold = _get_int_setting(
            cursor,
            "flapping_transition_threshold",
            int(DEFAULT_SETTINGS.get("flapping_transition_threshold", 3)),
        )

        raw_status = (new_status or "unknown").strip().lower()
        effective_status = previous_status

        if raw_status == "up":
            consecutive_failures = 0
            consecutive_successes = previous_successes + 1 if previous_status == "down" else 0
            if previous_status == "down" and consecutive_successes < up_threshold:
                effective_status = "down"
            else:
                effective_status = "up"
        elif raw_status == "down":
            consecutive_failures = previous_failures + 1
            consecutive_successes = 0
            if previous_status == "down" or consecutive_failures >= down_threshold:
                effective_status = "down"
            else:
                effective_status = previous_status
        else:
            consecutive_failures = previous_failures
            consecutive_successes = previous_successes
            effective_status = previous_status

        status_changed = previous_status != effective_status and previous_status != "unknown"

        cursor.execute(
            """
            INSERT INTO checks (monitor_id, status, response_time, error_msg, error_category, checked_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (monitor_id, raw_status, response_time, error_msg, error_category, timestamp),
        )

        check_id = cursor.lastrowid
        first_failed_at = None
        confirmed_down_at = None
        first_recovered_at = None
        confirmed_up_at = None

        if status_changed and effective_status == "down":
            first_failed_at = _get_first_recent_status_at(
                cursor, monitor_id, "down", consecutive_failures, timestamp
            )
            confirmed_down_at = timestamp
        elif status_changed and effective_status == "up":
            first_recovered_at = _get_first_recent_status_at(
                cursor, monitor_id, "up", max(1, consecutive_successes), timestamp
            )
            confirmed_up_at = timestamp

        _update_incidents_for_status_change(
            cursor,
            monitor_id=monitor_id,
            previous_status=previous_status,
            new_status=effective_status,
            timestamp=timestamp,
            check_id=check_id,
            error_msg=error_msg,
            error_category=error_category,
            first_failed_at=first_failed_at,
            confirmed_down_at=confirmed_down_at,
            first_recovered_at=first_recovered_at,
            confirmed_up_at=confirmed_up_at,
            confirmation_attempts=consecutive_failures,
            recovery_attempts=consecutive_successes,
        )

        is_flapping = False
        flapping_until = current_row["flapping_until"] if current_row else None
        if status_changed:
            is_flapping, detected_until = _detect_flapping(
                cursor,
                monitor_id,
                timestamp,
                flapping_window,
                flapping_threshold,
            )
            if detected_until:
                flapping_until = detected_until
        elif flapping_until:
            until_dt = _parse_iso(flapping_until)
            now_dt = _parse_iso(timestamp) or datetime.now(timezone.utc)
            is_flapping = bool(until_dt and until_dt > now_dt)

        cursor.execute(
            """
            UPDATE monitors
            SET status = ?,
                last_error = ?,
                last_error_category = ?,
                last_response_time = ?,
                last_checked_at = ?,
                last_change_at = CASE
                    WHEN status <> ? THEN ?
                    ELSE last_change_at
                END,
                consecutive_failures = ?,
                consecutive_successes = ?,
                is_flapping = ?,
                flapping_until = ?,
                updated_at = ?
            WHERE id = ?
            """,
            (
                effective_status,
                error_msg,
                error_category,
                response_time,
                timestamp,
                effective_status,
                timestamp,
                consecutive_failures,
                consecutive_successes,
                int(is_flapping),
                flapping_until,
                timestamp,
                monitor_id,
            ),
        )
        conn.commit()

    return {
        "monitor_id": monitor_id,
        "previous_status": previous_status,
        "status": effective_status,
        "status_changed": status_changed,
        "checked_at": timestamp,
        "check_id": check_id,
        "error_msg": error_msg,
        "error_category": error_category,
        "response_time": response_time,
        "raw_status": raw_status,
        "consecutive_failures": consecutive_failures,
        "consecutive_successes": consecutive_successes,
        "down_failures_threshold": down_threshold,
        "up_successes_threshold": up_threshold,
        "first_failed_at": first_failed_at,
        "confirmed_down_at": confirmed_down_at,
        "first_recovered_at": first_recovered_at,
        "confirmed_up_at": confirmed_up_at,
        "is_flapping": is_flapping,
        "flapping_until": flapping_until,
    }


def _update_incidents_for_status_change(
    cursor: sqlite3.Cursor,
    monitor_id: int,
    previous_status: str,
    new_status: str,
    timestamp: str,
    check_id: int,
    error_msg: Optional[str],
    error_category: Optional[str],
    first_failed_at: Optional[str],
    confirmed_down_at: Optional[str],
    first_recovered_at: Optional[str],
    confirmed_up_at: Optional[str],
    confirmation_attempts: int,
    recovery_attempts: int,
) -> None:
    if previous_status == new_status:
        return
    if previous_status == "unknown":
        return

    now = timestamp

    if new_status == "down" and previous_status != "down":
        cursor.execute(
            """
            SELECT id
            FROM incidents
            WHERE monitor_id = ? AND ended_at IS NULL
            ORDER BY started_at DESC, id DESC
            LIMIT 1
            """,
            (monitor_id,),
        )
        existing = cursor.fetchone()
        if existing is None:
            cursor.execute(
                """
                INSERT INTO incidents (
                    monitor_id, started_at, ended_at, start_check_id, end_check_id,
                    start_error_msg, end_error_msg, first_failed_at, confirmed_down_at,
                    confirmation_attempts, start_error_category, updated_at
                ) VALUES (?, ?, NULL, ?, NULL, ?, NULL, ?, ?, ?, ?, ?)
                """,
                (
                    monitor_id,
                    confirmed_down_at or now,
                    check_id,
                    error_msg,
                    first_failed_at,
                    confirmed_down_at or now,
                    confirmation_attempts,
                    error_category,
                    now,
                ),
            )
        return

    if new_status == "up" and previous_status == "down":
        cursor.execute(
            """
            SELECT id
            FROM incidents
            WHERE monitor_id = ? AND ended_at IS NULL
            ORDER BY started_at DESC, id DESC
            LIMIT 1
            """,
            (monitor_id,),
        )
        open_row = cursor.fetchone()
        if open_row is not None:
            cursor.execute(
                """
                UPDATE incidents
                SET ended_at = ?,
                    end_check_id = ?,
                    end_error_msg = ?,
                    first_recovered_at = ?,
                    confirmed_up_at = ?,
                    recovery_attempts = ?,
                    end_error_category = ?,
                    updated_at = ?
                WHERE id = ?
                """,
                (
                    confirmed_up_at or now,
                    check_id,
                    error_msg,
                    first_recovered_at,
                    confirmed_up_at or now,
                    recovery_attempts,
                    error_category,
                    now,
                    open_row["id"],
                ),
            )


def list_incidents(
    monitor_id: Optional[int] = None,
    status: str = "all",
    since_days: Optional[int] = 7,
    limit: int = 200,
) -> list[dict[str, Any]]:
    status = (status or "all").strip().lower()
    if status not in {"all", "open", "closed"}:
        status = "all"
    since_days = since_days if since_days is None else max(1, since_days)

    where_parts: list[str] = []
    params: list[Any] = []

    if monitor_id is not None:
        where_parts.append("i.monitor_id = ?")
        params.append(monitor_id)

    if status == "open":
        where_parts.append("i.ended_at IS NULL")
    elif status == "closed":
        where_parts.append("i.ended_at IS NOT NULL")

    if since_days is not None:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=since_days)).replace(microsecond=0).isoformat()
        where_parts.append("i.started_at >= ?")
        params.append(cutoff)

    where_sql = " WHERE " + " AND ".join(where_parts) if where_parts else ""

    with closing(get_db()) as conn:
        cursor = conn.cursor()
        cursor.execute(
            f"""
            SELECT
                i.id,
                i.monitor_id,
                i.started_at,
                i.ended_at,
                i.start_error_msg,
                i.end_error_msg,
                i.first_failed_at,
                i.confirmed_down_at,
                i.first_recovered_at,
                i.confirmed_up_at,
                i.confirmation_attempts,
                i.recovery_attempts,
                i.start_error_category,
                i.end_error_category,
                m.name AS monitor_name,
                m.type AS monitor_type,
                m.target AS monitor_target
            FROM incidents i
            JOIN monitors m ON m.id = i.monitor_id
            {where_sql}
            ORDER BY COALESCE(i.ended_at, i.started_at) DESC, i.id DESC
            LIMIT ?
            """,
            (*params, limit),
        )
        return [dict(row) for row in cursor.fetchall()]


def export_backup() -> dict[str, Any]:
    with closing(get_db()) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM monitors ORDER BY id ASC")
        monitors = [dict(row) for row in cursor.fetchall()]
        cursor.execute("SELECT * FROM checks ORDER BY id ASC")
        checks = [dict(row) for row in cursor.fetchall()]
        cursor.execute("SELECT * FROM incidents ORDER BY id ASC")
        incidents = [dict(row) for row in cursor.fetchall()]
        cursor.execute("SELECT key, value FROM settings ORDER BY key ASC")
        settings = {row["key"]: deserialize_setting(row["value"]) for row in cursor.fetchall()}

    return {
        "version": 1,
        "exported_at": utc_now(),
        "monitors": monitors,
        "checks": checks,
        "incidents": incidents,
        "settings": settings,
    }


def import_backup(payload: dict[str, Any]) -> None:
    monitors = payload.get("monitors", [])
    checks = payload.get("checks", [])
    incidents = payload.get("incidents", [])
    settings = payload.get("settings", {})

    with closing(get_db()) as conn:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM checks")
        cursor.execute("DELETE FROM incidents")
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
                    id, name, type, target, http_method, retry_count, interval, timeout,
                    enabled, status, last_error, last_error_category, last_response_time,
                    last_checked_at, last_change_at, consecutive_failures, consecutive_successes,
                    expected_text, forbidden_text, is_flapping, flapping_until, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    monitor.get("id"),
                    monitor["name"],
                    monitor["type"],
                    monitor["target"],
                    str(monitor.get("http_method", "GET")).upper(),
                    int(monitor.get("retry_count", 2) or 0),
                    monitor.get("interval", 60),
                    monitor.get("timeout", 10),
                    int(bool(monitor.get("enabled", True))),
                    monitor.get("status", "unknown"),
                    monitor.get("last_error"),
                    monitor.get("last_error_category"),
                    monitor.get("last_response_time"),
                    monitor.get("last_checked_at"),
                    monitor.get("last_change_at"),
                    int(monitor.get("consecutive_failures", 0) or 0),
                    int(monitor.get("consecutive_successes", 0) or 0),
                    monitor.get("expected_text"),
                    monitor.get("forbidden_text"),
                    int(bool(monitor.get("is_flapping", False))),
                    monitor.get("flapping_until"),
                    monitor.get("created_at", utc_now()),
                    monitor.get("updated_at", utc_now()),
                ),
            )

        for check in checks:
            cursor.execute(
                """
                INSERT INTO checks (id, monitor_id, status, response_time, error_msg, error_category, checked_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    check.get("id"),
                    check["monitor_id"],
                    check["status"],
                    check.get("response_time"),
                    check.get("error_msg"),
                    check.get("error_category"),
                    check.get("checked_at", utc_now()),
                ),
            )

        for incident in incidents:
            cursor.execute(
                """
                INSERT INTO incidents (
                    id, monitor_id, started_at, ended_at, start_check_id, end_check_id,
                    start_error_msg, end_error_msg, first_failed_at, confirmed_down_at,
                    first_recovered_at, confirmed_up_at, confirmation_attempts, recovery_attempts,
                    start_error_category, end_error_category, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    incident.get("id"),
                    incident["monitor_id"],
                    incident.get("started_at", utc_now()),
                    incident.get("ended_at"),
                    incident.get("start_check_id"),
                    incident.get("end_check_id"),
                    incident.get("start_error_msg"),
                    incident.get("end_error_msg"),
                    incident.get("first_failed_at"),
                    incident.get("confirmed_down_at"),
                    incident.get("first_recovered_at"),
                    incident.get("confirmed_up_at"),
                    incident.get("confirmation_attempts"),
                    incident.get("recovery_attempts"),
                    incident.get("start_error_category"),
                    incident.get("end_error_category"),
                    incident.get("created_at", utc_now()),
                    incident.get("updated_at", utc_now()),
                ),
            )

        conn.commit()
