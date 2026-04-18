from __future__ import annotations

import asyncio
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
from urllib.parse import urlencode
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import uvicorn
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
import httpx

from database import (
    cleanup_old_checks,
    create_monitor,
    delete_monitor,
    export_backup,
    get_db,
    get_monitor,
    get_monitor_summary,
    get_recent_logs_for_monitors,
    list_incidents,
    list_monitor_options,
    get_settings,
    import_backup,
    init_db,
    list_monitors,
    set_monitor_enabled,
    update_monitor,
    update_settings,
)
from monitor import (
    execute_monitor_check,
    format_notification_error,
    reschedule_monitor_jobs,
    run_all_checks_once,
    send_test_email_notification,
    send_test_telegram_notification,
)

from keepup_version import __version__


BASE_DIR = Path(__file__).resolve().parent
scheduler = AsyncIOScheduler()
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger("keepup")
UPDATE_STATUS_TTL_SECONDS = 60
_update_status_cache: dict[str, Any] = {"expires_at": 0.0, "payload": None}
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
    return dt.astimezone(get_timezone_or_utc(timezone_name)).strftime("%d.%m.%Y %H:%M:%S %Z")


def normalize_timezone(timezone_name: str) -> str:
    timezone_name = timezone_name.strip() or "UTC"
    try:
        ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError as exc:
        raise ValueError("Unbekannte Zeitzone. Bitte z. B. Europe/Berlin oder UTC verwenden.") from exc
    return timezone_name


def build_notification_settings_payload(
    app_timezone: str,
    down_failures_threshold: int,
    retention_days: int,
    telegram_enabled: Optional[str],
    telegram_bot_token: str,
    telegram_chat_id: str,
    smtp_enabled: Optional[str],
    smtp_host: str,
    smtp_port: int,
    smtp_username: str,
    smtp_password: str,
    smtp_from_email: str,
    smtp_to_email: str,
    smtp_use_tls: Optional[str],
    smtp_use_ssl: Optional[str],
) -> dict:
    down_failures_threshold = int(down_failures_threshold)
    retention_days = int(retention_days)
    if down_failures_threshold < 1:
        raise ValueError("Fehlschlag-Schwelle muss mindestens 1 sein.")
    if retention_days < 1:
        raise ValueError("Aufbewahrungszeit muss mindestens 1 Tag sein.")
    return {
        "app_timezone": normalize_timezone(app_timezone),
        "down_failures_threshold": down_failures_threshold,
        "retention_days": retention_days,
        "telegram_enabled": telegram_enabled == "on",
        "telegram_bot_token": telegram_bot_token.strip(),
        "telegram_chat_id": telegram_chat_id.strip(),
        "smtp_enabled": smtp_enabled == "on",
        "smtp_host": smtp_host.strip(),
        "smtp_port": smtp_port,
        "smtp_username": smtp_username.strip(),
        "smtp_password": smtp_password,
        "smtp_from_email": smtp_from_email.strip(),
        "smtp_to_email": smtp_to_email.strip(),
        "smtp_use_tls": smtp_use_tls == "on",
        "smtp_use_ssl": smtp_use_ssl == "on",
    }


def build_dashboard_context(request: Request) -> dict:
    monitors = list_monitors()
    settings = get_settings()
    app_timezone = settings.get("app_timezone", "UTC")
    logs_by_monitor = get_recent_logs_for_monitors([monitor["id"] for monitor in monitors])
    for monitor in monitors:
        monitor["logs"] = logs_by_monitor.get(monitor["id"], [])
        monitor["display_status"] = "paused" if not monitor.get("enabled", 1) else monitor["status"]
        monitor["last_checked_at"] = format_timestamp(monitor.get("last_checked_at"), app_timezone)
        monitor["last_change_at"] = format_timestamp(monitor.get("last_change_at"), app_timezone)
        monitor["last_success_at"] = format_timestamp(monitor.get("last_success_at"), app_timezone)
        for log in monitor["logs"]:
            log["checked_at"] = format_timestamp(log.get("checked_at"), app_timezone)

    down_count = sum(1 for monitor in monitors if monitor.get("enabled", 1) and monitor["status"] == "down")
    up_count = sum(1 for monitor in monitors if monitor.get("enabled", 1) and monitor["status"] == "up")
    unknown_count = sum(1 for monitor in monitors if monitor.get("enabled", 1) and monitor["status"] == "unknown")
    paused_count = sum(1 for monitor in monitors if not monitor.get("enabled", 1))
    overall_status = "All systems operational" if down_count == 0 else f"{down_count} issue(s) detected"
    overall_tone = "ok" if down_count == 0 else "problem"
    last_updated_at = format_timestamp(datetime.now(timezone.utc).replace(microsecond=0).isoformat(), app_timezone)

    return {
        "request": request,
        "monitors": monitors,
        "settings": settings,
        "app_version": get_app_version_display(),
        "active_page": "dashboard",
        "toast": get_toast(request),
        "summary": {
            "total": len(monitors),
            "up": up_count,
            "down": down_count,
            "unknown": unknown_count,
            "paused": paused_count,
            "overall_status": overall_status,
            "overall_tone": overall_tone,
            "last_updated_at": last_updated_at,
        },
    }


def build_dashboard_shell_context(request: Request) -> dict:
    settings = get_settings()
    summary = get_monitor_summary()
    app_timezone = settings.get("app_timezone", "UTC")
    overall_status = "All systems operational" if summary["down"] == 0 else f"{summary['down']} issue(s) detected"
    overall_tone = "ok" if summary["down"] == 0 else "problem"
    summary["overall_status"] = overall_status
    summary["overall_tone"] = overall_tone
    summary["last_updated_at"] = format_timestamp(datetime.now(timezone.utc).replace(microsecond=0).isoformat(), app_timezone)

    return {
        "request": request,
        "settings": settings,
        "app_version": get_app_version_display(),
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
        "timezone_options": timezone_options,
        "active_page": "settings",
        "toast": get_toast(request),
    }


def build_incidents_context(request: Request) -> dict:
    settings = get_settings()
    app_timezone = settings.get("app_timezone", "UTC")
    monitors = list_monitors()
    monitor_id, status, since_days, item_raw, page = parse_incident_filters(request)

    incidents = list_incidents(monitor_id=monitor_id, status=status, since_days=since_days)

    base_query: dict[str, str] = {}
    if monitor_id is not None:
        base_query["monitor_id"] = str(monitor_id)
    if status and status != "all":
        base_query["status"] = status
    base_query["days"] = "all" if since_days is None else str(since_days)

    feed_items: list[dict[str, Any]] = []

    for incident in incidents:
        incident["started_at_display"] = format_timestamp(incident.get("started_at"), app_timezone)
        incident["ended_at_display"] = format_timestamp(incident.get("ended_at"), app_timezone)

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
                "incident": incident,
                "select_url": incident.get("select_url"),
            }
        )

    if since_days is not None:
        cutoff_dt = datetime.now(timezone.utc) - timedelta(days=since_days)
    else:
        cutoff_dt = None

    for monitor in monitors:
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
    if query_string:
        incident_feed_url += f"?{query_string}"

    return {
        "request": request,
        "settings": settings,
        "app_version": get_app_version_display(),
        "active_page": "incidents",
        "toast": get_toast(request),
        "monitors": list_monitor_options(),
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
            timeout=3,
        )
        return (result.stdout or "").strip() or None
    except Exception:
        return None


def get_app_version_display() -> str:
    revision = _run_git_command(["git", "rev-list", "--count", "HEAD"])
    if revision and revision.isdigit():
        return f"{__version__} rev.{revision}"
    return __version__


def _schedule_self_restart(delay_seconds: float = 1.8) -> None:
    def _restart() -> None:
        os._exit(1)

    timer = threading.Timer(delay_seconds, _restart)
    timer.daemon = True
    timer.start()


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


async def _get_remote_main_sha() -> Optional[str]:
    url = "https://api.github.com/repos/Schello805/keepup/commits/main"
    headers = {"User-Agent": "KeepUp"}
    try:
        async with httpx.AsyncClient(timeout=6.0, headers=headers) as client:
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
    token = os.environ.get("KEEPUP_UPDATE_TOKEN", "").strip()
    update_enabled = bool(token)

    payload = {
        "current_version": get_app_version_display(),
        "local_sha": local_sha,
        "local_sha_short": (local_sha[:7] if local_sha else None),
        "remote_sha": remote_sha,
        "remote_sha_short": (remote_sha[:7] if remote_sha else None),
        "update_available": update_available,
        "update_enabled": update_enabled,
    }
    _update_status_cache["payload"] = payload
    _update_status_cache["expires_at"] = now + UPDATE_STATUS_TTL_SECONDS
    return payload


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("startup")
    init_db()
    scheduler.add_job(
        lambda: asyncio.Task(asyncio.to_thread(cleanup_old_checks)),
        "interval",
        hours=12,
        id="db-cleanup",
        replace_existing=True,
    )
    reschedule_monitor_jobs(scheduler)
    scheduler.start()
    async def _run_initial_checks() -> None:
        try:
            await run_all_checks_once()
        except Exception:
            logger.exception("initial_checks_failed")

    asyncio.create_task(_run_initial_checks())
    yield
    scheduler.shutdown(wait=False)
    logger.info("shutdown")


app = FastAPI(title="KeepUp", lifespan=lifespan)
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


@app.get("/health")
async def health() -> JSONResponse:
    return JSONResponse({"status": "ok"})


@app.get("/ready")
async def readiness() -> JSONResponse:
    db_ok = True
    db_error: Optional[str] = None
    try:
        conn = get_db()
        try:
            conn.execute("SELECT 1")
        finally:
            conn.close()
    except Exception as exc:
        db_ok = False
        db_error = str(exc)

    scheduler_running = bool(getattr(scheduler, "running", False))
    job_count = 0
    try:
        job_count = len(scheduler.get_jobs())
    except Exception:
        job_count = 0

    ready = db_ok and scheduler_running
    payload = {
        "ready": ready,
        "db": {"ok": db_ok, "error": db_error},
        "scheduler": {"running": scheduler_running, "jobs": job_count},
    }
    return JSONResponse(payload, status_code=200 if ready else 503)


@app.get("/api/update/status")
async def update_status() -> JSONResponse:
    payload = await get_cached_update_status_payload()
    return JSONResponse(payload)


@app.post("/api/update/run")
async def run_update() -> JSONResponse:
    expected = os.environ.get("KEEPUP_UPDATE_TOKEN", "").strip()
    if not expected:
        raise HTTPException(status_code=403, detail="Update ist nicht aktiviert (KEEPUP_UPDATE_TOKEN fehlt).")

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
            "restart_scheduled": restart_scheduled,
            "stdout": stdout,
            "stderr": stderr,
        },
        status_code=200 if ok else 500,
    )


@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request) -> HTMLResponse:
    return render_template(request, "index.html", build_dashboard_context(request))


@app.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request) -> HTMLResponse:
    return render_template(request, "settings.html", build_settings_context(request))


@app.get("/incidents", response_class=HTMLResponse)
async def incidents_page(request: Request) -> HTMLResponse:
    return render_template(request, "incidents.html", build_incidents_shell_context(request))


@app.get("/api/incidents/feed", response_class=HTMLResponse)
async def incidents_feed_partial(request: Request) -> HTMLResponse:
    return render_template(request, "incidents.html", {**build_incidents_context(request), "partial": "feed"})


@app.get("/api/dashboard", response_class=HTMLResponse)
async def dashboard_partial(request: Request) -> HTMLResponse:
    return render_template(request, "index.html", {**build_dashboard_context(request), "partial": True})


@app.get("/api/live/top", response_class=HTMLResponse)
async def live_top_partial(request: Request) -> HTMLResponse:
    return render_template(request, "index.html", {**build_dashboard_shell_context(request), "partial": "top"})


@app.get("/api/live/cards", response_class=HTMLResponse)
async def live_cards_partial(request: Request) -> HTMLResponse:
    return render_template(request, "index.html", {**build_dashboard_context(request), "partial": "cards"})


@app.get("/api/monitors")
async def monitor_snapshot() -> JSONResponse:
    monitors = list_monitors()
    for monitor in monitors:
        monitor["display_status"] = "paused" if not monitor.get("enabled", 1) else monitor["status"]
    summary = {
        "total": len(monitors),
        "up": sum(1 for monitor in monitors if monitor.get("enabled", 1) and monitor["status"] == "up"),
        "down": sum(1 for monitor in monitors if monitor.get("enabled", 1) and monitor["status"] == "down"),
        "unknown": sum(1 for monitor in monitors if monitor.get("enabled", 1) and monitor["status"] == "unknown"),
        "paused": sum(1 for monitor in monitors if not monitor.get("enabled", 1)),
    }
    return JSONResponse({"summary": summary, "monitors": monitors})


@app.post("/monitors")
async def create_monitor_route(
    name: str = Form(...),
    monitor_type: str = Form(...),
    target: str = Form(...),
    http_method: str = Form("GET"),
    retry_count: int = Form(2),
    interval: int = Form(...),
    timeout: int = Form(...),
) -> RedirectResponse:
    if monitor_type not in {"http", "ping"}:
        raise HTTPException(status_code=400, detail="Unsupported monitor type")
    if http_method not in {"GET", "HEAD"}:
        raise HTTPException(status_code=400, detail="Unsupported HTTP method")
    monitor_id = create_monitor(
        name=name,
        monitor_type=monitor_type,
        target=target,
        http_method=http_method,
        retry_count=max(0, min(5, retry_count)),
        interval=max(10, interval),
        timeout=max(2, timeout),
    )
    reschedule_monitor_jobs(scheduler)
    await execute_monitor_check(monitor_id)
    return flash_redirect("/", "Monitor wurde angelegt und sofort geprüft.")


@app.post("/monitors/{monitor_id}/edit")
async def edit_monitor_route(
    monitor_id: int,
    name: str = Form(...),
    monitor_type: str = Form(...),
    target: str = Form(...),
    http_method: str = Form("GET"),
    retry_count: int = Form(2),
    interval: int = Form(...),
    timeout: int = Form(...),
) -> RedirectResponse:
    monitor = get_monitor(monitor_id)
    if not monitor:
        raise HTTPException(status_code=404, detail="Monitor not found")
    if monitor_type not in {"http", "ping"}:
        raise HTTPException(status_code=400, detail="Unsupported monitor type")
    if http_method not in {"GET", "HEAD"}:
        raise HTTPException(status_code=400, detail="Unsupported HTTP method")

    update_monitor(
        monitor_id=monitor_id,
        name=name,
        monitor_type=monitor_type,
        target=target,
        http_method=http_method,
        retry_count=max(0, min(5, retry_count)),
        interval=max(10, interval),
        timeout=max(2, timeout),
    )
    reschedule_monitor_jobs(scheduler)
    return flash_redirect("/", "Monitor wurde aktualisiert.")


@app.post("/monitors/{monitor_id}/toggle")
async def toggle_monitor_route(monitor_id: int) -> RedirectResponse:
    monitor = get_monitor(monitor_id)
    if not monitor:
        raise HTTPException(status_code=404, detail="Monitor not found")
    is_enabled = not bool(monitor.get("enabled", 1))
    set_monitor_enabled(monitor_id, is_enabled)
    reschedule_monitor_jobs(scheduler)
    message = "Monitor wurde fortgesetzt." if is_enabled else "Monitor wurde pausiert."
    return flash_redirect("/", message, "info")


@app.post("/monitors/{monitor_id}/delete")
async def delete_monitor_route(monitor_id: int) -> RedirectResponse:
    delete_monitor(monitor_id)
    reschedule_monitor_jobs(scheduler)
    return flash_redirect("/", "Monitor wurde gelöscht.", "warning")


@app.post("/monitors/{monitor_id}/run")
async def run_monitor_route(monitor_id: int) -> RedirectResponse:
    await execute_monitor_check(monitor_id)
    return flash_redirect("/", "Manueller Check wurde gestartet.", "info")


@app.post("/settings/notifications")
async def update_notification_settings(
    app_timezone: str = Form("UTC"),
    down_failures_threshold: int = Form(3),
    retention_days: int = Form(7),
    telegram_enabled: Optional[str] = Form(None),
    telegram_bot_token: str = Form(""),
    telegram_chat_id: str = Form(""),
    smtp_enabled: Optional[str] = Form(None),
    smtp_host: str = Form(""),
    smtp_port: int = Form(587),
    smtp_username: str = Form(""),
    smtp_password: str = Form(""),
    smtp_from_email: str = Form(""),
    smtp_to_email: str = Form(""),
    smtp_use_tls: Optional[str] = Form(None),
    smtp_use_ssl: Optional[str] = Form(None),
) -> RedirectResponse:
    try:
        payload = build_notification_settings_payload(
            app_timezone,
            down_failures_threshold,
            retention_days,
            telegram_enabled,
            telegram_bot_token,
            telegram_chat_id,
            smtp_enabled,
            smtp_host,
            smtp_port,
            smtp_username,
            smtp_password,
            smtp_from_email,
            smtp_to_email,
            smtp_use_tls,
            smtp_use_ssl,
        )
    except ValueError as exc:
        return flash_redirect("/settings", str(exc), "error")
    update_settings(payload)
    return flash_redirect("/settings", "Einstellungen wurden gespeichert.")


@app.post("/settings/test/telegram")
async def test_telegram_settings(
    app_timezone: str = Form("UTC"),
    down_failures_threshold: int = Form(3),
    retention_days: int = Form(7),
    telegram_enabled: Optional[str] = Form(None),
    telegram_bot_token: str = Form(""),
    telegram_chat_id: str = Form(""),
    smtp_enabled: Optional[str] = Form(None),
    smtp_host: str = Form(""),
    smtp_port: int = Form(587),
    smtp_username: str = Form(""),
    smtp_password: str = Form(""),
    smtp_from_email: str = Form(""),
    smtp_to_email: str = Form(""),
    smtp_use_tls: Optional[str] = Form(None),
    smtp_use_ssl: Optional[str] = Form(None),
) -> RedirectResponse:
    try:
        payload = build_notification_settings_payload(
            app_timezone,
            down_failures_threshold,
            retention_days,
            telegram_enabled,
            telegram_bot_token,
            telegram_chat_id,
            smtp_enabled,
            smtp_host,
            smtp_port,
            smtp_username,
            smtp_password,
            smtp_from_email,
            smtp_to_email,
            smtp_use_tls,
            smtp_use_ssl,
        )
    except ValueError as exc:
        return flash_redirect("/settings", str(exc), "error")
    update_settings(payload)

    if not payload["telegram_bot_token"] or not payload["telegram_chat_id"]:
        return flash_redirect("/settings", "Bitte Bot-Token und Chat-ID für Telegram ausfüllen.", "error")

    try:
        await send_test_telegram_notification(payload)
    except Exception as exc:
        return flash_redirect("/settings", f"Telegram-Test fehlgeschlagen: {format_notification_error('telegram', exc)}", "error")

    return flash_redirect("/settings", "Telegram-Test wurde erfolgreich versendet.")


@app.post("/settings/test/smtp")
async def test_smtp_settings(
    app_timezone: str = Form("UTC"),
    down_failures_threshold: int = Form(3),
    retention_days: int = Form(7),
    telegram_enabled: Optional[str] = Form(None),
    telegram_bot_token: str = Form(""),
    telegram_chat_id: str = Form(""),
    smtp_enabled: Optional[str] = Form(None),
    smtp_host: str = Form(""),
    smtp_port: int = Form(587),
    smtp_username: str = Form(""),
    smtp_password: str = Form(""),
    smtp_from_email: str = Form(""),
    smtp_to_email: str = Form(""),
    smtp_use_tls: Optional[str] = Form(None),
    smtp_use_ssl: Optional[str] = Form(None),
) -> RedirectResponse:
    try:
        payload = build_notification_settings_payload(
            app_timezone,
            down_failures_threshold,
            retention_days,
            telegram_enabled,
            telegram_bot_token,
            telegram_chat_id,
            smtp_enabled,
            smtp_host,
            smtp_port,
            smtp_username,
            smtp_password,
            smtp_from_email,
            smtp_to_email,
            smtp_use_tls,
            smtp_use_ssl,
        )
    except ValueError as exc:
        return flash_redirect("/settings", str(exc), "error")
    update_settings(payload)

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
async def import_configuration(file: UploadFile = File(...)) -> RedirectResponse:
    if not file.filename or not file.filename.endswith(".json"):
        raise HTTPException(status_code=400, detail="Please upload a JSON backup file.")

    content = await file.read()
    try:
        payload = json.loads(content.decode("utf-8"))
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=400, detail=f"Invalid JSON file: {exc}") from exc

    await asyncio.to_thread(import_backup, payload)
    reschedule_monitor_jobs(scheduler)
    asyncio.create_task(run_all_checks_once())
    return flash_redirect("/", "Backup wurde importiert. Checks laufen jetzt neu an.")


if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
