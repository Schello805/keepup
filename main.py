from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from urllib.parse import urlencode
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import uvicorn
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from database import (
    cleanup_old_checks,
    create_monitor,
    delete_monitor,
    export_backup,
    get_monitor,
    get_recent_logs,
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
    reschedule_monitor_jobs,
    run_all_checks_once,
    send_test_email_notification,
    send_test_telegram_notification,
)


BASE_DIR = Path(__file__).resolve().parent
scheduler = AsyncIOScheduler()
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
    return {
        "app_timezone": normalize_timezone(app_timezone),
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
    for monitor in monitors:
        monitor["logs"] = get_recent_logs(monitor["id"])
        monitor["display_status"] = "paused" if not monitor.get("enabled", 1) else monitor["status"]
        monitor["last_checked_at"] = format_timestamp(monitor.get("last_checked_at"), app_timezone)
        monitor["last_change_at"] = format_timestamp(monitor.get("last_change_at"), app_timezone)
        for log in monitor["logs"]:
            log["checked_at"] = format_timestamp(log.get("checked_at"), app_timezone)

    down_count = sum(1 for monitor in monitors if monitor.get("enabled", 1) and monitor["status"] == "down")
    up_count = sum(1 for monitor in monitors if monitor.get("enabled", 1) and monitor["status"] == "up")
    unknown_count = sum(1 for monitor in monitors if monitor.get("enabled", 1) and monitor["status"] == "unknown")
    paused_count = sum(1 for monitor in monitors if not monitor.get("enabled", 1))
    overall_status = "All systems operational" if down_count == 0 else f"{down_count} issue(s) detected"
    overall_tone = "ok" if down_count == 0 else "problem"

    return {
        "request": request,
        "monitors": monitors,
        "settings": settings,
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
        },
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
        "timezone_options": timezone_options,
        "active_page": "settings",
        "toast": get_toast(request),
    }


@asynccontextmanager
async def lifespan(app: FastAPI):
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
    asyncio.create_task(run_all_checks_once())
    yield
    scheduler.shutdown(wait=False)


app = FastAPI(title="KeepUp", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))


@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request) -> HTMLResponse:
    return templates.TemplateResponse("index.html", build_dashboard_context(request))


@app.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request) -> HTMLResponse:
    return templates.TemplateResponse("settings.html", build_settings_context(request))


@app.get("/api/dashboard", response_class=HTMLResponse)
async def dashboard_partial(request: Request) -> HTMLResponse:
    return templates.TemplateResponse("index.html", {**build_dashboard_context(request), "partial": True})


@app.get("/api/live/top", response_class=HTMLResponse)
async def live_top_partial(request: Request) -> HTMLResponse:
    return templates.TemplateResponse("index.html", {**build_dashboard_context(request), "partial": "top"})


@app.get("/api/live/cards", response_class=HTMLResponse)
async def live_cards_partial(request: Request) -> HTMLResponse:
    return templates.TemplateResponse("index.html", {**build_dashboard_context(request), "partial": "cards"})


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
    interval: int = Form(...),
    timeout: int = Form(...),
) -> RedirectResponse:
    if monitor_type not in {"http", "ping"}:
        raise HTTPException(status_code=400, detail="Unsupported monitor type")
    monitor_id = create_monitor(
        name=name,
        monitor_type=monitor_type,
        target=target,
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
    interval: int = Form(...),
    timeout: int = Form(...),
) -> RedirectResponse:
    monitor = get_monitor(monitor_id)
    if not monitor:
        raise HTTPException(status_code=404, detail="Monitor not found")
    if monitor_type not in {"http", "ping"}:
        raise HTTPException(status_code=400, detail="Unsupported monitor type")

    update_monitor(
        monitor_id=monitor_id,
        name=name,
        monitor_type=monitor_type,
        target=target,
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
        return flash_redirect("/settings", f"Telegram-Test fehlgeschlagen: {exc}", "error")

    return flash_redirect("/settings", "Telegram-Test wurde erfolgreich versendet.")


@app.post("/settings/test/smtp")
async def test_smtp_settings(
    app_timezone: str = Form("UTC"),
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
        return flash_redirect("/settings", f"SMTP-Test fehlgeschlagen: {exc}", "error")

    return flash_redirect("/settings", "SMTP-Test wurde erfolgreich versendet.")


@app.get("/api/export")
async def export_configuration() -> JSONResponse:
    payload = export_backup()
    headers = {"Content-Disposition": 'attachment; filename="keepup-backup.json"'}
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
