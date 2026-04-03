from __future__ import annotations

import asyncio
import platform
import smtplib
import ssl
import time
from datetime import datetime, timezone
from email.message import EmailMessage
from typing import Any, Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from database import get_monitor, get_settings, list_monitors, log_check_result


HTTP_USER_AGENT = "KeepUp/1.0"


async def execute_monitor_check(monitor_id: int) -> Optional[dict[str, Any]]:
    monitor = await asyncio.to_thread(get_monitor, monitor_id)
    if not monitor:
        return None
    if not monitor.get("enabled", 1):
        return None

    if monitor["type"] == "http":
        result = await check_http_target(monitor)
    else:
        result = await check_ping_target(monitor)

    if result["status_changed"]:
        await send_status_change_notifications(monitor, result)

    return result


async def check_http_target(monitor: dict[str, Any]) -> dict[str, Any]:
    start = time.perf_counter()
    timeout = httpx.Timeout(monitor["timeout"])
    error_message = None
    status = "down"

    try:
        async with httpx.AsyncClient(
            timeout=timeout,
            headers={"User-Agent": HTTP_USER_AGENT},
            follow_redirects=True,
        ) as client:
            response = await client.get(monitor["target"])
        response_time = round((time.perf_counter() - start) * 1000, 2)
        if 200 <= response.status_code < 400:
            status = "up"
        else:
            error_message = f"HTTP status {response.status_code}"
    except httpx.HTTPError as exc:
        response_time = round((time.perf_counter() - start) * 1000, 2)
        error_message = str(exc)
    except Exception as exc:
        response_time = round((time.perf_counter() - start) * 1000, 2)
        error_message = f"Unexpected error: {exc}"

    return await asyncio.to_thread(
        log_check_result,
        monitor["id"],
        status,
        response_time,
        error_message,
    )


async def check_ping_target(monitor: dict[str, Any]) -> dict[str, Any]:
    start = time.perf_counter()
    system = platform.system().lower()
    timeout = int(monitor["timeout"])

    if system == "windows":
        command = ["ping", "-n", "1", "-w", str(timeout * 1000), monitor["target"]]
    elif system == "darwin":
        command = ["ping", "-c", "1", "-W", str(timeout), monitor["target"]]
    else:
        command = ["ping", "-c", "1", "-W", str(timeout), monitor["target"]]

    try:
        process = await asyncio.create_subprocess_exec(
            *command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await process.communicate()
        response_time = round((time.perf_counter() - start) * 1000, 2)

        status = "up" if process.returncode == 0 else "down"
        error_message = None if status == "up" else _extract_ping_error(stdout, stderr)
    except Exception as exc:
        response_time = round((time.perf_counter() - start) * 1000, 2)
        status = "down"
        error_message = f"Ping error: {exc}"

    return await asyncio.to_thread(
        log_check_result,
        monitor["id"],
        status,
        response_time,
        error_message,
    )


def _extract_ping_error(stdout: bytes, stderr: bytes) -> str:
    message = stderr.decode().strip() or stdout.decode().strip()
    if not message:
        return "Ping failed or timed out"
    return message.splitlines()[-1]


async def send_status_change_notifications(monitor: dict[str, Any], result: dict[str, Any]) -> None:
    settings = await asyncio.to_thread(get_settings)
    tasks = []

    if settings.get("telegram_enabled") and settings.get("telegram_bot_token") and settings.get("telegram_chat_id"):
        tasks.append(send_telegram_notification(settings, monitor, result))

    if settings.get("smtp_enabled") and settings.get("smtp_host") and settings.get("smtp_to_email"):
        tasks.append(asyncio.to_thread(send_email_notification, settings, monitor, result))

    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


def build_test_notification_payload(channel: str) -> tuple[dict[str, Any], dict[str, Any]]:
    checked_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    monitor = {
        "name": f"{channel.upper()} Test",
        "target": "keepup.local/test",
        "type": channel if channel in {"http", "ping"} else "system",
    }
    result = {
        "previous_status": "unknown",
        "status": "up",
        "response_time": 42.0,
        "checked_at": checked_at,
        "error_msg": "Dies ist eine manuell ausgelöste Testnachricht aus KeepUp.",
    }
    return monitor, result


def format_timestamp_for_notification(timestamp: str, timezone_name: str) -> str:
    try:
        dt = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    except ValueError:
        return timestamp
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    try:
        zone = ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError:
        zone = ZoneInfo("UTC")
    return dt.astimezone(zone).strftime("%Y-%m-%d %H:%M:%S %Z")


def build_notification_message(
    settings: dict[str, Any],
    monitor: dict[str, Any],
    result: dict[str, Any],
) -> tuple[str, str]:
    transition = f"{result['previous_status']} -> {result['status']}"
    title_status = "RECOVERED" if result["status"] == "up" else "DOWN"
    subject = f"{monitor['name']} {title_status}"
    response_text = (
        f"{result['response_time']:.2f} ms" if result.get("response_time") is not None else "n/a"
    )
    reason = result.get("error_msg") or "No error message"
    checked_at = format_timestamp_for_notification(result["checked_at"], settings.get("app_timezone", "UTC"))
    body = (
        f"Monitor: {monitor['name']}\n"
        f"Target: {monitor['target']}\n"
        f"Type: {monitor['type'].upper()}\n"
        f"Transition: {transition}\n"
        f"Checked at: {checked_at}\n"
        f"Response time: {response_text}\n"
        f"Details: {reason}\n\n"
        f"Powered by KeepUp"
    )
    return subject, body


async def send_test_telegram_notification(settings: dict[str, Any]) -> None:
    monitor, result = build_test_notification_payload("telegram")
    await send_telegram_notification(settings, monitor, result)


def send_test_email_notification(settings: dict[str, Any]) -> None:
    monitor, result = build_test_notification_payload("smtp")
    send_email_notification(settings, monitor, result)


async def send_telegram_notification(
    settings: dict[str, Any],
    monitor: dict[str, Any],
    result: dict[str, Any],
) -> None:
    subject, body = build_notification_message(settings, monitor, result)
    url = f"https://api.telegram.org/bot{settings['telegram_bot_token']}/sendMessage"
    payload = {
        "chat_id": settings["telegram_chat_id"],
        "text": f"{subject}\n\n{body}",
    }

    async with httpx.AsyncClient(timeout=10.0) as client:
        await client.post(url, json=payload)


def send_email_notification(
    settings: dict[str, Any],
    monitor: dict[str, Any],
    result: dict[str, Any],
) -> None:
    subject, body = build_notification_message(settings, monitor, result)

    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = settings.get("smtp_from_email") or settings.get("smtp_username") or "keepup@localhost"
    message["To"] = settings["smtp_to_email"]
    message.set_content(body)

    host = settings["smtp_host"]
    port = int(settings.get("smtp_port", 587))
    username = settings.get("smtp_username") or None
    password = settings.get("smtp_password") or None

    if settings.get("smtp_use_ssl"):
        context = ssl.create_default_context()
        with smtplib.SMTP_SSL(host, port, context=context, timeout=10) as server:
            if username and password:
                server.login(username, password)
            server.send_message(message)
        return

    with smtplib.SMTP(host, port, timeout=10) as server:
        server.ehlo()
        if settings.get("smtp_use_tls"):
            context = ssl.create_default_context()
            server.starttls(context=context)
            server.ehlo()
        if username and password:
            server.login(username, password)
        server.send_message(message)


def reschedule_monitor_jobs(scheduler: AsyncIOScheduler) -> None:
    for job in list(scheduler.get_jobs()):
        if job.id.startswith("monitor-"):
            scheduler.remove_job(job.id)

    monitors = list_monitors()
    for monitor in monitors:
        if not monitor.get("enabled", 1):
            continue
        scheduler.add_job(
            execute_monitor_check,
            "interval",
            seconds=max(10, int(monitor["interval"])),
            args=[monitor["id"]],
            id=f"monitor-{monitor['id']}",
            replace_existing=True,
            max_instances=1,
            coalesce=True,
            misfire_grace_time=max(10, int(monitor["interval"])),
        )


async def run_all_checks_once() -> None:
    monitors = await asyncio.to_thread(list_monitors)
    if not monitors:
        return
    await asyncio.gather(*(execute_monitor_check(monitor["id"]) for monitor in monitors if monitor.get("enabled", 1)))
