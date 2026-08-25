from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import os
import subprocess
import threading
import time
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlencode, urlparse
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import uvicorn
from apscheduler.events import EVENT_JOB_EXECUTED
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
import httpx

from database import (
    cleanup_old_checks,
    export_backup,
    get_db,
    get_monitor,
    get_monitor_group_summary,
    get_monitor_summary,
    get_recent_logs_for_monitors,
    list_incidents,
    list_monitor_incident_feed_options,
    list_monitor_options,
    list_status_wall_monitors,
    get_settings,
    import_backup,
    init_db,
    list_monitors,
    update_settings,
)
from monitor import (
    execute_monitor_check,
    format_notification_error,
    init_monitor_runtime,
    remove_monitor_job,
    reschedule_monitor_job,
    reschedule_monitor_jobs,
    run_all_checks_once,
    send_test_email_notification,
    send_test_ntfy_rich_notification,
    send_test_telegram_notification,
    shutdown_monitor_runtime,
)

from keepup_version import __version__
from keepup_formatting import (
    days_since,
    format_duration_compact,
    format_duration_short,
    format_timestamp,
    format_timestamp_without_tz,
    get_timezone_or_utc,
    outage_hours_between,
    parse_iso_datetime,
)
from keepup_system import build_system_metrics
from keepup_observability import RequestTimingMiddleware
from keepup_cache import DashboardCacheStore
from keepup_repository import MonitorRepository
from keepup_monitor_service import MonitorService
from keepup_routes_system import configure_system_routes, health, readiness, router as system_router


BASE_DIR = Path(__file__).resolve().parent
scheduler = AsyncIOScheduler()
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger("keepup")
UPDATE_STATUS_TTL_SECONDS = 60
UPDATE_REMOTE_TIMEOUT_SECONDS = 2.5
WEATHER_CACHE_TTL_SECONDS = 15 * 60
WEATHER_ERROR_CACHE_TTL_SECONDS = 60
WEATHER_REQUEST_TIMEOUT_SECONDS = 5.0
WEATHER_GEOCODING_URL = "https://geocoding-api.open-meteo.com/v1/search"
WEATHER_FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
_update_status_cache: dict[str, Any] = {"expires_at": 0.0, "payload": None}
APP_VERSION_TTL_SECONDS = 60
DASHBOARD_CARDS_CACHE_TTL_SECONDS = 5
MONITOR_DETAIL_CACHE_TTL_SECONDS = 120
MAX_IMPORT_BYTES = max(1, int(os.environ.get("KEEPUP_MAX_IMPORT_MB", "25"))) * 1024 * 1024
IMPORT_READ_CHUNK_BYTES = 1024 * 1024
_app_version_cache: dict[str, Any] = {"expires_at": 0.0, "value": None}
_dashboard_cache = DashboardCacheStore()
monitor_repository = MonitorRepository()
# Compatibility aliases keep extensions and existing tests stable during the refactor.
_dashboard_cards_cache = _dashboard_cache.cards
_dashboard_cards_cache_lock = _dashboard_cache.cards_lock
_dashboard_cards_refresh_task: Optional[asyncio.Task] = None
_dashboard_snapshot_cache = _dashboard_cache.snapshot
_dashboard_snapshot_cache_lock = _dashboard_cache.snapshot_lock
_dashboard_snapshot_refresh_task: Optional[asyncio.Task] = None
_status_wall_cache = _dashboard_cache.status_wall
_status_wall_cache_lock = _dashboard_cache.status_wall_lock
_status_wall_refresh_task: Optional[asyncio.Task] = None
_monitor_detail_cache: dict[int, dict[str, Any]] = {}
_monitor_detail_cache_lock = threading.Lock()
_monitor_detail_cache_generation = 0
_changelog_cache: dict[str, Any] = {"expires_at": 0.0, "items": None}
_weather_cache: dict[str, Any] = {"location": "", "expires_at": 0.0, "payload": None}
_weather_cache_lock = asyncio.Lock()
APP_TIMEZONE_OPTIONS = [
    "UTC",
    "Europe/Berlin",
    "Europe/Vienna",
    "Europe/Zurich",
    "Europe/London",
    "America/New_York",
    "America/Chicago",
    "America/Los_Angeles",
    "Asia/Tokyo",
    "Asia/Singapore",
    "Australia/Sydney",
]


def flash_redirect(path: str, message: str, tone: str = "success") -> RedirectResponse:
    query = urlencode({"toast": message, "tone": tone})
    return RedirectResponse(url=f"{path}?{query}", status_code=303)


def get_toast(request: Request) -> Optional[dict]:
    message = request.query_params.get("toast", "").strip()
    if not message:
        return None
    tone = request.query_params.get("tone", "success").strip() or "success"
    if tone not in {"success", "info", "warning", "error"}:
        tone = "success"
    return {"message": message, "tone": tone}


def get_incident_burst_bucket(timestamp: Optional[str]) -> Optional[str]:
    if not timestamp:
        return None
    try:
        dt = datetime.fromisoformat(str(timestamp).replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).replace(second=0, microsecond=0).isoformat()


def build_update_overlay_metrics() -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    try:
        conn = get_db()
        try:
            monitor_rows = conn.execute("SELECT id, created_at FROM monitors ORDER BY id ASC").fetchall()
            check_rows = conn.execute(
                "SELECT monitor_id, MIN(checked_at) AS first_checked_at FROM checks GROUP BY monitor_id"
            ).fetchall()
            incident_rows = conn.execute(
                "SELECT monitor_id, started_at, ended_at FROM incidents ORDER BY started_at ASC, id ASC"
            ).fetchall()
        finally:
            conn.close()
    except Exception:
        return {
            "monitor_count": 0,
            "uptime_pct": None,
            "downtime_human": "-",
            "mttr_human": "-",
        }

    first_seen_by_monitor: dict[int, datetime] = {}
    for row in check_rows:
        first_checked_at = parse_iso_datetime(row["first_checked_at"])
        if first_checked_at is not None:
            first_seen_by_monitor[int(row["monitor_id"])] = first_checked_at

    for row in incident_rows:
        started_at = parse_iso_datetime(row["started_at"])
        if started_at is None:
            continue
        monitor_id = int(row["monitor_id"])
        existing = first_seen_by_monitor.get(monitor_id)
        if existing is None or started_at < existing:
            first_seen_by_monitor[monitor_id] = started_at

    total_monitored_seconds = 0.0
    baseline_by_monitor: dict[int, datetime] = {}
    for row in monitor_rows:
        monitor_id = int(row["id"])
        created_at = parse_iso_datetime(row["created_at"])
        first_seen_at = first_seen_by_monitor.get(monitor_id)
        if created_at and first_seen_at:
            baseline = min(created_at, first_seen_at)
        else:
            baseline = created_at or first_seen_at or now
        baseline_by_monitor[monitor_id] = baseline
        total_monitored_seconds += max(0.0, (now - baseline).total_seconds())

    downtime_seconds = 0.0
    mttr_durations: list[float] = []
    for row in incident_rows:
        monitor_id = int(row["monitor_id"])
        baseline = baseline_by_monitor.get(monitor_id)
        if not baseline:
            continue
        started = parse_iso_datetime(row["started_at"]) or baseline
        ended = parse_iso_datetime(row["ended_at"]) or now
        overlap_start = max(started, baseline)
        overlap_end = min(ended, now)
        if overlap_end <= overlap_start:
            continue
        downtime_seconds += (overlap_end - overlap_start).total_seconds()
        if row["ended_at"] is not None and ended > started:
            mttr_durations.append((ended - started).total_seconds())

    uptime_pct = None
    if total_monitored_seconds > 0:
        uptime_pct = round(max(0.0, (total_monitored_seconds - downtime_seconds) / total_monitored_seconds) * 100.0, 3)
    mttr_seconds = (sum(mttr_durations) / len(mttr_durations)) if mttr_durations else None

    return {
        "monitor_count": len(monitor_rows),
        "uptime_pct": uptime_pct,
        "downtime_human": format_duration_short(int(round(downtime_seconds))) or "0s",
        "mttr_human": format_duration_short(int(round(mttr_seconds))) if mttr_seconds is not None else "-",
    }

def normalize_timezone(timezone_name: str) -> str:
    timezone_name = timezone_name.strip() or "UTC"
    try:
        ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError as exc:
        raise ValueError("Unbekannte Zeitzone. Bitte z. B. Europe/Berlin oder UTC verwenden.") from exc
    return timezone_name


def normalize_base_url(base_url: str, label: str = "KeepUp URL") -> str:
    base_url = base_url.strip()
    if not base_url:
        return ""
    if not (base_url.startswith("http://") or base_url.startswith("https://")):
        raise ValueError(f"{label} muss mit http:// oder https:// beginnen.")
    return base_url.rstrip("/")


def normalize_weather_location(location: str) -> str:
    location = " ".join(str(location or "").strip().split())
    if location and len(location) < 2:
        raise ValueError("Wetter-Ort muss mindestens 2 Zeichen lang sein.")
    if len(location) > 120:
        raise ValueError("Wetter-Ort darf maximal 120 Zeichen lang sein.")
    return location


def weather_code_details(code: Any) -> tuple[str, str]:
    try:
        code = int(code)
    except (TypeError, ValueError):
        return "Wetterlage unbekannt", "?"
    if code == 0:
        return "Klar", "☀"
    if code in {1, 2}:
        return "Leicht bewölkt", "🌤"
    if code == 3:
        return "Bedeckt", "☁"
    if code in {45, 48}:
        return "Nebel", "🌫"
    if code in {51, 53, 55, 56, 57}:
        return "Nieselregen", "🌦"
    if code in {61, 63, 65, 66, 67, 80, 81, 82}:
        return "Regen", "🌧"
    if code in {71, 73, 75, 77, 85, 86}:
        return "Schnee", "🌨"
    if code in {95, 96, 99}:
        return "Gewitter", "⛈"
    return "Wechselhaft", "🌥"


def select_weather_location(location: str, results: list[dict[str, Any]]) -> Optional[dict[str, Any]]:
    if not results:
        return None
    parts = [part.strip().casefold() for part in location.split(",") if part.strip()]
    requested_name = parts[0] if parts else location.casefold()
    qualifiers = parts[1:]

    def score(place: dict[str, Any]) -> int:
        name = str(place.get("name") or "").casefold()
        searchable = " ".join(
            str(place.get(key) or "").casefold()
            for key in ("name", "admin1", "admin2", "admin3", "admin4", "country", "country_code")
        )
        value = 100 if name == requested_name else (40 if requested_name in name else 0)
        for qualifier in qualifiers:
            value += 30 if qualifier in searchable else -30
        return value

    return max(results, key=score)


def build_weather_payload(location: str, geocoding: dict[str, Any], forecast: dict[str, Any]) -> dict[str, Any]:
    results = geocoding.get("results") or []
    place = select_weather_location(location, results)
    if not place:
        raise ValueError("Wetter-Ort wurde nicht gefunden.")
    daily = forecast.get("daily") or {}
    current = forecast.get("current") or {}
    daily_codes = daily.get("weather_code") or []
    code = current.get("weather_code")
    if code is None and daily_codes:
        code = daily_codes[0]
    condition, icon = weather_code_details(code)

    def first_value(key: str) -> Any:
        values = daily.get(key) or []
        return values[0] if values else None

    display_name = str(place.get("name") or location)
    region = str(place.get("admin1") or place.get("country_code") or "")
    if region and region.casefold() != display_name.casefold():
        display_name = f"{display_name}, {region}"
    return {
        "ok": True,
        "location": display_name,
        "condition": condition,
        "icon": icon,
        "temperature": current.get("temperature_2m"),
        "temperature_min": first_value("temperature_2m_min"),
        "temperature_max": first_value("temperature_2m_max"),
        "precipitation_probability": first_value("precipitation_probability_max"),
    }


async def fetch_weather_today(location: str) -> dict[str, Any]:
    headers = {"User-Agent": f"KeepUp/{__version__}"}
    search_name = location.split(",", 1)[0].strip()
    async with httpx.AsyncClient(timeout=WEATHER_REQUEST_TIMEOUT_SECONDS, headers=headers) as client:
        geocoding_response = await client.get(
            WEATHER_GEOCODING_URL,
            params={"name": search_name, "count": 10, "language": "de", "format": "json"},
        )
        geocoding_response.raise_for_status()
        geocoding = geocoding_response.json()
        results = geocoding.get("results") or []
        if not results:
            raise ValueError("Wetter-Ort wurde nicht gefunden.")
        place = select_weather_location(location, results)
        if not place:
            raise ValueError("Wetter-Ort wurde nicht gefunden.")
        forecast_response = await client.get(
            WEATHER_FORECAST_URL,
            params={
                "latitude": place["latitude"],
                "longitude": place["longitude"],
                "current": "temperature_2m,weather_code",
                "daily": "weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max",
                "forecast_days": 1,
                "timezone": "auto",
            },
        )
        forecast_response.raise_for_status()
        return build_weather_payload(location, geocoding, forecast_response.json())


async def get_cached_weather_today(location: str) -> dict[str, Any]:
    now = time.time()
    if (
        _weather_cache.get("location") == location
        and _weather_cache.get("payload") is not None
        and now < float(_weather_cache.get("expires_at") or 0.0)
    ):
        return dict(_weather_cache["payload"])
    async with _weather_cache_lock:
        now = time.time()
        if (
            _weather_cache.get("location") == location
            and _weather_cache.get("payload") is not None
            and now < float(_weather_cache.get("expires_at") or 0.0)
        ):
            return dict(_weather_cache["payload"])
        try:
            payload = await fetch_weather_today(location)
            ttl = WEATHER_CACHE_TTL_SECONDS
        except Exception as exc:
            logger.warning("weather_fetch_failed location=%s error=%s", location, exc)
            payload = {"ok": False, "location": location, "message": "Wetter ist vorübergehend nicht verfügbar."}
            ttl = WEATHER_ERROR_CACHE_TTL_SECONDS
        _weather_cache.update(location=location, expires_at=now + ttl, payload=payload)
        return dict(payload)


def build_notification_settings_payload(
    keepup_base_url: str,
    app_timezone: str,
    default_monitor_interval: int,
    global_monitor_interval_override: int,
    down_failures_threshold: int,
    up_successes_threshold: int,
    retention_days: int,
    flapping_window_minutes: int,
    flapping_transition_threshold: int,
    notification_batch_window_seconds: int,
    scheduler_jitter_seconds: int,
    telegram_enabled: Optional[str],
    telegram_bot_token: str,
    telegram_chat_id: str,
    ntfy_enabled: Optional[str],
    ntfy_server_url: str,
    ntfy_topic: str,
    ntfy_token: str,
    ntfy_username: str,
    ntfy_password: str,
    ntfy_priority: int,
    smtp_enabled: Optional[str],
    smtp_host: str,
    smtp_port: int,
    smtp_username: str,
    smtp_password: str,
    smtp_from_email: str,
    smtp_to_email: str,
    smtp_use_tls: Optional[str],
    smtp_use_ssl: Optional[str],
    weather_location: Optional[str] = None,
) -> dict:
    existing_settings = get_settings()
    default_monitor_interval = int(default_monitor_interval)
    global_monitor_interval_override = int(global_monitor_interval_override)
    down_failures_threshold = int(down_failures_threshold)
    up_successes_threshold = int(up_successes_threshold)
    retention_days = int(retention_days)
    flapping_window_minutes = int(flapping_window_minutes)
    flapping_transition_threshold = int(flapping_transition_threshold)
    notification_batch_window_seconds = int(notification_batch_window_seconds)
    scheduler_jitter_seconds = int(scheduler_jitter_seconds)
    if default_monitor_interval < 10:
        raise ValueError("Standard-Intervall muss mindestens 10 Sekunden sein.")
    if global_monitor_interval_override not in {0} and global_monitor_interval_override < 10:
        raise ValueError("Globales Override-Intervall muss 0 oder mindestens 10 Sekunden sein.")
    if down_failures_threshold < 1:
        raise ValueError("Fehlschlag-Schwelle muss mindestens 1 sein.")
    if up_successes_threshold < 1:
        raise ValueError("Recovery-Schwelle muss mindestens 1 sein.")
    if retention_days < 1:
        raise ValueError("Aufbewahrungszeit muss mindestens 1 Tag sein.")
    if flapping_window_minutes < 1:
        raise ValueError("Flapping-Fenster muss mindestens 1 Minute sein.")
    if flapping_transition_threshold < 2:
        raise ValueError("Flapping-Schwelle muss mindestens 2 Statuswechsel sein.")
    if notification_batch_window_seconds < 0:
        raise ValueError("Sammelmeldungs-Fenster darf nicht negativ sein.")
    if scheduler_jitter_seconds < 0:
        raise ValueError("Scheduler-Jitter darf nicht negativ sein.")
    ntfy_priority = int(ntfy_priority)
    if ntfy_priority < 1 or ntfy_priority > 5:
        raise ValueError("ntfy Priorität muss zwischen 1 und 5 liegen.")
    return {
        "keepup_base_url": normalize_base_url(keepup_base_url),
        "app_timezone": normalize_timezone(app_timezone),
        "weather_location": normalize_weather_location(weather_location) if weather_location is not None else str(existing_settings.get("weather_location") or ""),
        "default_monitor_interval": default_monitor_interval,
        "global_monitor_interval_override": global_monitor_interval_override,
        "down_failures_threshold": down_failures_threshold,
        "up_successes_threshold": up_successes_threshold,
        "retention_days": retention_days,
        "flapping_window_minutes": flapping_window_minutes,
        "flapping_transition_threshold": flapping_transition_threshold,
        "notification_batch_window_seconds": notification_batch_window_seconds,
        "scheduler_jitter_seconds": scheduler_jitter_seconds,
        "telegram_enabled": telegram_enabled == "on",
        "telegram_bot_token": telegram_bot_token.strip() or str(existing_settings.get("telegram_bot_token") or ""),
        "telegram_chat_id": telegram_chat_id.strip(),
        "ntfy_enabled": ntfy_enabled == "on",
        "ntfy_server_url": normalize_base_url(ntfy_server_url, "ntfy Server-URL"),
        "ntfy_topic": ntfy_topic.strip().strip("/"),
        "ntfy_token": ntfy_token.strip() or str(existing_settings.get("ntfy_token") or ""),
        "ntfy_username": ntfy_username.strip(),
        "ntfy_password": ntfy_password or str(existing_settings.get("ntfy_password") or ""),
        "ntfy_priority": ntfy_priority,
        "smtp_enabled": smtp_enabled == "on",
        "smtp_host": smtp_host.strip(),
        "smtp_port": smtp_port,
        "smtp_username": smtp_username.strip(),
        "smtp_password": smtp_password or str(existing_settings.get("smtp_password") or ""),
        "smtp_from_email": smtp_from_email.strip(),
        "smtp_to_email": smtp_to_email.strip(),
        "smtp_use_tls": smtp_use_tls == "on",
        "smtp_use_ssl": smtp_use_ssl == "on",
    }


async def read_limited_upload(file: UploadFile, max_bytes: int = MAX_IMPORT_BYTES) -> bytes:
    content = bytearray()
    while True:
        chunk = await file.read(IMPORT_READ_CHUNK_BYTES)
        if not chunk:
            break
        content.extend(chunk)
        if len(content) > max_bytes:
            raise ValueError(f"Backup-Datei ist zu groß. Maximal erlaubt sind {max_bytes // 1024 // 1024} MB.")
    return bytes(content)


def build_dashboard_context(request: Request) -> dict:
    cards_payload = build_dashboard_cards_payload()
    monitors = cards_payload["monitors"]
    settings = cards_payload["settings"]

    down_count = sum(1 for monitor in monitors if monitor.get("enabled", 1) and monitor["status"] == "down")
    up_count = sum(1 for monitor in monitors if monitor.get("enabled", 1) and monitor["status"] == "up")
    unknown_count = sum(1 for monitor in monitors if monitor.get("enabled", 1) and monitor["status"] == "unknown")
    paused_count = sum(1 for monitor in monitors if not monitor.get("enabled", 1))
    categories = build_monitor_category_summary(monitors)
    overall_status = "All systems operational" if down_count == 0 else f"{down_count} issue(s) detected"
    overall_tone = "ok" if down_count == 0 else "problem"
    last_updated_at = format_timestamp(
        datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        settings.get("app_timezone", "UTC"),
    )

    return {
        "request": request,
        "monitors": monitors,
        "settings": settings,
        "app_version": get_app_version_display(),
        "changelog_preview": get_changelog_items(limit=3),
        "active_page": "dashboard",
        "toast": get_toast(request),
        "summary": {
            "total": len(monitors),
            "up": up_count,
            "down": down_count,
            "unknown": unknown_count,
            "paused": paused_count,
            "categories": categories,
            "overall_status": overall_status,
            "overall_tone": overall_tone,
            "last_updated_at": last_updated_at,
        },
    }


def get_dashboard_snapshot_version() -> int:
    return _dashboard_cache.version()


def advance_dashboard_snapshot_version() -> int:
    return _dashboard_cache.advance_version()


def build_dashboard_snapshot(request: Request) -> dict[str, Any]:
    """Render counters and cards from one monitor query and one snapshot version."""
    version = get_dashboard_snapshot_version()
    now = time.time()
    with _dashboard_snapshot_cache_lock:
        cached = _dashboard_snapshot_cache.get("payload")
        if (
            cached
            and int(_dashboard_snapshot_cache.get("version") or 0) == version
            and now < float(_dashboard_snapshot_cache.get("expires_at") or 0.0)
        ):
            return cached

    context = build_dashboard_context(request)
    payload = {
        "version": version,
        "summary": context["summary"],
        "top_html": render_template_content("index.html", {**context, "partial": "top"}),
        "cards_html": render_template_content("index.html", {**context, "partial": "cards"}),
        "wall": build_status_wall_payload(context["monitors"]),
    }
    if version == get_dashboard_snapshot_version():
        with _dashboard_snapshot_cache_lock:
            _dashboard_snapshot_cache.update(
                version=version,
                expires_at=time.time() + DASHBOARD_CARDS_CACHE_TTL_SECONDS,
                payload=payload,
            )
        with _status_wall_cache_lock:
            _status_wall_cache.update(
                version=version,
                expires_at=time.time() + DASHBOARD_CARDS_CACHE_TTL_SECONDS,
                payload=payload["wall"],
            )
    return payload


def peek_dashboard_snapshot() -> Optional[dict[str, Any]]:
    with _dashboard_snapshot_cache_lock:
        payload = _dashboard_snapshot_cache.get("payload")
        return payload if isinstance(payload, dict) else None


async def ensure_dashboard_snapshot_refresh(request: Request) -> None:
    global _dashboard_snapshot_refresh_task
    if _dashboard_snapshot_refresh_task is not None and not _dashboard_snapshot_refresh_task.done():
        return

    async def refresh() -> None:
        try:
            await asyncio.to_thread(build_dashboard_snapshot, request)
        except Exception:
            logger.exception("dashboard_snapshot_refresh_failed")

    _dashboard_snapshot_refresh_task = asyncio.create_task(refresh())


def build_status_wall_payload(monitors: Optional[list[dict[str, Any]]] = None) -> dict[str, Any]:
    if monitors is None:
        monitors = list_status_wall_monitors()
    settings = get_settings()
    app_timezone = settings.get("app_timezone", "UTC")
    global_interval_override = max(0, int(settings.get("global_monitor_interval_override") or 0))
    for monitor in monitors:
        monitor["display_status"] = "paused" if not monitor.get("enabled", 1) else monitor["status"]
        monitor["effective_interval"] = global_interval_override or int(monitor.get("interval") or 60)
        last_checked_at = monitor.get("last_checked_at")
        if last_checked_at and "T" in str(last_checked_at):
            monitor["last_checked_at"] = format_timestamp_without_tz(monitor.get("last_checked_at"), app_timezone)
    summary = {
        "total": len(monitors),
        "up": sum(1 for monitor in monitors if monitor.get("enabled", 1) and monitor["status"] == "up"),
        "down": sum(1 for monitor in monitors if monitor.get("enabled", 1) and monitor["status"] == "down"),
        "unknown": sum(1 for monitor in monitors if monitor.get("enabled", 1) and monitor["status"] == "unknown"),
        "paused": sum(1 for monitor in monitors if not monitor.get("enabled", 1)),
    }
    payload = {
        "version": get_dashboard_snapshot_version(),
        "summary": summary,
        "monitors": [
            {
                "id": monitor["id"],
                "name": monitor["name"],
                "target": monitor["target"],
                "category": str(monitor.get("category") or "Ohne Kategorie"),
                "categories": monitor.get("categories") or [],
                "type": (
                    "PING + HTTP" if monitor.get("ping_mode") == "and" else "PING ODER HTTP"
                ) if monitor.get("ping_enabled") else str(monitor.get("type") or "").upper(),
                "status": monitor["display_status"],
                "response_time": monitor.get("last_response_time"),
                "last_checked_at": monitor.get("last_checked_at"),
                "interval": monitor.get("effective_interval"),
                "uptime_30d": monitor.get("uptime_30d"),
                "history": monitor.get("history") or [],
            }
            for monitor in monitors
        ],
    }
    if get_dashboard_snapshot_version() == payload["version"]:
        with _status_wall_cache_lock:
            _status_wall_cache.update(
                version=payload["version"],
                expires_at=time.time() + DASHBOARD_CARDS_CACHE_TTL_SECONDS,
                payload=payload,
            )
    return payload


def peek_status_wall_payload() -> Optional[dict[str, Any]]:
    with _status_wall_cache_lock:
        payload = _status_wall_cache.get("payload")
        return payload if isinstance(payload, dict) else None


async def ensure_status_wall_refresh() -> None:
    global _status_wall_refresh_task
    if _status_wall_refresh_task is not None and not _status_wall_refresh_task.done():
        return

    async def refresh() -> None:
        try:
            await asyncio.to_thread(build_status_wall_payload)
        except Exception:
            logger.exception("status_wall_refresh_failed")

    _status_wall_refresh_task = asyncio.create_task(refresh())


def build_dashboard_cards_payload() -> dict[str, Any]:
    monitors = list_monitors(include_heavy_details=False)
    settings = get_settings()
    app_timezone = settings.get("app_timezone", "UTC")
    global_interval_override = max(0, int(settings.get("global_monitor_interval_override") or 0))
    for monitor in monitors:
        last_success_raw = monitor.get("last_success_at")
        last_down_raw = monitor.get("last_down_at")
        monitor["display_status"] = "paused" if not monitor.get("enabled", 1) else monitor["status"]
        monitor["effective_interval"] = global_interval_override or int(monitor.get("interval") or 60)
        monitor["last_checked_at"] = format_timestamp(monitor.get("last_checked_at"), app_timezone)
        monitor["last_change_at"] = format_timestamp(monitor.get("last_change_at"), app_timezone)
        monitor["last_success_at"] = format_timestamp_without_tz(last_success_raw, app_timezone)
        monitor["last_down_at"] = format_timestamp_without_tz(last_down_raw, app_timezone)
        monitor["outage_hours"] = outage_hours_between(last_success_raw, last_down_raw)
        monitor["uptime_since_days"] = days_since(monitor.get("created_at"))
    return {
        "monitors": monitors,
        "settings": settings,
    }


def build_monitor_category_summary(monitors: list[dict[str, Any]]) -> list[dict[str, Any]]:
    categories: dict[str, dict[str, Any]] = {}
    for monitor in monitors:
        labels = monitor.get("categories") or []
        if not isinstance(labels, list) or not labels:
            labels = [str(monitor.get("category") or "").strip()] if monitor.get("category") else [""]
        for raw_value in labels:
            raw_label = str(raw_value or "").strip()
            key = raw_label.lower() if raw_label else "__none__"
            label = raw_label or "Ohne Kategorie"
            item = categories.setdefault(
                key,
                {"key": key, "label": label, "total": 0, "up": 0, "down": 0, "unknown": 0, "paused": 0},
            )
            item["total"] += 1
            if not monitor.get("enabled", 1):
                item["paused"] += 1
            else:
                status = str(monitor.get("status") or "unknown")
                if status in {"up", "down", "unknown"}:
                    item[status] += 1

    return sorted(
        categories.values(),
        key=lambda item: (-int(item["down"]), -int(item["unknown"]), str(item["label"]).casefold()),
    )


def build_monitor_detail_context(request: Request, monitor_id: int) -> Optional[dict[str, Any]]:
    settings = get_settings()
    app_timezone = settings.get("app_timezone", "UTC")
    global_interval_override = max(0, int(settings.get("global_monitor_interval_override") or 0))
    monitors = list_monitors(
        monitor_ids=[monitor_id],
        include_heavy_details=True,
        include_uptime_rollups=False,
    )
    if not monitors:
        return None

    monitor = monitors[0]
    monitor["effective_interval"] = global_interval_override or int(monitor.get("interval") or 60)
    logs_by_monitor = get_recent_logs_for_monitors([monitor_id])
    monitor["logs"] = logs_by_monitor.get(monitor_id, [])
    monitor["display_status"] = "paused" if not monitor.get("enabled", 1) else monitor["status"]
    monitor["last_checked_at"] = format_timestamp(monitor.get("last_checked_at"), app_timezone)
    monitor["last_change_at"] = format_timestamp(monitor.get("last_change_at"), app_timezone)
    monitor["last_success_at"] = format_timestamp(monitor.get("last_success_at"), app_timezone)
    monitor["last_down_at"] = format_timestamp(monitor.get("last_down_at"), app_timezone)
    for log in monitor["logs"]:
        log["checked_at"] = format_timestamp(log.get("checked_at"), app_timezone)

    return {
        "request": request,
        "settings": settings,
        "monitor": monitor,
        "active_page": "dashboard",
    }


def invalidate_monitor_detail_cache(monitor_id: Optional[int] = None) -> None:
    global _monitor_detail_cache_generation
    with _monitor_detail_cache_lock:
        _monitor_detail_cache_generation += 1
        if monitor_id is None:
            _monitor_detail_cache.clear()
        else:
            _monitor_detail_cache.pop(int(monitor_id), None)


def get_monitor_detail_html(request: Request, monitor_id: int, force_refresh: bool = False) -> Optional[str]:
    monitor_id = int(monitor_id)
    now = time.time()
    with _monitor_detail_cache_lock:
        cached = _monitor_detail_cache.get(monitor_id)
        generation = _monitor_detail_cache_generation
        if cached and not force_refresh and now < float(cached.get("expires_at") or 0.0):
            return str(cached["html"])

    context = build_monitor_detail_context(request, monitor_id)
    if not context:
        return None
    html = render_template_content("index.html", {**context, "partial": "monitor-detail"})
    with _monitor_detail_cache_lock:
        if generation == _monitor_detail_cache_generation:
            _monitor_detail_cache[monitor_id] = {
                "html": html,
                "expires_at": time.time() + MONITOR_DETAIL_CACHE_TTL_SECONDS,
            }
    return html


def build_all_monitor_detail_html(request: Request) -> dict[str, str]:
    with _monitor_detail_cache_lock:
        generation = _monitor_detail_cache_generation
    settings = get_settings()
    app_timezone = settings.get("app_timezone", "UTC")
    global_interval_override = max(0, int(settings.get("global_monitor_interval_override") or 0))
    monitors = list_monitors(include_heavy_details=True, include_uptime_rollups=False)
    logs_by_monitor = get_recent_logs_for_monitors([int(monitor["id"]) for monitor in monitors])
    result: dict[str, str] = {}
    for monitor in monitors:
        monitor_id = int(monitor["id"])
        monitor["effective_interval"] = global_interval_override or int(monitor.get("interval") or 60)
        monitor["logs"] = logs_by_monitor.get(monitor_id, [])
        monitor["display_status"] = "paused" if not monitor.get("enabled", 1) else monitor["status"]
        monitor["last_checked_at"] = format_timestamp(monitor.get("last_checked_at"), app_timezone)
        monitor["last_change_at"] = format_timestamp(monitor.get("last_change_at"), app_timezone)
        monitor["last_success_at"] = format_timestamp(monitor.get("last_success_at"), app_timezone)
        monitor["last_down_at"] = format_timestamp(monitor.get("last_down_at"), app_timezone)
        for log in monitor["logs"]:
            log["checked_at"] = format_timestamp(log.get("checked_at"), app_timezone)
        result[str(monitor_id)] = render_template_content(
            "index.html",
            {
                "request": request,
                "settings": settings,
                "monitor": monitor,
                "active_page": "dashboard",
                "partial": "monitor-detail",
            },
        )
    expires_at = time.time() + MONITOR_DETAIL_CACHE_TTL_SECONDS
    with _monitor_detail_cache_lock:
        if generation == _monitor_detail_cache_generation:
            for monitor_id, html in result.items():
                _monitor_detail_cache[int(monitor_id)] = {"html": html, "expires_at": expires_at}
    return result


def build_dashboard_shell_context(request: Request, *, cached_only: bool = False) -> dict:
    settings = get_settings()
    snapshot = peek_dashboard_snapshot() if cached_only else None
    cached_summary = snapshot.get("summary") if isinstance(snapshot, dict) else None
    summary = dict(cached_summary) if isinstance(cached_summary, dict) else (
        {"total": 0, "up": 0, "down": 0, "unknown": 0, "paused": 0, "categories": []}
        if cached_only
        else get_monitor_summary()
    )
    app_timezone = settings.get("app_timezone", "UTC")
    overall_status = "All systems operational" if summary["down"] == 0 else f"{summary['down']} issue(s) detected"
    overall_tone = "ok" if summary["down"] == 0 else "problem"
    summary["overall_status"] = overall_status
    summary["overall_tone"] = overall_tone
    summary["last_updated_at"] = format_timestamp(datetime.now(timezone.utc).replace(microsecond=0).isoformat(), app_timezone)
    if not cached_only:
        summary["categories"] = get_monitor_group_summary()

    cached_changelog = _changelog_cache.get("items")
    changelog_preview = list(cached_changelog)[:3] if cached_only and isinstance(cached_changelog, list) else get_changelog_items(limit=3)
    cached_version = _app_version_cache.get("value")
    app_version = str(cached_version) if cached_only and cached_version else (str(__version__) if cached_only else get_app_version_display())

    return {
        "request": request,
        "settings": settings,
        "app_version": app_version,
        "changelog_preview": changelog_preview,
        "active_page": "dashboard",
        "toast": get_toast(request),
        "summary": summary,
    }


def build_settings_context(request: Request) -> dict:
    settings = get_settings()
    timezone_options = APP_TIMEZONE_OPTIONS.copy()
    current_timezone = settings.get("app_timezone", "UTC")
    if current_timezone not in timezone_options:
        timezone_options.insert(0, current_timezone)
    return {
        "request": request,
        "settings": settings,
        "app_version": get_app_version_display(),
        "changelog_preview": get_changelog_items(limit=3),
        "system_metrics": build_system_metrics(),
        "timezone_options": timezone_options,
        "active_page": "settings",
        "toast": get_toast(request),
    }


def build_settings_system_status_context(request: Request) -> dict:
    return {
        "request": request,
        "system_metrics": build_system_metrics(),
    }


def build_incidents_context(request: Request, *, quick: bool = False) -> dict:
    settings = get_settings()
    app_timezone = settings.get("app_timezone", "UTC")
    monitors = list_monitor_incident_feed_options()
    monitor_id, status, since_days, item_raw, page = parse_incident_filters(request)

    incidents = list_incidents(monitor_id=monitor_id, status=status, since_days=since_days, limit=20 if quick else 200)

    base_query: dict[str, str] = {}
    if monitor_id is not None:
        base_query["monitor_id"] = str(monitor_id)
    if status and status != "all":
        base_query["status"] = status
    base_query["days"] = "all" if since_days is None else str(since_days)

    feed_items: list[dict[str, Any]] = []
    incident_burst_counts: dict[str, int] = {}
    for incident in incidents:
        bucket = get_incident_burst_bucket(incident.get("started_at"))
        if bucket:
            incident_burst_counts[bucket] = incident_burst_counts.get(bucket, 0) + 1

    for incident in incidents:
        incident["started_at_display"] = format_timestamp(incident.get("started_at"), app_timezone)
        incident["ended_at_display"] = format_timestamp(incident.get("ended_at"), app_timezone)
        incident["first_failed_at_display"] = format_timestamp(incident.get("first_failed_at"), app_timezone)
        incident["confirmed_down_at_display"] = format_timestamp(incident.get("confirmed_down_at"), app_timezone)
        incident["first_recovered_at_display"] = format_timestamp(incident.get("first_recovered_at"), app_timezone)
        incident["confirmed_up_at_display"] = format_timestamp(incident.get("confirmed_up_at"), app_timezone)

        incident_item_id = f"incident:{incident.get('id')}"
        incident["item_id"] = incident_item_id
        incident["select_url"] = "/incidents?" + urlencode({**base_query, "item": incident_item_id})

        duration_seconds: Optional[int] = None
        started_at = incident.get("started_at")
        ended_at = incident.get("ended_at")
        try:
            if started_at:
                start_dt = datetime.fromisoformat(started_at.replace("Z", "+00:00"))
                end_dt = (
                    datetime.fromisoformat(ended_at.replace("Z", "+00:00"))
                    if ended_at
                    else datetime.now(timezone.utc)
                )
                if start_dt.tzinfo is None:
                    start_dt = start_dt.replace(tzinfo=timezone.utc)
                if end_dt.tzinfo is None:
                    end_dt = end_dt.replace(tzinfo=timezone.utc)
                duration_seconds = max(0, int((end_dt - start_dt).total_seconds()))
        except Exception:
            duration_seconds = None

        incident["duration_seconds"] = duration_seconds
        incident["duration_display"] = format_duration_short(duration_seconds)
        burst_bucket = get_incident_burst_bucket(incident.get("started_at"))
        burst_count = incident_burst_counts.get(burst_bucket or "", 0)
        incident["burst_count"] = burst_count
        incident["burst_hint"] = (
            f"{burst_count} Incidents starteten in derselben Minute. "
            "Das spricht eher für DNS-, Netzwerk-, Proxy- oder Host-Probleme "
            "als für einzelne Dienste."
            if burst_count >= 3
            else None
        )

        feed_items.append(
            {
                "kind": "incident",
                "item_id": incident_item_id,
                "timestamp": incident.get("started_at"),
                "timestamp_display": incident.get("started_at_display"),
                "monitor_id": incident.get("monitor_id"),
                "monitor_name": incident.get("monitor_name"),
                "monitor_type": incident.get("monitor_type"),
                "monitor_target": incident.get("monitor_target"),
                "is_open": incident.get("ended_at") is None,
                "duration_display": incident.get("duration_display"),
                "burst_count": burst_count,
                "burst_hint": incident.get("burst_hint"),
                "incident": incident,
                "select_url": incident.get("select_url"),
            }
        )

    if since_days is not None:
        cutoff_dt = datetime.now(timezone.utc) - timedelta(days=since_days)
    else:
        cutoff_dt = None

    for monitor in ([] if quick else monitors):
        if monitor_id is not None and int(monitor.get("id")) != monitor_id:
            continue

        created_at = monitor.get("created_at")
        if not created_at:
            continue

        try:
            created_dt = datetime.fromisoformat(str(created_at).replace("Z", "+00:00"))
            if created_dt.tzinfo is None:
                created_dt = created_dt.replace(tzinfo=timezone.utc)
            if cutoff_dt is not None and created_dt < cutoff_dt:
                continue
        except Exception:
            pass

        created_item_id = f"created:{monitor.get('id')}"
        feed_items.append(
            {
                "kind": "created",
                "item_id": created_item_id,
                "timestamp": created_at,
                "timestamp_display": format_timestamp(str(created_at), app_timezone),
                "monitor_id": monitor.get("id"),
                "monitor_name": monitor.get("name"),
                "monitor_type": monitor.get("type"),
                "monitor_target": monitor.get("target"),
                "select_url": "/incidents?" + urlencode({**base_query, "item": created_item_id}),
            }
        )

    def _sort_key(item: dict[str, Any]) -> str:
        return str(item.get("timestamp") or "")

    feed_items.sort(key=_sort_key, reverse=True)

    per_page = 20
    total_items = len(feed_items)
    total_pages = max(1, (total_items + per_page - 1) // per_page)
    page = min(max(1, page), total_pages)
    page_start = (page - 1) * per_page
    page_end = page_start + per_page
    paged_feed_items = feed_items[page_start:page_end]

    selected_item: Optional[dict[str, Any]] = None
    if item_raw:
        for item in paged_feed_items:
            if item.get("item_id") == item_raw:
                selected_item = item
                break
    if selected_item is None and paged_feed_items:
        selected_item = paged_feed_items[0]

    pagination_base_query = dict(base_query)
    if item_raw:
        pagination_base_query["item"] = item_raw

    def _build_page_url(target_page: int) -> str:
        query = {**pagination_base_query, "page": str(target_page)}
        return "/incidents?" + urlencode(query)

    return {
        "request": request,
        "settings": settings,
        "app_version": get_app_version_display(),
        "changelog_preview": get_changelog_items(limit=3),
        "active_page": "incidents",
        "toast": get_toast(request),
        "monitors": monitors,
        "incidents": incidents,
        "feed_items": paged_feed_items,
        "selected_item": selected_item,
        "filters": {
            "monitor_id": monitor_id,
            "status": status,
            "days": since_days,
            "page": page,
        },
        "pagination": {
            "page": page,
            "per_page": per_page,
            "total_items": total_items,
            "total_pages": total_pages,
            "has_prev": page > 1,
            "has_next": page < total_pages,
            "prev_url": _build_page_url(page - 1) if page > 1 else None,
            "next_url": _build_page_url(page + 1) if page < total_pages else None,
            "from_item": page_start + 1 if total_items else 0,
            "to_item": min(page_end, total_items),
        },
    }


def build_incidents_shell_context(request: Request) -> dict:
    settings = get_settings()
    monitor_id, status, since_days, _item_raw, page = parse_incident_filters(request)
    query_string = request.url.query
    incident_feed_url = "/api/incidents/feed"
    monitor_options = list_monitor_options()
    if query_string:
        incident_feed_url += f"?{query_string}"

    return {
        "request": request,
        "settings": settings,
        "app_version": get_app_version_display(),
        "changelog_preview": get_changelog_items(limit=3),
        "active_page": "incidents",
        "toast": get_toast(request),
        "monitors": monitor_options,
        "monitor_count": len(monitor_options),
        "filters": {
            "monitor_id": monitor_id,
            "status": status,
            "days": since_days,
            "page": page,
        },
        "incident_feed_url": incident_feed_url,
    }


def parse_incident_filters(request: Request) -> tuple[Optional[int], str, Optional[int], str, int]:
    monitor_id_raw = request.query_params.get("monitor_id", "").strip()
    status = request.query_params.get("status", "all").strip().lower() or "all"
    days_raw = request.query_params.get("days", "7").strip()
    item_raw = request.query_params.get("item", "").strip()
    page_raw = request.query_params.get("page", "1").strip()

    monitor_id: Optional[int] = None
    if monitor_id_raw:
        try:
            monitor_id = int(monitor_id_raw)
        except ValueError:
            monitor_id = None

    since_days: Optional[int] = 7
    if days_raw in {"all", "0", ""}:
        since_days = None
    else:
        try:
            since_days = max(1, int(days_raw))
        except ValueError:
            since_days = 7
    try:
        page = max(1, int(page_raw))
    except ValueError:
        page = 1

    return monitor_id, status, since_days, item_raw, page


def _run_git_command(args: list[str]) -> Optional[str]:
    try:
        result = subprocess.run(
            args,
            cwd=str(BASE_DIR),
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=1,
        )
        return (result.stdout or "").strip() or None
    except Exception:
        return None


def _humanize_commit_subject(subject: str) -> str:
    normalized = subject.strip().rstrip(".")
    lower = normalized.lower()
    translations = (
        ("serve dashboard shell from cache", "Der Dashboard-Seitenrahmen kommt jetzt direkt aus dem vorhandenen Snapshot und wartet beim Seitenwechsel weder auf Datenbankauswertungen noch auf Git-Metadaten."),
        ("streamline dashboard and incident navigation", "Dashboard und Incidents wechseln jetzt ohne blockierenden Historienaufbau. Neue Incident-Einträge erscheinen zuerst, ältere Daten folgen im Hintergrund; Detaildaten werden gestaffelt vorgeladen."),
        ("move active filter to lower corner", "Der aktive Filter sitzt jetzt unten rechts und bleibt auf Smartphones mit ausreichend Abstand über der Navigation erreichbar."),
        ("polish filter and light theme contrast", "Der schwebende Filter kommt jetzt ohne äußeren Rahmen aus. Gleichzeitig sind Kennzahlen, Gruppen und Ansichtssteuerung der hellen Status-Wall deutlich besser lesbar."),
        ("add status wall theme modes", "Die Live-Status-Wall bietet jetzt systemgesteuertes, helles und dunkles Farbschema. Die Auswahl bleibt lokal im Browser gespeichert."),
        ("refine active filter focus style", "Der aktive Filter verwendet jetzt statt des eckigen Browserrahmens einen dezenten, abgerundeten Fokusindikator im KeepUp-Design."),
        ("pin active filter to viewport edge", "Der aktive Dashboard-Filter sitzt jetzt zuverlässig direkt am rechten Bildschirmrand und bleibt dort auch bei automatischen Aktualisierungen."),
        ("centralize monitor lifecycle and dashboard sounds", "Monitor-Aktionen koordinieren Datenbank, Scheduler, Cache und Hintergrundchecks jetzt über einen eigenen Service. Die Dashboard-Sounds wurden zusätzlich aus dem HTML-Template ausgelagert."),
        ("move floating filter button to screen edge", "Der schwebende Filterbutton sitzt jetzt platzsparend mittig am rechten Bildschirmrand."),
        ("extract system router and dashboard sorting", "Health- und Bereitschaftsprüfungen liegen jetzt in einem eigenen FastAPI-Router; die Karten-Sortierung wurde aus dem HTML-Template in ein geprüftes JavaScript-Modul verschoben."),
        ("show active filters as floating button", "Aktive Dashboard-Filter erscheinen jetzt platzsparend als schwebender Button und lassen sich mit einem Klick vollständig löschen."),
        ("add architecture boundaries and performance budgets", "Monitor-Zugriffe laufen jetzt über eine klare Repository-Grenze, Datenbankmigrationen werden schrittweise angewendet und automatische Performance-Budgets schützen schnelle Cache- und Frontend-Pfade."),
        ("refactor core architecture", "Die Kernlogik ist jetzt klar in Module für Caches, Formatierung, Systemmetriken, API-Modelle und Performance-Messung aufgeteilt. Datenbankänderungen werden versioniert und HTMX wird lokal ausgeliefert."),
        ("add live dashboard experience and status wall", "Dashboard-Daten sind jetzt atomar synchronisiert; hinzu kommen Live-Events, Mini-Timelines, Fokusmodus, Gruppenkennzahlen, Status-Wall und optionale Sounds."),
        ("add monitor groups and category filters", "Monitore können jetzt in Gruppen/Kategorien organisiert und gefiltert werden."),
        ("support multiple monitor groups", "Monitore können jetzt mehreren Gruppen gleichzeitig zugeordnet und über jede dieser Gruppen gefiltert werden."),
        ("make detail preloading non-blocking", "Kartendetails werden jetzt gedrosselt im Hintergrund vorgeladen, ohne das Dashboard oder den Raspberry Pi durch einen großen Sammelabruf auszubremsen."),
        ("fix card detail loading", "Kartendetails starten beim Öffnen zuverlässig neu, zeigen währenddessen einen Ladebalken und beschränken die Auswertung auf sieben Tage."),
        ("speed up initial dashboard loading", "Das Dashboard liefert zuerst eine kompakte Oberfläche aus, lädt Karten nur einmal aus dem Snapshot und verwendet ein deutlich kleineres Logo."),
        ("remove first request database stalls", "Der erste Seitenaufruf blockiert nicht mehr an wiederholter SQLite-WAL-Konfiguration oder langsamen Git-Abfragen."),
        ("make monitor edits update inline", "Bearbeitete Monitore werden direkt im Dashboard mit Ladeanzeige aktualisiert."),
        ("use real category dropdowns", "Kategorie-Felder nutzen echte Dropdowns mit Option für neue Gruppen."),
        ("show monitor group badges", "Monitor-Karten zeigen die zugehörige Gruppe deutlicher als Badge an."),
        ("fix category validation while monitors wait", "Kategorie-Auswahl und Speichern bleiben auch bei wartenden Monitoren bedienbar."),
        ("stabilize editing while monitors refresh", "Das Bearbeiten von Monitoren bleibt stabil, auch wenn andere Karten gerade aktualisieren."),
        ("keep monitor edit button clickable during refreshes", "Der Speichern-Button im Monitor-Dialog bleibt auch bei mehreren laufenden Änderungen zuverlässig bedienbar."),
        ("align monitor modal field rows", "Felder im Monitor-Dialog sind am Desktop sauberer auf gleicher Höhe ausgerichtet."),
        ("preserve dashboard filters after monitor saves", "Dashboard-Filter und Sortierung bleiben nach dem Anlegen oder Speichern von Monitoren erhalten."),
        ("avoid blocking live card refreshes after edits", "Live-Karten liefern vorhandene Daten sofort aus, während Aktualisierungen nach Monitoränderungen im Hintergrund laufen."),
        ("keep pending monitor category visible during refreshes", "Geänderte Monitor-Kategorien bleiben sofort sichtbar, auch wenn kurz ein älterer Kartenstand zurückkommt."),
        ("preserve sort selection during live refreshes", "Die gewählte Sortierung bleibt auch nach Live-Aktualisierungen sichtbar und aktiv."),
        ("render incidents feed directly on page load", "Die Incident-Liste wird direkt mit der Seite ausgeliefert und bleibt nicht mehr unnötig im Wartebildschirm hängen."),
        ("compact dashboard summary area", "Der obere Dashboard-Bereich mit Status und Gruppen ist platzsparender dargestellt."),
        ("add settings field tooltips", "Die Einstellungen erklären ihre Felder jetzt direkt über kompakte Tooltips."),
        ("fix settings tooltip display", "Tooltips in den Einstellungen öffnen jetzt einzeln und sind besser lesbar."),
        ("make settings tooltips wider on mobile", "Tooltips in den Einstellungen nutzen auf Smartphones mehr Bildschirmbreite."),
        ("keep dashboard counts and filters in sync", "Dashboard-Zähler entsprechen zuverlässig den Monitor-Karten und aktive Filter sind deutlich sichtbar."),
        ("isolate dashboard cache test data", "Die automatischen Cache-Tests laufen jetzt unabhängig von einer vorhandenen lokalen Datenbank."),
        ("synchronize live monitor status displays", "Kopfzeile, Statuszähler und Monitor-Karten zeigen bei Statuswechseln jetzt denselben Stand."),
        ("leeren filterzustand kompakt anzeigen", "Leere Filterergebnisse werden kompakt erklärt, ohne großen Abstand vor dem Changelog."),
        ("statuszahlen bei ladezuständen synchronisieren", "Laufende Prüfungen behalten den bestätigten Monitorstatus und Gruppenzähler bleiben synchron."),
        ("make live refresh tolerate network changes", "Live-Aktualisierungen reagieren ruhiger auf kurze Netzwerkwechsel."),
        ("show monitor forms in compact modals", "Monitor anlegen und bearbeiten öffnet jetzt als kompaktes Overlay ohne Scrollsprung."),
        ("compact monitor form layout on desktop", "Monitor-Formulare sind am Desktop deutlich kompakter und passen besser auf eine Bildschirmhöhe."),
        ("constrain monitor modal width", "Monitor-Dialoge sind am Desktop schmaler, damit die Felder nicht über die ganze Browserbreite laufen."),
        ("tighten first monitor modal fields", "Die ersten Felder im Monitor-Dialog sind am Desktop kürzer und übersichtlicher angeordnet."),
        ("improve dashboard sorting and edit position", "Dashboard-Sortierung wurde erweitert und bearbeitete Karten behalten ihre Position ruhiger bei."),
        ("fix changelog page theme", "Die Änderungsseite nutzt jetzt wieder das dunkle KeepUp-Design."),
        ("show changelog during updates", "Während eines Updates werden die enthaltenen Änderungen direkt angezeigt."),
        ("translate update changelog summaries", "Update-Änderungen werden konsequenter auf Deutsch zusammengefasst."),
        ("tighten monitor card height", "Monitor-Karten wurden kompakter gemacht und bleiben gleichmäßiger hoch."),
        ("constrain monitor card width", "Monitor-Karten halten ihre Breite stabiler und überlappen weniger."),
        ("use natural monitor card height", "Monitor-Karten nutzen eine natürlichere Höhe ohne unnötige Leerflächen."),
        ("compact monitor card controls", "Die Bedienelemente der Monitor-Karten wurden kompakter angeordnet."),
        ("move monitor card actions upward", "Die Aktionsbuttons auf Monitor-Karten sitzen jetzt näher am Inhalt."),
        ("make update wait screen more compact", "Der Wartescreen während eines Updates wurde kompakter und besser für Smartphones optimiert."),
        ("improve update changelog context", "Update-Änderungen werden mit mehr deutschem Kontext angezeigt."),
        ("add ntfy notification channel", "ntfy wurde als zusätzlicher Benachrichtigungskanal ergänzt."),
        ("fix ntfy action header encoding", "ntfy-Aktionslinks funktionieren jetzt auch mit Umlauten im Titel zuverlässig."),
        ("add rich ntfy test notification", "Ein zusätzlicher ntfy-Layout-Test wurde ergänzt."),
        ("remove duplicate ntfy test button", "Der doppelte ntfy-Testbutton wurde entfernt."),
        ("collapse botfather guide", "Die BotFather-Anleitung ist jetzt platzsparend einklappbar."),
        ("keep dashboard visible while monitors change", "Das Dashboard bleibt beim Anlegen und Ändern von Monitoren sichtbar."),
        ("show new monitor immediately while first check runs", "Neue Monitore erscheinen sofort mit Ladeanzeige auf dem Dashboard."),
        ("show warmup monitor cards instead of skeletons", "Nach Neustart oder Update werden sofort Monitor-Karten mit Ladeanzeige gezeigt."),
        ("make monitor create and delete instant in frontend", "Monitore erscheinen oder verschwinden im Frontend sofort, während der Cache im Hintergrund aktualisiert wird."),
        ("speed up browser update checks", "Update-Prüfung und Browser-Updates wurden beschleunigt."),
        ("stabilize update modal height", "Die Höhe des Update-Wartebildschirms bleibt bei wechselnden Statusmeldungen stabil."),
        ("stabilize monitor action refreshes", "Dashboard-Aktualisierungen nach Monitor-Aktionen laufen stabiler und schneller."),
        ("group notification settings", "Telegram, ntfy und E-Mail wurden in den Einstellungen gemeinsam gruppiert."),
        ("add frontend changelog from commits", "Eine Änderungsseite zeigt die letzten Updates verständlich im Frontend."),
        ("add automated ci checks", "Automatische Tests auf GitHub wurden ergänzt."),
        ("harden local operations and backup handling", "Lokale Sicherheits- und Backup-Schutzfunktionen wurden verbessert."),
        ("run manual checks without page reload", "Manuelle Monitor-Prüfungen laufen ohne komplettes Neuladen der Seite."),
        ("add monitor form field help", "Hilfetexte beim Anlegen und Bearbeiten von Monitoren wurden ergänzt."),
        ("fix monitor edit cache refresh", "Aktualisierte Monitor-Daten werden nach dem Speichern zuverlässiger angezeigt."),
        ("add ping http check modes", "Kombinierte Ping-/HTTP-Prüfungen wurden ergänzt."),
        ("trim telegram status history legend", "Telegram-Meldungen wurden gekürzt und verzichten auf unnötige Farblegenden."),
        ("fix ping http redundancy status", "Die Logik für redundante PING-oder-HTTP-Prüfungen wurde korrigiert."),
        ("link telegram monitor names to their source urls", "Monitor-Namen in Telegram verlinken direkt auf die überwachte Quelle."),
        ("treat protected http endpoints as reachable", "Geschützte HTTP-Endpunkte werden als erreichbar erkannt, wenn sie erwartbar antworten."),
        ("normalize url values used as combo ping targets", "Ping-Ziele kombinierter Checks werden aus URLs zuverlässiger normalisiert."),
        ("add combined ping and http monitor checks", "Monitore können Ping und HTTP gemeinsam prüfen."),
        ("simplify telegram notification icons", "Telegram-Benachrichtigungen nutzen weniger und ruhigere Icons."),
        ("improve backups, card details, and dashboard responsiveness", "Backups, Kartendetails und Dashboard-Reaktionszeit wurden verbessert."),
        ("repair corrupted system python caches during setup", "Das Setup kann beschädigte Python-Cache-Dateien besser bereinigen."),
        ("recover damaged python environments during updates", "Updates können beschädigte Python-Umgebungen besser wiederherstellen."),
        ("keep monitor details visible when chart rendering fails", "Kartendetails bleiben sichtbar, auch wenn ein Diagramm nicht geladen werden kann."),
        ("enhance telegram notifications with links and check history", "Telegram-Meldungen enthalten Links und eine kompakte Check-Historie."),
        ("center uptime mini-card content on dashboard cards", "Uptime-Kennzahlen auf Karten sind optisch sauberer zentriert."),
        ("refine outage duration display and compact dashboard card metrics", "Ausfallzeiten und Karten-Kennzahlen wurden kompakter dargestellt."),
        ("add outage-hours display and remove timezone suffix from ui timestamps", "Karten zeigen Ausfallzeiten an und verzichten im UI auf Zeitzonen-Suffixe."),
        ("add card uptime trio and persist down filter state", "Monitor-Karten zeigen mehrere Uptime-Werte und behalten den Down-Filter bei."),
        ("persist dashboard status filter and show last down timestamp", "Dashboard-Filter bleiben erhalten und Karten zeigen das letzte Nicht-Erreichen."),
        ("clarify http content rule behavior", "Hinweise zu HTTP-Inhaltsregeln wurden verständlicher gemacht."),
        ("optimize monitor scheduler updates", "Scheduler-Updates für Monitoränderungen wurden beschleunigt."),
        ("improve mobile restart overlay and fix migrated downtime metrics", "Der Neustart-Wartebildschirm wurde mobil verbessert und migrierte Downtime-Werte korrigiert."),
        ("build local tailwind pipeline and enhance dashboard update ux", "Tailwind läuft lokal und der Update-Ablauf im Dashboard wurde verbessert."),
        ("show system resource snapshot in settings", "Die Einstellungen zeigen CPU-, RAM- und Netzwerk-Auslastung des Hosts."),
        ("document raspberry pi sizing recommendations", "Die README enthält Empfehlungen für Raspberry-Pi-Betrieb und sinnvolle Intervalle."),
    )
    for prefix, text in translations:
        if lower.startswith(prefix):
            return text
    german_markers = (
        " der ", " die ", " das ", " und ", " für ", " wurde ", " werden ",
        "beheben", "verbessern", "anzeigen", "synchronisieren", "entfernen", "aktualisieren",
    )
    padded_lower = f" {lower} "
    if any(marker in padded_lower for marker in german_markers) or any(char in lower for char in "äöüß"):
        return f"{normalized}." if normalized and not normalized.endswith(".") else normalized
    keyword_summaries = (
        (("card", "height"), "Die Darstellung der Monitor-Karten wurde verbessert."),
        (("monitor", "card"), "Die Monitor-Karten wurden im Frontend verbessert."),
        (("changelog",), "Die Anzeige der Änderungen wurde verbessert."),
        (("update",), "Der Update-Ablauf wurde verbessert."),
        (("telegram", "notification"), "Telegram-Benachrichtigungen wurden verbessert."),
        (("backup",), "Backup-Funktionen wurden verbessert."),
        (("import",), "Der Import von Backups wurde verbessert."),
        (("dashboard",), "Das Dashboard wurde verbessert."),
        (("incident",), "Die Incident-Ansicht wurde verbessert."),
        (("settings",), "Die Einstellungen wurden verbessert."),
        (("security",), "Sicherheitsaspekte wurden verbessert."),
        (("test",), "Automatische Tests wurden verbessert."),
        (("fix",), "Ein Fehler wurde behoben."),
    )
    for keywords, text in keyword_summaries:
        if all(keyword in lower for keyword in keywords):
            return text
    return ""


def _format_german_date(date_value: str) -> str:
    value = date_value.strip()
    if not value:
        return ""
    try:
        if "T" in value:
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return dt.strftime("%d.%m.%Y")
        if len(value) == 10 and value[4] == "-" and value[7] == "-":
            dt = datetime.strptime(value, "%Y-%m-%d")
            return dt.strftime("%d.%m.%Y")
    except ValueError:
        return value
    return value


def get_changelog_items(limit: int = 8) -> list[dict[str, str]]:
    fetch_limit = max(20, limit)
    now = time.time()
    cached_items = _changelog_cache.get("items")
    expires_at = float(_changelog_cache.get("expires_at") or 0.0)
    if cached_items is not None and now < expires_at:
        return list(cached_items)[:limit]

    output = _run_git_command(
        [
            "git",
            "log",
            f"-n{fetch_limit}",
            "--date=format:%d.%m.%Y %H:%M",
            "--pretty=format:%h%x09%ad%x09%s",
        ]
    )
    items: list[dict[str, str]] = []
    for line in (output or "").splitlines():
        parts = line.split("\t", 2)
        if len(parts) != 3:
            continue
        sha, committed_at, subject = (part.strip() for part in parts)
        summary = _humanize_commit_subject(subject)
        if summary:
            items.append(
                {
                    "sha": sha,
                    "committed_at": committed_at,
                    "subject": subject,
                    "summary": summary,
                }
            )

    _changelog_cache["items"] = items
    _changelog_cache["expires_at"] = now + 60
    return items[:limit]


def build_changelog_context(request: Request) -> dict:
    settings = get_settings()
    changelog_items = get_changelog_items(limit=20)
    return {
        "request": request,
        "settings": settings,
        "app_version": get_app_version_display(),
        "changelog_preview": changelog_items[:3],
        "changelog_items": changelog_items,
        "active_page": "changelog",
        "toast": get_toast(request),
    }


def get_app_version_display() -> str:
    now = time.time()
    cached_value = _app_version_cache.get("value")
    expires_at = float(_app_version_cache.get("expires_at") or 0.0)
    if cached_value and now < expires_at:
        return str(cached_value)

    revision = _run_git_command(["git", "rev-list", "--count", "HEAD"])
    value = f"{__version__} rev.{revision}" if revision and revision.isdigit() else __version__
    _app_version_cache["value"] = value
    _app_version_cache["expires_at"] = now + APP_VERSION_TTL_SECONDS
    return value


def is_combo_monitor_type(monitor_type: str) -> bool:
    return monitor_type in {"ping_http", "ping_http_or", "ping_http_and"}


def combo_ping_mode(monitor_type: str) -> str:
    return "and" if monitor_type == "ping_http_and" else "or"


def normalize_monitor_target(monitor_type: str, target: str) -> str:
    normalized = target.strip()
    if monitor_type in {"http", "ping_http", "ping_http_or", "ping_http_and"} and normalized:
        parsed = urlparse(normalized)
        if not parsed.scheme:
            normalized = f"https://{normalized}"
    return normalized


def invalidate_dashboard_cards_cache() -> None:
    advance_dashboard_snapshot_version()
    with _dashboard_cards_cache_lock:
        _dashboard_cards_cache["html"] = None
        _dashboard_cards_cache["expires_at"] = 0.0
        _dashboard_cards_cache["generation"] = int(_dashboard_cards_cache.get("generation") or 0) + 1


def mark_dashboard_cards_cache_stale() -> None:
    advance_dashboard_snapshot_version()
    with _dashboard_cards_cache_lock:
        _dashboard_cards_cache["generation"] = int(_dashboard_cards_cache.get("generation") or 0) + 1
        if _dashboard_cards_cache.get("html"):
            _dashboard_cards_cache["expires_at"] = time.time() - 1
        else:
            _dashboard_cards_cache["expires_at"] = 0.0


def peek_dashboard_cards_html() -> Optional[str]:
    with _dashboard_cards_cache_lock:
        cached_html = _dashboard_cards_cache.get("html")
        return str(cached_html) if cached_html else None


def render_template_content(name: str, context: dict[str, Any]) -> str:
    if "request" not in context:
        context = {**context, "request": None}
    template = templates.env.get_template(name)
    return template.render(**context)


def get_dashboard_cards_html(force_refresh: bool = False) -> Optional[str]:
    now = time.time()
    with _dashboard_cards_cache_lock:
        cached_html = _dashboard_cards_cache.get("html")
        expires_at = float(_dashboard_cards_cache.get("expires_at") or 0.0)
        generation = int(_dashboard_cards_cache.get("generation") or 0)
        if not force_refresh and cached_html and now < expires_at:
            return str(cached_html)
        stale_html = str(cached_html) if cached_html else None

    try:
        payload = build_dashboard_cards_payload()
        html = render_template_content("index.html", {**payload, "partial": "cards-inner"})
    except Exception:
        logger.exception("dashboard_cards_cache_build_failed")
        return stale_html

    with _dashboard_cards_cache_lock:
        if generation != int(_dashboard_cards_cache.get("generation") or 0):
            return stale_html
        _dashboard_cards_cache["html"] = html
        _dashboard_cards_cache["expires_at"] = time.time() + DASHBOARD_CARDS_CACHE_TTL_SECONDS
    return html


def dashboard_cards_cache_is_stale() -> bool:
    with _dashboard_cards_cache_lock:
        expires_at = float(_dashboard_cards_cache.get("expires_at") or 0.0)
        return time.time() >= expires_at


def dashboard_cards_cache_needs_immediate_rebuild() -> bool:
    with _dashboard_cards_cache_lock:
        return not bool(_dashboard_cards_cache.get("html"))


async def ensure_dashboard_cards_cache_refresh(force: bool = False) -> None:
    global _dashboard_cards_refresh_task
    if not force and not dashboard_cards_cache_is_stale():
        return
    if _dashboard_cards_refresh_task is not None and not _dashboard_cards_refresh_task.done():
        return

    async def _refresh() -> None:
        try:
            await asyncio.to_thread(get_dashboard_cards_html, True)
        except Exception:
            logger.exception("dashboard_cards_refresh_failed")

    _dashboard_cards_refresh_task = asyncio.create_task(_refresh())


async def wait_for_dashboard_cards_cache_refresh(force: bool = False) -> None:
    """Refresh stale cards once and wait until the shared cache is coherent."""
    for attempt in range(2):
        await ensure_dashboard_cards_cache_refresh(force=force or attempt > 0)
        task = _dashboard_cards_refresh_task
        if task is not None:
            await asyncio.shield(task)
        if not dashboard_cards_cache_is_stale():
            return


async def execute_monitor_check_and_refresh_cards(monitor_id: int) -> None:
    await execute_monitor_check(monitor_id)
    invalidate_monitor_detail_cache(monitor_id)
    mark_dashboard_cards_cache_stale()
    await ensure_dashboard_cards_cache_refresh(force=True)


async def refresh_dashboard_cards_cache() -> None:
    await ensure_dashboard_cards_cache_refresh(force=True)


monitor_service = MonitorService(
    repository=monitor_repository,
    invalidate_detail=invalidate_monitor_detail_cache,
    mark_dashboard_stale=mark_dashboard_cards_cache_stale,
    reschedule_job=lambda monitor_id: reschedule_monitor_job(scheduler, monitor_id),
    remove_job=lambda monitor_id: remove_monitor_job(scheduler, monitor_id),
    check_and_refresh=lambda monitor_id: execute_monitor_check_and_refresh_cards(monitor_id),
    refresh_dashboard=lambda: refresh_dashboard_cards_cache(),
    execute_check=lambda monitor_id: execute_monitor_check(monitor_id),
)


def handle_scheduler_job_executed(event: Any) -> None:
    result = getattr(event, "retval", None)
    if (
        str(getattr(event, "job_id", "")).startswith("monitor-")
        and isinstance(result, dict)
        and result.get("status_changed")
    ):
        try:
            invalidate_monitor_detail_cache(int(str(event.job_id).removeprefix("monitor-")))
        except ValueError:
            invalidate_monitor_detail_cache()
        mark_dashboard_cards_cache_stale()


def _schedule_self_restart(delay_seconds: float = 1.8) -> None:
    def _restart() -> None:
        os._exit(1)

    timer = threading.Timer(delay_seconds, _restart)
    timer.daemon = True
    timer.start()


def _same_origin_base(request: Request) -> str:
    return str(request.base_url).rstrip("/")


def _is_same_origin_request(request: Request) -> bool:
    expected = _same_origin_base(request)
    origin = (request.headers.get("origin") or "").strip()
    referer = (request.headers.get("referer") or "").strip()
    if origin:
        return origin.rstrip("/") == expected
    if referer:
        try:
            parsed = urlparse(referer)
        except Exception:
            return False
        referer_origin = f"{parsed.scheme}://{parsed.netloc}".rstrip("/")
        return referer_origin == expected
    return False


def _build_update_run_token(secret: str, window: int) -> str:
    payload = f"{window}:{BASE_DIR}"
    return hmac.new(secret.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256).hexdigest()


def _current_update_token_window(now: Optional[float] = None) -> int:
    timestamp = now if now is not None else time.time()
    return int(timestamp // 120)


def _issue_update_run_token(secret: str) -> tuple[str, int]:
    window = _current_update_token_window()
    expires_at = (window + 1) * 120
    return _build_update_run_token(secret, window), expires_at


def _validate_update_run_token(secret: str, provided_token: str) -> bool:
    if not provided_token:
        return False
    current_window = _current_update_token_window()
    valid_tokens = (
        _build_update_run_token(secret, current_window),
        _build_update_run_token(secret, current_window - 1),
    )
    return any(hmac.compare_digest(provided_token, valid_token) for valid_token in valid_tokens)


def _format_commit_change(sha: str, subject: str, committed_at: str = "") -> dict[str, str]:
    sha = sha.strip()
    subject = subject.strip()
    return {
        "sha": sha[:7],
        "subject": subject,
        "summary": _humanize_commit_subject(subject),
        "committed_at": _format_german_date(committed_at),
    }


def _get_update_commit_summaries(previous_sha: Optional[str], current_sha: Optional[str], limit: int = 8) -> list[str]:
    if not previous_sha or not current_sha or previous_sha == current_sha:
        return []
    try:
        result = subprocess.run(
            [
                "git",
                "-C",
                str(BASE_DIR),
                "log",
                "--pretty=format:%h %s",
                f"{previous_sha}..{current_sha}",
                f"-n{max(1, limit)}",
            ],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=4,
        )
    except Exception:
        return []
    return [line.strip() for line in (result.stdout or "").splitlines() if line.strip()]


def _get_update_commit_details(previous_sha: Optional[str], current_sha: Optional[str], limit: int = 8) -> list[dict[str, str]]:
    if not previous_sha or not current_sha or previous_sha == current_sha:
        return []
    try:
        result = subprocess.run(
            [
                "git",
                "-C",
                str(BASE_DIR),
                "log",
                "--date=format:%d.%m.%Y",
                "--pretty=format:%h%x09%ad%x09%s",
                f"{previous_sha}..{current_sha}",
                f"-n{max(1, limit)}",
            ],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=4,
        )
    except Exception:
        return []

    changes: list[dict[str, str]] = []
    for line in (result.stdout or "").splitlines():
        parts = line.split("\t", 2)
        if len(parts) != 3:
            continue
        sha, committed_at, subject = (part.strip() for part in parts)
        change = _format_commit_change(sha, subject, committed_at)
        if change["summary"]:
            changes.append(change)
    return changes


async def _get_pending_update_changes(local_sha: Optional[str], remote_sha: Optional[str], limit: int = 6) -> list[dict[str, str]]:
    if not local_sha or not remote_sha or local_sha == remote_sha:
        return []
    url = f"https://api.github.com/repos/Schello805/keepup/compare/{local_sha}...{remote_sha}"
    headers = {"User-Agent": "KeepUp"}
    try:
        async with httpx.AsyncClient(timeout=UPDATE_REMOTE_TIMEOUT_SECONDS, headers=headers) as client:
            res = await client.get(url)
        if res.status_code != 200:
            return []
        payload = res.json()
        commits = payload.get("commits") or []
        changes: list[dict[str, str]] = []
        for commit in commits[-max(1, limit):]:
            sha = str(commit.get("sha") or "")
            commit_payload = commit.get("commit") or {}
            message = str(commit_payload.get("message") or "").splitlines()[0].strip()
            author_payload = commit_payload.get("author") or {}
            committed_at = str(author_payload.get("date") or "")
            if sha and message:
                change = _format_commit_change(sha, message, committed_at)
                if change["summary"]:
                    changes.append(change)
        return list(reversed(changes))
    except Exception:
        return []


async def _get_remote_main_sha() -> Optional[str]:
    url = "https://api.github.com/repos/Schello805/keepup/commits/main"
    headers = {"User-Agent": "KeepUp"}
    try:
        async with httpx.AsyncClient(timeout=UPDATE_REMOTE_TIMEOUT_SECONDS, headers=headers) as client:
            res = await client.get(url)
        if res.status_code != 200:
            return None
        payload = res.json()
        sha = payload.get("sha")
        return sha.strip() if isinstance(sha, str) and sha.strip() else None
    except Exception:
        return None


async def get_cached_update_status_payload() -> dict[str, Any]:
    now = time.time()
    cached_payload = _update_status_cache.get("payload")
    expires_at = float(_update_status_cache.get("expires_at") or 0.0)
    if cached_payload and now < expires_at:
        return cached_payload

    local_sha = _run_git_command(["git", "rev-parse", "HEAD"])
    remote_sha = await _get_remote_main_sha()
    update_available = bool(local_sha and remote_sha and local_sha != remote_sha)
    pending_changes = await _get_pending_update_changes(local_sha, remote_sha) if update_available else []
    token = os.environ.get("KEEPUP_UPDATE_TOKEN", "").strip()
    update_enabled = bool(token)
    update_run_token = None
    update_run_token_expires_at = None
    if token:
        update_run_token, update_run_token_expires_at = _issue_update_run_token(token)

    payload = {
        "current_version": get_app_version_display(),
        "local_sha": local_sha,
        "local_sha_short": (local_sha[:7] if local_sha else None),
        "remote_sha": remote_sha,
        "remote_sha_short": (remote_sha[:7] if remote_sha else None),
        "update_available": update_available,
        "update_enabled": update_enabled,
        "pending_changes": pending_changes,
        "update_run_token": update_run_token,
        "update_run_token_expires_at": update_run_token_expires_at,
        "overlay_metrics": build_update_overlay_metrics(),
    }
    _update_status_cache["payload"] = payload
    _update_status_cache["expires_at"] = now + UPDATE_STATUS_TTL_SECONDS
    return payload


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("startup")
    init_db()
    await init_monitor_runtime()
    asyncio.create_task(ensure_dashboard_cards_cache_refresh(force=True))
    scheduler.add_job(
        lambda: asyncio.Task(asyncio.to_thread(cleanup_old_checks)),
        "interval",
        hours=12,
        id="db-cleanup",
        replace_existing=True,
    )
    reschedule_monitor_jobs(scheduler)
    scheduler.add_listener(handle_scheduler_job_executed, EVENT_JOB_EXECUTED)
    scheduler.start()
    async def _run_initial_checks() -> None:
        try:
            await run_all_checks_once()
            mark_dashboard_cards_cache_stale()
        except Exception:
            logger.exception("initial_checks_failed")

    asyncio.create_task(_run_initial_checks())
    yield
    scheduler.shutdown(wait=False)
    await shutdown_monitor_runtime()
    logger.info("shutdown")


app = FastAPI(title="KeepUp", lifespan=lifespan)
app.add_middleware(GZipMiddleware, minimum_size=1024, compresslevel=6)
app.add_middleware(RequestTimingMiddleware, slow_request_seconds=0.75)
configure_system_routes(get_db, scheduler)
app.include_router(system_router)
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))


def render_template(request: Request, name: str, context: dict[str, Any]) -> HTMLResponse:
    # Render templates via the Jinja2 environment directly to avoid
    # passing the full request/context as "globals" to get_template()
    # (which can trigger Jinja2 cache hashing errors for unhashable values).
    if "request" not in context:
        context = {**context, "request": request}
    template = templates.env.get_template(name)
    content = template.render(**context)
    return HTMLResponse(content)


@app.get("/api/update/status")
async def update_status() -> JSONResponse:
    payload = await get_cached_update_status_payload()
    return JSONResponse(payload)


@app.post("/api/update/run")
async def run_update(request: Request) -> JSONResponse:
    expected = os.environ.get("KEEPUP_UPDATE_TOKEN", "").strip()
    if not expected:
        raise HTTPException(status_code=403, detail="Update ist nicht aktiviert (KEEPUP_UPDATE_TOKEN fehlt).")
    if not _is_same_origin_request(request):
        raise HTTPException(status_code=403, detail="Update-Anfrage wurde aus Sicherheitsgründen blockiert.")
    provided_proof = (request.headers.get("x-keepup-update-proof") or "").strip()
    if not _validate_update_run_token(expected, provided_proof):
        raise HTTPException(status_code=403, detail="Update-Freigabe ist ungültig oder abgelaufen. Bitte Seite neu laden.")

    script_path = BASE_DIR / "scripts" / "update_keepup.sh"
    if not script_path.exists():
        raise HTTPException(status_code=500, detail="Update-Script fehlt.")

    previous_sha = _run_git_command(["git", "rev-parse", "HEAD"])

    try:
        result = await asyncio.to_thread(
            lambda: subprocess.run(
                ["bash", str(script_path)],
                cwd=str(BASE_DIR),
                check=False,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=240,
                env={**os.environ, "KEEPUP_FRONTEND_UPDATE": "1"},
            )
        )
    except subprocess.TimeoutExpired:
        raise HTTPException(status_code=500, detail="Update-Script timeout.")
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Update fehlgeschlagen: {exc}")

    stdout = (result.stdout or "").strip()
    stderr = (result.stderr or "").strip()
    ok = result.returncode == 0
    current_sha = _run_git_command(["git", "rev-parse", "HEAD"])
    changes = _get_update_commit_summaries(previous_sha, current_sha)
    change_details = _get_update_commit_details(previous_sha, current_sha)
    restart_scheduled = bool(ok and current_sha and current_sha != previous_sha)
    if restart_scheduled:
        _schedule_self_restart()
    return JSONResponse(
        {
            "ok": ok,
            "returncode": result.returncode,
            "previous_sha": previous_sha,
            "current_sha": current_sha,
            "previous_sha_short": (previous_sha[:7] if previous_sha else None),
            "current_sha_short": (current_sha[:7] if current_sha else None),
            "changes": changes,
            "change_details": change_details,
            "restart_scheduled": restart_scheduled,
            "service_ready_url": "/ready",
            "stdout": stdout,
            "stderr": stderr,
        },
        status_code=200 if ok else 500,
    )


@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request) -> HTMLResponse:
    context = await asyncio.to_thread(build_dashboard_shell_context, request, cached_only=True)
    await ensure_dashboard_snapshot_refresh(request)
    return await asyncio.to_thread(render_template, request, "index.html", context)


@app.get("/wall", response_class=HTMLResponse)
async def status_wall(request: Request) -> HTMLResponse:
    payload = peek_status_wall_payload()
    if payload is None:
        try:
            payload = await asyncio.wait_for(asyncio.to_thread(build_status_wall_payload), timeout=8.0)
        except asyncio.TimeoutError:
            logger.warning("status_wall_initial_build_timeout")
            monitors = await asyncio.to_thread(list_status_wall_monitors, False)
            payload = build_status_wall_payload(monitors)
            payload["details_pending"] = True
            await ensure_status_wall_refresh()
    context = {
        "request": request,
        "settings": get_settings(),
        "payload": payload,
        "app_version": get_app_version_display(),
    }
    return await asyncio.to_thread(render_template, request, "status_wall.html", context)


@app.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request) -> HTMLResponse:
    context = await asyncio.to_thread(build_settings_context, request)
    return await asyncio.to_thread(render_template, request, "settings.html", context)


@app.get("/api/settings/system-status", response_class=HTMLResponse)
async def settings_system_status_partial(request: Request) -> HTMLResponse:
    context = await asyncio.to_thread(build_settings_system_status_context, request)
    return await asyncio.to_thread(render_template, request, "settings.html", {**context, "partial": "system-status"})


@app.get("/incidents", response_class=HTMLResponse)
async def incidents_page(request: Request) -> HTMLResponse:
    context = await asyncio.to_thread(build_incidents_shell_context, request)
    return await asyncio.to_thread(render_template, request, "incidents.html", context)


@app.get("/changelog", response_class=HTMLResponse)
async def changelog_page(request: Request) -> HTMLResponse:
    context = await asyncio.to_thread(build_changelog_context, request)
    return await asyncio.to_thread(render_template, request, "changelog.html", context)


@app.get("/api/incidents/feed", response_class=HTMLResponse)
async def incidents_feed_partial(request: Request) -> HTMLResponse:
    context = await asyncio.to_thread(build_incidents_context, request, quick=request.query_params.get("quick") == "1")
    return await asyncio.to_thread(render_template, request, "incidents.html", {**context, "partial": "feed"})


@app.get("/api/dashboard", response_class=HTMLResponse)
async def dashboard_partial(request: Request) -> HTMLResponse:
    context = await asyncio.to_thread(build_dashboard_context, request)
    return await asyncio.to_thread(render_template, request, "index.html", {**context, "partial": True})


@app.get("/api/dashboard/snapshot")
async def dashboard_snapshot(request: Request) -> JSONResponse:
    payload = peek_dashboard_snapshot()
    version = get_dashboard_snapshot_version()
    if payload is None or int(payload.get("version") or 0) != version:
        await ensure_dashboard_snapshot_refresh(request)
        return JSONResponse(
            {"ready": False, "version": version},
            status_code=202,
            headers={"Cache-Control": "no-store", "Retry-After": "1"},
        )
    with _dashboard_snapshot_cache_lock:
        expired = time.time() >= float(_dashboard_snapshot_cache.get("expires_at") or 0.0)
    if expired:
        await ensure_dashboard_snapshot_refresh(request)
    return JSONResponse(payload, headers={"Cache-Control": "no-store"})


@app.get("/api/weather/today")
async def weather_today() -> JSONResponse:
    location = str(get_settings().get("weather_location") or "").strip()
    if not location:
        return JSONResponse({"ok": False, "configured": False, "message": "Kein Wetter-Ort konfiguriert."})
    payload = await get_cached_weather_today(location)
    return JSONResponse(payload, headers={"Cache-Control": "no-store"})


@app.get("/favicon.ico", include_in_schema=False)
async def favicon() -> RedirectResponse:
    return RedirectResponse(url="/static/logo.png", status_code=307)


@app.get("/api/status-wall")
async def status_wall_snapshot(request: Request) -> JSONResponse:
    payload = peek_status_wall_payload()
    version = get_dashboard_snapshot_version()
    if payload is None or int(payload.get("version") or 0) != version:
        try:
            payload = await asyncio.wait_for(asyncio.to_thread(build_status_wall_payload), timeout=8.0)
        except asyncio.TimeoutError:
            await ensure_status_wall_refresh()
            return JSONResponse(
                {"ready": False, "version": version},
                status_code=202,
                headers={"Cache-Control": "no-store", "Retry-After": "1"},
            )
    with _status_wall_cache_lock:
        expired = time.time() >= float(_status_wall_cache.get("expires_at") or 0.0)
    if expired:
        await ensure_status_wall_refresh()
    return JSONResponse(payload, headers={"Cache-Control": "no-store"})


@app.get("/api/live/events")
async def dashboard_live_events(request: Request) -> StreamingResponse:
    async def event_stream():
        last_version = -1
        while not await request.is_disconnected():
            version = get_dashboard_snapshot_version()
            if version != last_version:
                last_version = version
                yield f"event: snapshot\ndata: {version}\n\n"
            else:
                yield ": keepalive\n\n"
            await asyncio.sleep(2)

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.get("/api/live/top", response_class=HTMLResponse)
async def live_top_partial(request: Request) -> HTMLResponse:
    context = await asyncio.to_thread(build_dashboard_shell_context, request)
    return await asyncio.to_thread(render_template, request, "index.html", {**context, "partial": "top"})


@app.get("/api/live/cards", response_class=HTMLResponse)
async def live_cards_partial(request: Request) -> HTMLResponse:
    if dashboard_cards_cache_is_stale():
        await wait_for_dashboard_cards_cache_refresh(force=False)
    html = peek_dashboard_cards_html()
    if html is None:
        await wait_for_dashboard_cards_cache_refresh(force=True)
        html = peek_dashboard_cards_html()
    if html is None:
        context = await asyncio.to_thread(build_dashboard_cards_payload)
        for monitor in context["monitors"]:
            if monitor.get("enabled", 1):
                monitor["cache_refresh_running"] = True
        return await asyncio.to_thread(render_template, request, "index.html", {**context, "partial": "cards"})
    settings = get_settings()
    return await asyncio.to_thread(
        render_template,
        request,
        "index.html",
        {"settings": settings, "initial_cards_html": html, "partial": "cards-shell"},
    )


@app.get("/api/monitors/{monitor_id}/details", response_class=HTMLResponse)
async def monitor_detail_partial(request: Request, monitor_id: int, refresh: bool = False) -> HTMLResponse:
    html = await asyncio.to_thread(get_monitor_detail_html, request, monitor_id, refresh)
    if html is None:
        raise HTTPException(status_code=404, detail="Monitor not found")
    return HTMLResponse(html, headers={"Cache-Control": "private, max-age=15"})


@app.get("/api/monitor-details")
async def all_monitor_details(request: Request) -> JSONResponse:
    details = await asyncio.to_thread(build_all_monitor_detail_html, request)
    return JSONResponse({"details": details}, headers={"Cache-Control": "no-store"})


@app.get("/api/monitors")
async def monitor_snapshot() -> JSONResponse:
    monitors = list_monitors(include_heavy_details=False)
    for monitor in monitors:
        monitor["display_status"] = "paused" if not monitor.get("enabled", 1) else monitor["status"]
    summary = {
        "total": len(monitors),
        "up": sum(1 for monitor in monitors if monitor.get("enabled", 1) and monitor["status"] == "up"),
        "down": sum(1 for monitor in monitors if monitor.get("enabled", 1) and monitor["status"] == "down"),
        "unknown": sum(1 for monitor in monitors if monitor.get("enabled", 1) and monitor["status"] == "unknown"),
        "paused": sum(1 for monitor in monitors if not monitor.get("enabled", 1)),
        "categories": build_monitor_category_summary(monitors),
    }
    return JSONResponse({"summary": summary, "monitors": monitors})


@app.post("/monitors")
async def create_monitor_route(
    request: Request,
    name: str = Form(...),
    category: str = Form(""),
    categories: list[str] = Form([]),
    category_custom: str = Form(""),
    monitor_type: str = Form(...),
    target: str = Form(...),
    ping_target: str = Form(""),
    http_method: str = Form("GET"),
    retry_count: int = Form(2),
    interval: int = Form(...),
    timeout: int = Form(...),
    expected_text: str = Form(""),
    forbidden_text: str = Form(""),
) -> Response:
    if monitor_type not in {"http", "ping", "ping_http", "ping_http_or", "ping_http_and"}:
        raise HTTPException(status_code=400, detail="Unsupported monitor type")
    if http_method not in {"GET", "HEAD"}:
        raise HTTPException(status_code=400, detail="Unsupported HTTP method")
    target = normalize_monitor_target(monitor_type, target)
    is_combo = is_combo_monitor_type(monitor_type)
    ping_mode = combo_ping_mode(monitor_type)
    accept = (request.headers.get("accept") or "").lower()
    if is_combo and not ping_target.strip() and not urlparse(target).hostname:
        if "application/json" in accept:
            return JSONResponse(
                {"ok": False, "message": "Für PING/HTTP-Kombi bitte eine gültige HTTP-URL oder ein Ping-Ziel angeben."},
                status_code=400,
            )
        return flash_redirect("/", "Für PING/HTTP-Kombi bitte eine gültige HTTP-URL oder ein Ping-Ziel angeben.", "error")
    created_monitor = monitor_service.create(
        name=name,
        category=category,
        monitor_type="http" if is_combo else monitor_type,
        target=target,
        ping_enabled=is_combo,
        ping_mode=ping_mode,
        ping_target=ping_target,
        http_method=http_method,
        retry_count=max(0, min(5, retry_count)),
        interval=max(10, interval),
        timeout=max(2, timeout),
        expected_text=expected_text,
        forbidden_text=forbidden_text,
        categories=[*categories, category_custom],
    )
    monitor_id = int(created_monitor["id"])
    if "application/json" in accept:
        return JSONResponse(
            {
                "ok": True,
                "id": monitor_id,
                "name": name.strip(),
                "category": created_monitor.get("category", ""),
                "categories": created_monitor.get("categories", []),
                "target": target,
                "ping_target": ping_target.strip(),
                "monitor_type": "http" if is_combo else monitor_type,
                "type_label": ("PING + HTTP" if ping_mode == "and" else "PING oder HTTP") if is_combo else monitor_type.upper(),
                "interval": max(10, interval),
                "created_at": created_monitor.get("created_at"),
                "message": "Monitor wurde angelegt. Der erste Check läuft im Hintergrund.",
            }
        )
    return flash_redirect("/", "Monitor wurde angelegt. Der erste Check läuft im Hintergrund.")


@app.post("/monitors/{monitor_id}/edit")
async def edit_monitor_route(
    request: Request,
    monitor_id: int,
    name: str = Form(...),
    category: str = Form(""),
    categories: list[str] = Form([]),
    category_custom: str = Form(""),
    monitor_type: str = Form(...),
    target: str = Form(...),
    ping_target: str = Form(""),
    http_method: str = Form("GET"),
    retry_count: int = Form(2),
    interval: int = Form(...),
    timeout: int = Form(...),
    expected_text: str = Form(""),
    forbidden_text: str = Form(""),
) -> Response:
    monitor = monitor_repository.get(monitor_id)
    if not monitor:
        raise HTTPException(status_code=404, detail="Monitor not found")
    if monitor_type not in {"http", "ping", "ping_http", "ping_http_or", "ping_http_and"}:
        raise HTTPException(status_code=400, detail="Unsupported monitor type")
    if http_method not in {"GET", "HEAD"}:
        raise HTTPException(status_code=400, detail="Unsupported HTTP method")
    target = normalize_monitor_target(monitor_type, target)
    is_combo = is_combo_monitor_type(monitor_type)
    ping_mode = combo_ping_mode(monitor_type)
    accept = (request.headers.get("accept") or "").lower()
    if is_combo and not ping_target.strip() and not urlparse(target).hostname:
        if "application/json" in accept:
            return JSONResponse(
                {"ok": False, "message": "Für PING/HTTP-Kombi bitte eine gültige HTTP-URL oder ein Ping-Ziel angeben."},
                status_code=400,
            )
        return flash_redirect("/", "Für PING/HTTP-Kombi bitte eine gültige HTTP-URL oder ein Ping-Ziel angeben.", "error")

    updated_monitor = monitor_service.update(
        monitor_id=monitor_id,
        name=name,
        category=category,
        monitor_type="http" if is_combo else monitor_type,
        target=target,
        ping_enabled=is_combo,
        ping_mode=ping_mode,
        ping_target=ping_target,
        http_method=http_method,
        retry_count=max(0, min(5, retry_count)),
        interval=max(10, interval),
        timeout=max(2, timeout),
        expected_text=expected_text,
        forbidden_text=forbidden_text,
        categories=[*categories, category_custom],
    )
    if "application/json" in accept:
        return JSONResponse(
            {
                "ok": True,
                "id": monitor_id,
                "name": name.strip(),
                "category": updated_monitor.get("category", ""),
                "categories": updated_monitor.get("categories", []),
                "target": target,
                "ping_target": ping_target.strip(),
                "monitor_type": "http" if is_combo else monitor_type,
                "type_label": ("PING + HTTP" if ping_mode == "and" else "PING oder HTTP") if is_combo else monitor_type.upper(),
                "interval": max(10, interval),
                "created_at": monitor.get("created_at"),
                "message": "Monitor wurde aktualisiert. Der Check läuft im Hintergrund.",
            }
        )
    await asyncio.to_thread(get_dashboard_cards_html, True)
    return flash_redirect("/", "Monitor wurde aktualisiert.")


@app.post("/monitors/{monitor_id}/toggle")
async def toggle_monitor_route(monitor_id: int, request: Request):
    monitor = monitor_repository.get(monitor_id)
    if not monitor:
        raise HTTPException(status_code=404, detail="Monitor not found")
    is_enabled = monitor_service.toggle(monitor_id)
    message = "Monitor wurde fortgesetzt." if is_enabled else "Monitor wurde pausiert."
    accept = (request.headers.get("accept") or "").lower()
    if "application/json" in accept:
        return JSONResponse({"ok": True, "enabled": bool(is_enabled), "message": message})
    return flash_redirect("/", message, "info")


@app.post("/monitors/{monitor_id}/delete")
async def delete_monitor_route(monitor_id: int, request: Request) -> Response:
    monitor_service.delete(monitor_id)
    accept = (request.headers.get("accept") or "").lower()
    if "application/json" in accept:
        return JSONResponse({"ok": True, "id": monitor_id, "message": "Monitor wurde gelöscht."})
    return flash_redirect("/", "Monitor wurde gelöscht.", "warning")


@app.post("/monitors/{monitor_id}/run")
async def run_monitor_route(monitor_id: int, request: Request):
    result = await monitor_service.run(monitor_id)
    accept = (request.headers.get("accept") or "").lower()
    if "application/json" in accept:
        return JSONResponse(
            {
                "ok": bool(result),
                "status": result.get("status") if result else None,
                "message": "Monitor wurde geprüft.",
            },
            status_code=200 if result else 404,
        )
    return flash_redirect("/", "Monitor wurde geprüft.", "info")


@app.post("/settings/notifications")
async def update_notification_settings(
    keepup_base_url: str = Form(""),
    app_timezone: str = Form("UTC"),
    default_monitor_interval: int = Form(60),
    global_monitor_interval_override: int = Form(0),
    down_failures_threshold: int = Form(3),
    up_successes_threshold: int = Form(1),
    retention_days: int = Form(7),
    flapping_window_minutes: int = Form(15),
    flapping_transition_threshold: int = Form(3),
    notification_batch_window_seconds: int = Form(30),
    scheduler_jitter_seconds: int = Form(10),
    telegram_enabled: Optional[str] = Form(None),
    telegram_bot_token: str = Form(""),
    telegram_chat_id: str = Form(""),
    ntfy_enabled: Optional[str] = Form(None),
    ntfy_server_url: str = Form(""),
    ntfy_topic: str = Form(""),
    ntfy_token: str = Form(""),
    ntfy_username: str = Form(""),
    ntfy_password: str = Form(""),
    ntfy_priority: int = Form(3),
    smtp_enabled: Optional[str] = Form(None),
    smtp_host: str = Form(""),
    smtp_port: int = Form(587),
    smtp_username: str = Form(""),
    smtp_password: str = Form(""),
    smtp_from_email: str = Form(""),
    smtp_to_email: str = Form(""),
    smtp_use_tls: Optional[str] = Form(None),
    smtp_use_ssl: Optional[str] = Form(None),
    weather_location: Optional[str] = Form(None),
) -> RedirectResponse:
    try:
        payload = build_notification_settings_payload(
            keepup_base_url,
            app_timezone,
            default_monitor_interval,
            global_monitor_interval_override,
            down_failures_threshold,
            up_successes_threshold,
            retention_days,
            flapping_window_minutes,
            flapping_transition_threshold,
            notification_batch_window_seconds,
            scheduler_jitter_seconds,
            telegram_enabled,
            telegram_bot_token,
            telegram_chat_id,
            ntfy_enabled,
            ntfy_server_url,
            ntfy_topic,
            ntfy_token,
            ntfy_username,
            ntfy_password,
            ntfy_priority,
            smtp_enabled,
            smtp_host,
            smtp_port,
            smtp_username,
            smtp_password,
            smtp_from_email,
            smtp_to_email,
            smtp_use_tls,
            smtp_use_ssl,
            weather_location,
        )
    except ValueError as exc:
        return flash_redirect("/settings", str(exc), "error")
    update_settings(payload)
    invalidate_monitor_detail_cache()
    reschedule_monitor_jobs(scheduler)
    return flash_redirect("/settings", "Einstellungen wurden gespeichert.")


@app.post("/settings/test/telegram")
async def test_telegram_settings(
    keepup_base_url: str = Form(""),
    app_timezone: str = Form("UTC"),
    default_monitor_interval: int = Form(60),
    global_monitor_interval_override: int = Form(0),
    down_failures_threshold: int = Form(3),
    up_successes_threshold: int = Form(1),
    retention_days: int = Form(7),
    flapping_window_minutes: int = Form(15),
    flapping_transition_threshold: int = Form(3),
    notification_batch_window_seconds: int = Form(30),
    scheduler_jitter_seconds: int = Form(10),
    telegram_enabled: Optional[str] = Form(None),
    telegram_bot_token: str = Form(""),
    telegram_chat_id: str = Form(""),
    ntfy_enabled: Optional[str] = Form(None),
    ntfy_server_url: str = Form(""),
    ntfy_topic: str = Form(""),
    ntfy_token: str = Form(""),
    ntfy_username: str = Form(""),
    ntfy_password: str = Form(""),
    ntfy_priority: int = Form(3),
    smtp_enabled: Optional[str] = Form(None),
    smtp_host: str = Form(""),
    smtp_port: int = Form(587),
    smtp_username: str = Form(""),
    smtp_password: str = Form(""),
    smtp_from_email: str = Form(""),
    smtp_to_email: str = Form(""),
    smtp_use_tls: Optional[str] = Form(None),
    smtp_use_ssl: Optional[str] = Form(None),
    weather_location: Optional[str] = Form(None),
) -> RedirectResponse:
    try:
        payload = build_notification_settings_payload(
            keepup_base_url,
            app_timezone,
            default_monitor_interval,
            global_monitor_interval_override,
            down_failures_threshold,
            up_successes_threshold,
            retention_days,
            flapping_window_minutes,
            flapping_transition_threshold,
            notification_batch_window_seconds,
            scheduler_jitter_seconds,
            telegram_enabled,
            telegram_bot_token,
            telegram_chat_id,
            ntfy_enabled,
            ntfy_server_url,
            ntfy_topic,
            ntfy_token,
            ntfy_username,
            ntfy_password,
            ntfy_priority,
            smtp_enabled,
            smtp_host,
            smtp_port,
            smtp_username,
            smtp_password,
            smtp_from_email,
            smtp_to_email,
            smtp_use_tls,
            smtp_use_ssl,
            weather_location,
        )
    except ValueError as exc:
        return flash_redirect("/settings", str(exc), "error")
    update_settings(payload)
    invalidate_monitor_detail_cache()

    if not payload["telegram_bot_token"] or not payload["telegram_chat_id"]:
        return flash_redirect("/settings", "Bitte Bot-Token und Chat-ID für Telegram ausfüllen.", "error")

    try:
        await send_test_telegram_notification(payload)
    except Exception as exc:
        return flash_redirect("/settings", f"Telegram-Test fehlgeschlagen: {format_notification_error('telegram', exc)}", "error")

    return flash_redirect("/settings", "Telegram-Test wurde erfolgreich versendet.")


@app.post("/settings/test/ntfy-rich")
async def test_ntfy_rich_settings(
    keepup_base_url: str = Form(""),
    app_timezone: str = Form("UTC"),
    default_monitor_interval: int = Form(60),
    global_monitor_interval_override: int = Form(0),
    down_failures_threshold: int = Form(3),
    up_successes_threshold: int = Form(1),
    retention_days: int = Form(7),
    flapping_window_minutes: int = Form(15),
    flapping_transition_threshold: int = Form(3),
    notification_batch_window_seconds: int = Form(30),
    scheduler_jitter_seconds: int = Form(10),
    telegram_enabled: Optional[str] = Form(None),
    telegram_bot_token: str = Form(""),
    telegram_chat_id: str = Form(""),
    ntfy_enabled: Optional[str] = Form(None),
    ntfy_server_url: str = Form(""),
    ntfy_topic: str = Form(""),
    ntfy_token: str = Form(""),
    ntfy_username: str = Form(""),
    ntfy_password: str = Form(""),
    ntfy_priority: int = Form(3),
    smtp_enabled: Optional[str] = Form(None),
    smtp_host: str = Form(""),
    smtp_port: int = Form(587),
    smtp_username: str = Form(""),
    smtp_password: str = Form(""),
    smtp_from_email: str = Form(""),
    smtp_to_email: str = Form(""),
    smtp_use_tls: Optional[str] = Form(None),
    smtp_use_ssl: Optional[str] = Form(None),
    weather_location: Optional[str] = Form(None),
) -> RedirectResponse:
    try:
        payload = build_notification_settings_payload(
            keepup_base_url,
            app_timezone,
            default_monitor_interval,
            global_monitor_interval_override,
            down_failures_threshold,
            up_successes_threshold,
            retention_days,
            flapping_window_minutes,
            flapping_transition_threshold,
            notification_batch_window_seconds,
            scheduler_jitter_seconds,
            telegram_enabled,
            telegram_bot_token,
            telegram_chat_id,
            ntfy_enabled,
            ntfy_server_url,
            ntfy_topic,
            ntfy_token,
            ntfy_username,
            ntfy_password,
            ntfy_priority,
            smtp_enabled,
            smtp_host,
            smtp_port,
            smtp_username,
            smtp_password,
            smtp_from_email,
            smtp_to_email,
            smtp_use_tls,
            smtp_use_ssl,
            weather_location,
        )
    except ValueError as exc:
        return flash_redirect("/settings", str(exc), "error")
    update_settings(payload)
    invalidate_monitor_detail_cache()

    if not payload["ntfy_server_url"] or not payload["ntfy_topic"]:
        return flash_redirect("/settings", "Bitte ntfy Server-URL und Topic für den Layout-Test ausfüllen.", "error")

    try:
        await send_test_ntfy_rich_notification(payload)
    except Exception as exc:
        return flash_redirect("/settings", f"ntfy-Layout-Test fehlgeschlagen: {format_notification_error('ntfy', exc)}", "error")

    return flash_redirect("/settings", "ntfy-Layout-Test wurde erfolgreich versendet.")


@app.post("/settings/test/smtp")
async def test_smtp_settings(
    keepup_base_url: str = Form(""),
    app_timezone: str = Form("UTC"),
    default_monitor_interval: int = Form(60),
    global_monitor_interval_override: int = Form(0),
    down_failures_threshold: int = Form(3),
    up_successes_threshold: int = Form(1),
    retention_days: int = Form(7),
    flapping_window_minutes: int = Form(15),
    flapping_transition_threshold: int = Form(3),
    notification_batch_window_seconds: int = Form(30),
    scheduler_jitter_seconds: int = Form(10),
    telegram_enabled: Optional[str] = Form(None),
    telegram_bot_token: str = Form(""),
    telegram_chat_id: str = Form(""),
    ntfy_enabled: Optional[str] = Form(None),
    ntfy_server_url: str = Form(""),
    ntfy_topic: str = Form(""),
    ntfy_token: str = Form(""),
    ntfy_username: str = Form(""),
    ntfy_password: str = Form(""),
    ntfy_priority: int = Form(3),
    smtp_enabled: Optional[str] = Form(None),
    smtp_host: str = Form(""),
    smtp_port: int = Form(587),
    smtp_username: str = Form(""),
    smtp_password: str = Form(""),
    smtp_from_email: str = Form(""),
    smtp_to_email: str = Form(""),
    smtp_use_tls: Optional[str] = Form(None),
    smtp_use_ssl: Optional[str] = Form(None),
    weather_location: Optional[str] = Form(None),
) -> RedirectResponse:
    try:
        payload = build_notification_settings_payload(
            keepup_base_url,
            app_timezone,
            default_monitor_interval,
            global_monitor_interval_override,
            down_failures_threshold,
            up_successes_threshold,
            retention_days,
            flapping_window_minutes,
            flapping_transition_threshold,
            notification_batch_window_seconds,
            scheduler_jitter_seconds,
            telegram_enabled,
            telegram_bot_token,
            telegram_chat_id,
            ntfy_enabled,
            ntfy_server_url,
            ntfy_topic,
            ntfy_token,
            ntfy_username,
            ntfy_password,
            ntfy_priority,
            smtp_enabled,
            smtp_host,
            smtp_port,
            smtp_username,
            smtp_password,
            smtp_from_email,
            smtp_to_email,
            smtp_use_tls,
            smtp_use_ssl,
            weather_location,
        )
    except ValueError as exc:
        return flash_redirect("/settings", str(exc), "error")
    update_settings(payload)
    invalidate_monitor_detail_cache()

    if not payload["smtp_host"] or not payload["smtp_to_email"]:
        return flash_redirect("/settings", "Bitte SMTP-Host und Ziel-E-Mail für den SMTP-Test ausfüllen.", "error")

    try:
        await asyncio.to_thread(send_test_email_notification, payload)
    except Exception as exc:
        return flash_redirect("/settings", f"SMTP-Test fehlgeschlagen: {format_notification_error('smtp', exc)}", "error")

    return flash_redirect("/settings", "SMTP-Test wurde erfolgreich versendet.")


@app.get("/api/export")
async def export_configuration() -> JSONResponse:
    payload = export_backup()
    export_date = datetime.now().strftime("%Y-%m-%d")
    headers = {"Content-Disposition": f'attachment; filename="keepup-backup-{export_date}.json"'}
    return JSONResponse(content=payload, headers=headers)


@app.post("/api/import")
async def import_configuration(request: Request, file: UploadFile = File(...)) -> RedirectResponse:
    if not file.filename or not file.filename.endswith(".json"):
        return flash_redirect("/settings", "Bitte eine JSON-Backup-Datei auswählen.", "error")

    content_length = request.headers.get("content-length")
    if content_length and content_length.isdigit():
        # Multipart overhead is tiny, but allow one chunk of headroom to avoid false positives.
        if int(content_length) > MAX_IMPORT_BYTES + IMPORT_READ_CHUNK_BYTES:
            return flash_redirect(
                "/settings",
                f"Backup-Datei ist zu groß. Maximal erlaubt sind {MAX_IMPORT_BYTES // 1024 // 1024} MB.",
                "error",
            )

    try:
        content = await read_limited_upload(file)
    except ValueError as exc:
        return flash_redirect("/settings", str(exc), "error")

    try:
        payload = json.loads(content.decode("utf-8"))
    except json.JSONDecodeError as exc:
        return flash_redirect("/settings", f"Ungültige JSON-Datei: {exc}", "error")

    await asyncio.to_thread(import_backup, payload)
    invalidate_monitor_detail_cache()
    reschedule_monitor_jobs(scheduler)
    mark_dashboard_cards_cache_stale()
    asyncio.create_task(refresh_dashboard_cards_cache())
    asyncio.create_task(run_all_checks_once())
    return flash_redirect("/", "Backup wurde importiert. Checks laufen jetzt neu an.")


if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
