from __future__ import annotations

import asyncio
import logging
import platform
import random
import socket
import smtplib
import ssl
import time
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from typing import Any, Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx
import html
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from database import get_monitor, get_settings, list_monitors, log_check_result


HTTP_USER_AGENT = "KeepUp/1.0"
MAX_CONCURRENT_HTTP_CHECKS = 8
HTTP_CONNECTION_LIMIT = 16
HTTP_KEEPALIVE_CONNECTION_LIMIT = 8
logger = logging.getLogger("keepup.monitor")
_notification_batch: list[tuple[dict[str, Any], dict[str, Any]]] = []
_notification_batch_task: Optional[asyncio.Task] = None
_notification_batch_lock = asyncio.Lock()
_http_check_client: Optional[httpx.AsyncClient] = None
_http_check_client_lock = asyncio.Lock()
_http_check_semaphore = asyncio.Semaphore(MAX_CONCURRENT_HTTP_CHECKS)


async def _ensure_http_check_client() -> httpx.AsyncClient:
    global _http_check_client
    if _http_check_client is not None:
        return _http_check_client

    async with _http_check_client_lock:
        if _http_check_client is None:
            _http_check_client = httpx.AsyncClient(
                headers={"User-Agent": HTTP_USER_AGENT},
                follow_redirects=True,
                limits=httpx.Limits(
                    max_connections=HTTP_CONNECTION_LIMIT,
                    max_keepalive_connections=HTTP_KEEPALIVE_CONNECTION_LIMIT,
                ),
            )
    return _http_check_client


async def init_monitor_runtime() -> None:
    await _ensure_http_check_client()


async def shutdown_monitor_runtime() -> None:
    global _http_check_client
    async with _http_check_client_lock:
        if _http_check_client is not None:
            await _http_check_client.aclose()
            _http_check_client = None


def categorize_monitor_error(message: Optional[str], status: str = "down") -> Optional[str]:
    if status == "up":
        return None
    if not message:
        return "unknown"
    lowered = message.lower()
    if "expected content missing" in lowered or "erwarteter inhalt" in lowered:
        return "content_mismatch"
    if "forbidden content" in lowered or "verbotener inhalt" in lowered:
        return "content_mismatch"
    if "http-status" in lowered:
        return "http_status"
    if "certificate" in lowered or "tls" in lowered or "ssl" in lowered:
        return "tls"
    if "dns" in lowered or "hostname" in lowered or "name resolution" in lowered or "aufgelöst" in lowered:
        return "dns"
    if "timeout" in lowered or "timed out" in lowered or "zeitüberschreitung" in lowered:
        return "timeout"
    if "refused" in lowered or "abgelehnt" in lowered:
        return "refused"
    if "network" in lowered or "route" in lowered or "netzwerk" in lowered:
        return "network"
    if "ping" in lowered:
        return "ping"
    return "unknown"


def normalize_monitor_error(message: Optional[str]) -> Optional[str]:
    if not message:
        return message

    lowered = message.lower()
    replacements = [
        ("nodename nor servname provided, or not known", "Hostname konnte nicht aufgelöst werden."),
        ("name or service not known", "Hostname konnte nicht aufgelöst werden."),
        ("temporary failure in name resolution", "DNS-Auflösung ist fehlgeschlagen."),
        ("all connection attempts failed", "Verbindung zum Ziel konnte nicht aufgebaut werden."),
        ("connection refused", "Verbindung wurde vom Zielsystem abgelehnt."),
        ("no route to host", "Kein Netzwerkpfad zum Ziel verfügbar."),
        ("network is unreachable", "Netzwerk ist nicht erreichbar."),
        ("operation timed out", "Zeitüberschreitung beim Verbindungsaufbau."),
        ("timed out", "Zeitüberschreitung beim Warten auf die Antwort."),
        ("ping failed or timed out", "Ping fehlgeschlagen oder Zeitlimit überschritten."),
    ]
    for needle, friendly in replacements:
        if needle in lowered:
            return friendly
    return message


def format_notification_error(channel: str, exc: Exception) -> str:
    message = str(exc).strip()

    if channel == "telegram":
        if isinstance(exc, httpx.HTTPStatusError):
            status = exc.response.status_code
            try:
                payload = exc.response.json()
                description = payload.get("description")
            except Exception:
                description = None
            if description:
                return f"Telegram API meldet Fehler {status}: {description}"
            return f"Telegram API meldet Fehler {status}."
        if isinstance(exc, (httpx.ConnectTimeout, httpx.ReadTimeout, httpx.WriteTimeout)):
            return "Zeitüberschreitung beim Kontaktieren der Telegram API."
        if isinstance(exc, httpx.ConnectError):
            return "Telegram API konnte nicht erreicht werden."
        return message or "Unbekannter Telegram-Fehler."

    if channel == "smtp":
        if isinstance(exc, smtplib.SMTPAuthenticationError):
            return "SMTP-Login fehlgeschlagen. Bitte Benutzername und Passwort prüfen."
        if isinstance(exc, smtplib.SMTPConnectError):
            return "Verbindung zum SMTP-Server konnte nicht aufgebaut werden."
        if isinstance(exc, smtplib.SMTPServerDisconnected):
            return "SMTP-Server hat die Verbindung unerwartet geschlossen."
        if isinstance(exc, smtplib.SMTPRecipientsRefused):
            return "Empfängeradresse wurde vom SMTP-Server abgelehnt."
        if isinstance(exc, smtplib.SMTPException):
            return message or "SMTP-Fehler beim Senden der E-Mail."
        if isinstance(exc, socket.gaierror):
            return "SMTP-Hostname konnte nicht aufgelöst werden."
        if isinstance(exc, TimeoutError):
            return "Zeitüberschreitung beim SMTP-Server."
        return message or "Unbekannter SMTP-Fehler."

    return message or "Unbekannter Fehler."


async def execute_monitor_check(monitor_id: int) -> Optional[dict[str, Any]]:
    monitor = await asyncio.to_thread(get_monitor, monitor_id)
    if not monitor:
        return None
    if not monitor.get("enabled", 1):
        return None

    logger.info(
        "check_start monitor_id=%s type=%s target=%s",
        monitor.get("id"),
        monitor.get("type"),
        monitor.get("target"),
    )

    retry_count = max(0, int(monitor.get("retry_count") or 0))
    total_attempts = 1 + retry_count
    error_category = None

    for attempt in range(total_attempts):
        if monitor["type"] == "http":
            status, response_time, error_message, error_category = await check_http_target_raw(monitor)
        else:
            status, response_time, error_message, error_category = await check_ping_target_raw(monitor)
        
        if status == "up":
            break
            
        if attempt < total_attempts - 1:
            logger.warning(
                "check_retry monitor_id=%s attempt=%s status=%s error=%s",
                monitor.get("id"),
                attempt + 1,
                status,
                (error_message or ""),
            )
            await asyncio.sleep(5)

    result = await asyncio.to_thread(
        log_check_result,
        monitor["id"],
        status,
        response_time,
        error_message,
        error_category,
    )

    if result["status_changed"]:
        logger.warning(
            "status_change monitor_id=%s %s->%s response_time=%s error=%s",
            monitor.get("id"),
            result.get("previous_status"),
            result.get("status"),
            result.get("response_time"),
            (result.get("error_msg") or ""),
        )
        await queue_status_change_notification(monitor, result)
    else:
        logger.info(
            "check_done monitor_id=%s status=%s response_time=%s",
            monitor.get("id"),
            result.get("status"),
            result.get("response_time"),
        )

    return result


async def check_http_target_raw(monitor: dict[str, Any]) -> tuple[str, float, Optional[str], Optional[str]]:
    start = time.perf_counter()
    timeout_seconds = float(monitor["timeout"])
    timeout = httpx.Timeout(timeout_seconds, pool=min(2.0, timeout_seconds))
    error_message = None
    status = "down"
    method = str(monitor.get("http_method") or "GET").upper()
    if method not in {"GET", "HEAD"}:
        method = "GET"

    try:
        client = await _ensure_http_check_client()
        async with _http_check_semaphore:
            response = await client.request(method, monitor["target"], timeout=timeout)
            if method == "HEAD" and response.status_code == 405:
                response = await client.get(monitor["target"], timeout=timeout)
        response_time = round((time.perf_counter() - start) * 1000, 2)
        if 200 <= response.status_code < 400:
            expected_text = str(monitor.get("expected_text") or "").strip()
            forbidden_text = str(monitor.get("forbidden_text") or "").strip()
            body_text = response.text if (expected_text or forbidden_text) else ""
            if expected_text and expected_text not in body_text:
                error_message = f"Erwarteter Inhalt nicht gefunden: {expected_text}"
                status = "down"
            elif forbidden_text and forbidden_text in body_text:
                error_message = f"Verbotener Inhalt gefunden: {forbidden_text}"
                status = "down"
            else:
                status = "up"
        else:
            error_message = f"HTTP-Status {response.status_code}"
    except httpx.HTTPError as exc:
        response_time = round((time.perf_counter() - start) * 1000, 2)
        error_message = normalize_monitor_error(str(exc))
    except Exception as exc:
        response_time = round((time.perf_counter() - start) * 1000, 2)
        error_message = normalize_monitor_error(f"Unerwarteter Fehler: {exc}")

    return status, response_time, error_message, categorize_monitor_error(error_message, status)


async def check_ping_target_raw(monitor: dict[str, Any]) -> tuple[str, float, Optional[str], Optional[str]]:
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
        error_message = None if status == "up" else normalize_monitor_error(_extract_ping_error(stdout, stderr))
    except Exception as exc:
        response_time = round((time.perf_counter() - start) * 1000, 2)
        status = "down"
        error_message = normalize_monitor_error(f"Ping-Fehler: {exc}")

    return status, response_time, error_message, categorize_monitor_error(error_message, status)


def _extract_ping_error(stdout: bytes, stderr: bytes) -> str:
    message = stderr.decode().strip() or stdout.decode().strip()
    if not message:
        return "Ping failed or timed out"
    return message.splitlines()[-1]



async def queue_status_change_notification(monitor: dict[str, Any], result: dict[str, Any]) -> None:
    settings = await asyncio.to_thread(get_settings)
    delay = max(0, int(settings.get("notification_batch_window_seconds") or 0))
    if delay <= 0:
        await send_status_change_notifications(monitor, result)
        return

    global _notification_batch_task
    async with _notification_batch_lock:
        _notification_batch.append((monitor.copy(), result.copy()))
        if _notification_batch_task is None or _notification_batch_task.done():
            _notification_batch_task = asyncio.create_task(_flush_notification_batch_after_delay(delay))


async def _flush_notification_batch_after_delay(delay: int) -> None:
    await asyncio.sleep(delay)
    async with _notification_batch_lock:
        items = list(_notification_batch)
        _notification_batch.clear()
    if not items:
        return
    if len(items) == 1:
        monitor, result = items[0]
        await send_status_change_notifications(monitor, result)
        return
    await send_batched_status_change_notifications(items)


async def send_batched_status_change_notifications(
    items: list[tuple[dict[str, Any], dict[str, Any]]],
) -> None:
    settings = await asyncio.to_thread(get_settings)
    tasks = []

    if settings.get("telegram_enabled") and settings.get("telegram_bot_token") and settings.get("telegram_chat_id"):
        tasks.append(send_telegram_batch_notification(settings, items))

    if settings.get("smtp_enabled") and settings.get("smtp_host") and settings.get("smtp_to_email"):
        tasks.append(asyncio.to_thread(send_email_batch_notification, settings, items))

    if tasks:
        results = await asyncio.gather(*tasks, return_exceptions=True)
        for res in results:
            if isinstance(res, Exception):
                logger.error("notification_batch_error error=%s", str(res))


def build_batch_notification_message(
    settings: dict[str, Any],
    items: list[tuple[dict[str, Any], dict[str, Any]]],
) -> tuple[str, str]:
    down_items = [(m, r) for m, r in items if r.get("status") == "down"]
    up_items = [(m, r) for m, r in items if r.get("status") == "up"]
    subject = f"KeepUp Sammelmeldung: {len(down_items)} DOWN, {len(up_items)} RECOVERED"
    lines = [subject, ""]
    if down_items:
        lines.append("DOWN:")
        for monitor, result in down_items:
            checked_at = format_timestamp_for_notification(result.get("checked_at", ""), settings.get("app_timezone", "UTC"))
            category = result.get("error_category") or "unknown"
            detail = result.get("error_msg") or "Keine Fehlermeldung verfügbar."
            lines.append(f"- {monitor.get('name')} ({category}) um {checked_at}: {detail}")
        lines.append("")
    if up_items:
        lines.append("RECOVERED:")
        for monitor, result in up_items:
            checked_at = format_timestamp_for_notification(result.get("checked_at", ""), settings.get("app_timezone", "UTC"))
            lines.append(f"- {monitor.get('name')} um {checked_at}")
        lines.append("")
    categories: dict[str, int] = {}
    for _monitor, result in items:
        category = str(result.get("error_category") or "recovery")
        categories[category] = categories.get(category, 0) + 1
    lines.append("Kategorien: " + ", ".join(f"{key}={value}" for key, value in sorted(categories.items())))
    lines.append("")
    lines.append("Powered by KeepUp")
    return subject, "\n".join(lines)


async def send_telegram_batch_notification(
    settings: dict[str, Any],
    items: list[tuple[dict[str, Any], dict[str, Any]]],
) -> None:
    subject, body = build_batch_notification_message(settings, items)
    text = html.escape(body).replace("\n", "\n")
    url = f"https://api.telegram.org/bot{settings['telegram_bot_token']}/sendMessage"
    payload = {
        "chat_id": settings["telegram_chat_id"],
        "text": f"<b>{html.escape(subject)}</b>\n\n{text}",
        "parse_mode": "HTML",
    }
    async with httpx.AsyncClient(timeout=10.0) as client:
        response = await client.post(url, json=payload)
        response.raise_for_status()
        data = response.json()
        if not data.get("ok", False):
            raise RuntimeError(str(data.get("description") or "Telegram API hat die Sammelmeldung abgelehnt."))


def send_email_batch_notification(
    settings: dict[str, Any],
    items: list[tuple[dict[str, Any], dict[str, Any]]],
) -> None:
    subject, body = build_batch_notification_message(settings, items)
    send_email_text(settings, subject, body)

async def send_status_change_notifications(monitor: dict[str, Any], result: dict[str, Any]) -> None:
    settings = await asyncio.to_thread(get_settings)
    tasks = []

    if settings.get("telegram_enabled") and settings.get("telegram_bot_token") and settings.get("telegram_chat_id"):
        tasks.append(send_telegram_notification(settings, monitor, result))

    if settings.get("smtp_enabled") and settings.get("smtp_host") and settings.get("smtp_to_email"):
        tasks.append(asyncio.to_thread(send_email_notification, settings, monitor, result))

    if tasks:
        results = await asyncio.gather(*tasks, return_exceptions=True)
        for res in results:
            if isinstance(res, Exception):
                logger.error(
                    "notification_error monitor_id=%s error=%s",
                    monitor.get("id"),
                    str(res),
                )


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
    return dt.astimezone(zone).strftime("%d.%m.%Y %H:%M:%S %Z")


def format_timestamp_without_tz(timestamp: str, timezone_name: str) -> str:
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
    # Do not include timezone abbreviation per user request
    return dt.astimezone(zone).strftime("%d.%m.%Y %H:%M:%S")


def build_notification_message(
    settings: dict[str, Any],
    monitor: dict[str, Any],
    result: dict[str, Any],
) -> tuple[str, str]:
    transition = f"{result['previous_status']} -> {result['status']}"
    is_recovered = result["status"] == "up"
    title_status = "RECOVERED" if is_recovered else "DOWN"
    subject = f"{monitor['name']} {title_status}"
    response_text = (
        f"{result['response_time']:.2f} ms" if result.get("response_time") is not None else "n/a"
    )
    reason = result.get("error_msg") or (
        "Wieder erreichbar." if is_recovered else "Keine Fehlermeldung verfügbar."
    )
    checked_at = format_timestamp_for_notification(result["checked_at"], settings.get("app_timezone", "UTC"))
    threshold = int(result.get("down_failures_threshold") or settings.get("down_failures_threshold") or 1)
    up_threshold = int(result.get("up_successes_threshold") or settings.get("up_successes_threshold") or 1)
    failures = int(result.get("consecutive_failures") or 0)
    successes = int(result.get("consecutive_successes") or 0)
    detection_note = (
        f"Recovery nach {successes}/{up_threshold} erfolgreichen Checks bestätigt."
        if is_recovered
        else f"DOWN erst nach {failures}/{threshold} bestätigten Fehlschlägen gemeldet."
    )
    category = result.get("error_category") or ("recovery" if is_recovered else "unknown")
    flapping_note = "Ja" if result.get("is_flapping") else "Nein"
    body = (
        f"Monitor: {monitor['name']}\n"
        f"Target: {monitor['target']}\n"
        f"Type: {monitor['type'].upper()}\n"
        f"Transition: {transition}\n"
        f"Checked at: {checked_at}\n"
        f"Response time: {response_text}\n"
        f"Detection: {detection_note}\n"
        f"Category: {category}\n"
        f"Flapping: {flapping_note}\n"
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
    # Build an HTML-formatted message suitable for Telegram
    # First line: icon + bold monitor name + status
    status_icon = "✅" if result.get("status") == "up" else "❌"
    status_text = "UP" if result.get("status") == "up" else "DOWN"

    # Escape user-provided fields to avoid HTML injection
    monitor_name = html.escape(str(monitor.get("name", "")))
    monitor_target = html.escape(str(monitor.get("target", "")))
    monitor_type = html.escape(str(monitor.get("type", "")).upper())
    transition = html.escape(f"{result.get('previous_status')} -> {result.get('status')}")
    threshold = int(result.get("down_failures_threshold") or settings.get("down_failures_threshold") or 1)
    up_threshold = int(result.get("up_successes_threshold") or settings.get("up_successes_threshold") or 1)
    failures = int(result.get("consecutive_failures") or 0)
    successes = int(result.get("consecutive_successes") or 0)
    detection_note = (
        f"Recovery bestätigt nach {successes}/{up_threshold} Erfolgen."
        if result.get("status") == "up"
        else f"DOWN bestätigt nach {failures}/{threshold} Fehlschlägen."
    )
    category = html.escape(str(result.get("error_category") or ("recovery" if result.get("status") == "up" else "unknown")))
    flapping_note = "Ja" if result.get("is_flapping") else "Nein"
    reason = result.get("error_msg") or (
        "Wieder erreichbar." if result.get("status") == "up" else "Keine Fehlermeldung verfügbar."
    )
    detection_note = html.escape(detection_note)
    reason = html.escape(str(reason))

    response_text = (
        f"{result['response_time']:.2f} ms" if result.get("response_time") is not None else "n/a"
    )
    response_text = html.escape(response_text)
    checked_at = format_timestamp_without_tz(result.get("checked_at", ""), settings.get("app_timezone", "UTC"))
    checked_at = html.escape(checked_at)

    # Compose Telegram HTML message. Note: Telegram does not support smaller font sizes,
    # so we use italics for the "Powered by" footer as a visual de-emphasis.
    telegram_lines = [
        f"{status_icon} <b>{monitor_name} {status_text}</b>",
        f"Monitor: {monitor_name}",
        f"Target: {monitor_target}",
        f"Type: {monitor_type}",
        f"Transition: {transition}",
        f"Checked at: {checked_at}",
        f"Response time: {response_text}",
        f"Detection: {detection_note}",
        f"Category: {category}",
        f"Flapping: {flapping_note}",
        f"Details: {reason}",
        "",
        "<i>Powered by KeepUp</i>",
    ]

    text = "\n".join(telegram_lines)

    url = f"https://api.telegram.org/bot{settings['telegram_bot_token']}/sendMessage"
    payload = {
        "chat_id": settings["telegram_chat_id"],
        "text": text,
        "parse_mode": "HTML",
    }

    async with httpx.AsyncClient(timeout=10.0) as client:
        response = await client.post(url, json=payload)
        response.raise_for_status()
        data = response.json()
        if not data.get("ok", False):
            raise RuntimeError(str(data.get("description") or "Telegram API hat die Nachricht abgelehnt."))


def send_email_notification(
    settings: dict[str, Any],
    monitor: dict[str, Any],
    result: dict[str, Any],
) -> None:
    subject, body = build_notification_message(settings, monitor, result)
    send_email_text(settings, subject, body)


def send_email_text(settings: dict[str, Any], subject: str, body: str) -> None:
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

    settings = get_settings()
    jitter_seconds = max(0, int(settings.get("scheduler_jitter_seconds") or 0))
    global_interval_override = max(0, int(settings.get("global_monitor_interval_override") or 0))
    monitors = list_monitors()
    for monitor in monitors:
        if not monitor.get("enabled", 1):
            continue
        interval_seconds = max(10, global_interval_override or int(monitor["interval"]))
        jitter = random.randint(0, jitter_seconds) if jitter_seconds else 0
        scheduler.add_job(
            execute_monitor_check,
            "interval",
            seconds=interval_seconds,
            next_run_time=datetime.now(timezone.utc) + timedelta(seconds=jitter),
            args=[monitor["id"]],
            id=f"monitor-{monitor['id']}",
            replace_existing=True,
            max_instances=1,
            coalesce=True,
            misfire_grace_time=interval_seconds,
        )


async def run_all_checks_once() -> None:
    monitors = await asyncio.to_thread(list_monitors)
    if not monitors:
        return
    await asyncio.gather(*(execute_monitor_check(monitor["id"]) for monitor in monitors if monitor.get("enabled", 1)))
