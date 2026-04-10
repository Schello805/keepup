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
    "down_failures_threshold": 3,
    "retention_days": 7,
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

            CREATE TABLE IF NOT EXISTS incidents (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                monitor_id INTEGER NOT NULL,
                started_at TEXT NOT NULL,
                ended_at TEXT,
                start_check_id INTEGER,
                end_check_id INTEGER,
                start_error_msg TEXT,
                end_error_msg TEXT,
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
        "updated_at": "TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP",
    }
    for column_name, column_type in required_columns.items():
        if column_name not in existing_columns:
            cursor.execute(f"ALTER TABLE monitors ADD COLUMN {column_name} {column_type}")


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


def list_monitors() -> list[dict[str, Any]]:
    with closing(get_db()) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM monitors ORDER BY id DESC")
        monitors = [dict(row) for row in cursor.fetchall()]
        if not monitors:
            return []

        monitor_ids = [monitor["id"] for monitor in monitors]
        placeholders = ", ".join("?" for _ in monitor_ids)

        now_dt = datetime.now(timezone.utc).replace(microsecond=0)
        monitor_map = {monitor["id"]: monitor for monitor in monitors}

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
            (*monitor_ids, HISTORY_LIMIT),
        )
        recent_checks_by_monitor: dict[int, list[sqlite3.Row]] = {monitor_id: [] for monitor_id in monitor_ids}
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
            tuple(monitor_ids),
        )
        last_success_map = {
            row["monitor_id"]: row["last_success_at"]
            for row in cursor.fetchall()
        }

        cursor.execute(
            f"""
            SELECT monitor_id, started_at, ended_at
            FROM incidents
            WHERE monitor_id IN ({placeholders})
              AND started_at <= ?
            ORDER BY monitor_id ASC, started_at ASC, id ASC
            """,
            (*monitor_ids, now_dt.isoformat()),
        )
        incident_rows_by_monitor: dict[int, list[sqlite3.Row]] = {monitor_id: [] for monitor_id in monitor_ids}
        for row in cursor.fetchall():
            incident_rows_by_monitor[row["monitor_id"]].append(row)

        for monitor in monitors:
            rows = list(reversed(recent_checks_by_monitor.get(monitor["id"], [])))
            monitor["history"] = [r["status"] for r in rows]

            monitor["chart_data_json"] = json.dumps([
                {"x": r["checked_at"], "y": r["response_time"]}
                for r in rows
            ])

            recent_statuses = [row["status"] for row in recent_checks_by_monitor.get(monitor["id"], [])]
            total = len(recent_statuses)
            up_count = sum(1 for status in recent_statuses if status == "up")
            monitor["uptime_percentage"] = round((up_count / total) * 100, 1) if total else None

            monitor["last_success_at"] = last_success_map.get(monitor["id"])
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
    enabled: bool = True,
) -> int:
    now = utc_now()
    with closing(get_db()) as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            INSERT INTO monitors (
                name, type, target, http_method, retry_count, interval, timeout, enabled, status, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'unknown', ?, ?)
            """,
            (
                name.strip(),
                monitor_type,
                target.strip(),
                http_method.strip().upper(),
                max(0, int(retry_count)),
                interval,
                timeout,
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
            SELECT id, status, response_time, error_msg, checked_at
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
            SELECT monitor_id, id, status, response_time, error_msg, checked_at
            FROM (
                SELECT
                    monitor_id,
                    id,
                    status,
                    response_time,
                    error_msg,
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
        cursor.execute("SELECT status, consecutive_failures FROM monitors WHERE id = ?", (monitor_id,))
        current_row = cursor.fetchone()
        previous_status = current_row["status"] if current_row else "unknown"
        previous_failures = int(current_row["consecutive_failures"] or 0) if current_row else 0
        threshold = _get_int_setting(
            cursor,
            "down_failures_threshold",
            int(DEFAULT_SETTINGS.get("down_failures_threshold", 3)),
        )

        raw_status = (new_status or "unknown").strip().lower()
        effective_status = previous_status

        if raw_status == "up":
            effective_status = "up"
            consecutive_failures = 0
        elif raw_status == "down":
            consecutive_failures = previous_failures + 1
            if previous_status == "down" or consecutive_failures >= threshold:
                effective_status = "down"
            else:
                effective_status = previous_status
        else:
            consecutive_failures = previous_failures
            effective_status = previous_status

        status_changed = previous_status != effective_status and previous_status != "unknown"

        cursor.execute(
            """
            INSERT INTO checks (monitor_id, status, response_time, error_msg, checked_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (monitor_id, raw_status, response_time, error_msg, timestamp),
        )

        check_id = cursor.lastrowid

        _update_incidents_for_status_change(
            cursor,
            monitor_id=monitor_id,
            previous_status=previous_status,
            new_status=effective_status,
            timestamp=timestamp,
            check_id=check_id,
            error_msg=error_msg,
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
                consecutive_failures = ?,
                updated_at = ?
            WHERE id = ?
            """,
            (
                effective_status,
                error_msg,
                response_time,
                timestamp,
                effective_status,
                timestamp,
                consecutive_failures,
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
        "response_time": response_time,
        "raw_status": raw_status,
        "consecutive_failures": consecutive_failures,
        "down_failures_threshold": threshold,
    }


def _update_incidents_for_status_change(
    cursor: sqlite3.Cursor,
    monitor_id: int,
    previous_status: str,
    new_status: str,
    timestamp: str,
    check_id: int,
    error_msg: Optional[str],
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
                    start_error_msg, end_error_msg, updated_at
                ) VALUES (?, ?, NULL, ?, NULL, ?, NULL, ?)
                """,
                (monitor_id, now, check_id, error_msg, now),
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
                    updated_at = ?
                WHERE id = ?
                """,
                (now, check_id, error_msg, now, open_row["id"]),
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
                    id, name, type, target, http_method, retry_count, interval, timeout, enabled, status, last_error,
                    last_response_time, last_checked_at, last_change_at, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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

        for incident in incidents:
            cursor.execute(
                """
                INSERT INTO incidents (
                    id, monitor_id, started_at, ended_at, start_check_id, end_check_id,
                    start_error_msg, end_error_msg, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                    incident.get("created_at", utc_now()),
                    incident.get("updated_at", utc_now()),
                ),
            )

        conn.commit()
