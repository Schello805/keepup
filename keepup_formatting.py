from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


def get_timezone_or_utc(timezone_name: str) -> ZoneInfo:
    try:
        return ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError:
        return ZoneInfo("UTC")


def format_timestamp(timestamp: Optional[str], timezone_name: str) -> Optional[str]:
    if not timestamp:
        return None
    try:
        dt = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    except ValueError:
        return timestamp
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(get_timezone_or_utc(timezone_name)).strftime("%d.%m.%Y %H:%M:%S")


def format_timestamp_without_tz(timestamp: Optional[str], timezone_name: str) -> Optional[str]:
    return format_timestamp(timestamp, timezone_name)


def parse_iso_datetime(timestamp: Optional[str]) -> Optional[datetime]:
    if not timestamp:
        return None
    try:
        dt = datetime.fromisoformat(str(timestamp).replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def days_since(timestamp: Optional[str]) -> Optional[int]:
    dt = parse_iso_datetime(timestamp)
    if dt is None:
        return None
    return max(0, int((datetime.now(timezone.utc) - dt).total_seconds() // 86400))


def format_duration_compact(seconds: Optional[int]) -> Optional[str]:
    if seconds is None:
        return None
    seconds = max(0, int(seconds))
    minutes, _sec = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    days, hours = divmod(hours, 24)
    if days:
        return f"{days} Tage {hours} Std." if hours else f"{days} Tage"
    if hours:
        return f"{hours} Std. {minutes} Min." if minutes else f"{hours} Std."
    if minutes:
        return f"{minutes} Min."
    return f"{seconds} Sek."


def format_duration_short(seconds: Optional[int]) -> Optional[str]:
    if seconds is None:
        return None
    seconds = max(0, int(seconds))
    minutes, sec = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    days, hours = divmod(hours, 24)
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {minutes}m"
    if minutes:
        return f"{minutes}m {sec}s"
    return f"{sec}s"


def outage_hours_between(success_at: Optional[str], down_at: Optional[str]) -> Optional[str]:
    success_dt = parse_iso_datetime(success_at)
    down_dt = parse_iso_datetime(down_at)
    if success_dt is None or down_dt is None:
        return None
    delta_seconds = int(round((down_dt - success_dt).total_seconds()))
    if delta_seconds <= 0:
        return None
    return format_duration_compact(delta_seconds)


def format_bytes_compact(num_bytes: Optional[float]) -> str:
    if num_bytes is None:
        return "-"
    value = float(num_bytes)
    units = ["B", "KB", "MB", "GB", "TB"]
    for unit in units:
        if value < 1024 or unit == units[-1]:
            return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} {unit}"
        value /= 1024.0
    return "-"
