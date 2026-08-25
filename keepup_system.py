from __future__ import annotations

import time
from typing import Any, Optional

from keepup_formatting import format_bytes_compact


_metrics_cache: dict[str, Any] = {
    "timestamp": None,
    "cpu_total": None,
    "cpu_idle": None,
    "bytes_sent": None,
    "bytes_recv": None,
}


def _read_linux_cpu_times() -> tuple[Optional[int], Optional[int]]:
    try:
        with open("/proc/stat", "r", encoding="utf-8") as fh:
            first = fh.readline().strip().split()
        if not first or first[0] != "cpu":
            return None, None
        values = [int(value) for value in first[1:]]
        total = sum(values)
        idle = values[3] + (values[4] if len(values) > 4 else 0)
        return total, idle
    except Exception:
        return None, None


def _read_linux_memory() -> tuple[Optional[int], Optional[int], Optional[float]]:
    try:
        meminfo: dict[str, int] = {}
        with open("/proc/meminfo", "r", encoding="utf-8") as fh:
            for line in fh:
                key, rest = line.split(":", 1)
                meminfo[key] = int(rest.strip().split()[0]) * 1024
        total = meminfo.get("MemTotal")
        available = meminfo.get("MemAvailable")
        if total is None or available is None:
            return None, None, None
        used = max(0, total - available)
        percent = (used / total) * 100 if total else None
        return used, total, percent
    except Exception:
        return None, None, None


def _read_linux_net_bytes() -> tuple[Optional[int], Optional[int]]:
    try:
        recv_total = 0
        sent_total = 0
        with open("/proc/net/dev", "r", encoding="utf-8") as fh:
            for line in fh.readlines()[2:]:
                iface, data = line.split(":", 1)
                if iface.strip() == "lo":
                    continue
                fields = data.split()
                recv_total += int(fields[0])
                sent_total += int(fields[8])
        return sent_total, recv_total
    except Exception:
        return None, None


def build_system_metrics() -> dict[str, Any]:
    now = time.time()
    cpu_total, cpu_idle = _read_linux_cpu_times()
    memory_used, memory_total, memory_percent = _read_linux_memory()
    bytes_sent, bytes_recv = _read_linux_net_bytes()

    previous_timestamp = _metrics_cache.get("timestamp")
    previous_cpu_total = _metrics_cache.get("cpu_total")
    previous_cpu_idle = _metrics_cache.get("cpu_idle")
    previous_sent = _metrics_cache.get("bytes_sent")
    previous_recv = _metrics_cache.get("bytes_recv")

    cpu_percent: Optional[float] = None
    if (
        cpu_total is not None
        and cpu_idle is not None
        and previous_cpu_total is not None
        and previous_cpu_idle is not None
        and cpu_total > int(previous_cpu_total)
    ):
        total_delta = cpu_total - int(previous_cpu_total)
        idle_delta = cpu_idle - int(previous_cpu_idle)
        if total_delta > 0:
            cpu_percent = max(0.0, min(100.0, (1 - (idle_delta / total_delta)) * 100))

    upload_rate: Optional[float] = None
    download_rate: Optional[float] = None
    if (
        previous_timestamp is not None
        and previous_sent is not None
        and previous_recv is not None
        and bytes_sent is not None
        and bytes_recv is not None
        and now > float(previous_timestamp)
    ):
        elapsed = max(0.001, now - float(previous_timestamp))
        upload_rate = max(0.0, (bytes_sent - float(previous_sent)) / elapsed)
        download_rate = max(0.0, (bytes_recv - float(previous_recv)) / elapsed)

    _metrics_cache.update(
        timestamp=now,
        cpu_total=cpu_total,
        cpu_idle=cpu_idle,
        bytes_sent=bytes_sent,
        bytes_recv=bytes_recv,
    )
    return {
        "cpu_percent": round(cpu_percent, 1) if cpu_percent is not None else None,
        "memory_percent": round(memory_percent, 1) if memory_percent is not None else None,
        "memory_used": format_bytes_compact(memory_used),
        "memory_total": format_bytes_compact(memory_total),
        "net_sent_total": format_bytes_compact(bytes_sent),
        "net_recv_total": format_bytes_compact(bytes_recv),
        "net_upload_rate": format_bytes_compact(upload_rate) + "/s" if upload_rate is not None else "-",
        "net_download_rate": format_bytes_compact(download_rate) + "/s" if download_rate is not None else "-",
    }
