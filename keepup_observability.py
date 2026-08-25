from __future__ import annotations

import logging
import time
from typing import Any, Awaitable, Callable


class RequestTimingMiddleware:
    """Add time-to-first-byte telemetry without buffering streaming responses."""

    def __init__(self, app: Any, slow_request_seconds: float = 0.75) -> None:
        self.app = app
        self.slow_request_seconds = max(0.05, float(slow_request_seconds))
        self.logger = logging.getLogger("keepup.performance")

    async def __call__(
        self,
        scope: dict[str, Any],
        receive: Callable[[], Awaitable[dict[str, Any]]],
        send: Callable[[dict[str, Any]], Awaitable[None]],
    ) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        started_at = time.perf_counter()
        response_started = False

        async def send_with_timing(message: dict[str, Any]) -> None:
            nonlocal response_started
            if message.get("type") == "http.response.start":
                response_started = True
                duration_ms = (time.perf_counter() - started_at) * 1000.0
                headers = list(message.get("headers") or [])
                headers.append((b"server-timing", f"app;dur={duration_ms:.1f}".encode("ascii")))
                message["headers"] = headers
                if duration_ms >= self.slow_request_seconds * 1000.0:
                    self.logger.warning(
                        "slow_request method=%s path=%s status=%s duration_ms=%.1f",
                        scope.get("method"), scope.get("path"), message.get("status"), duration_ms,
                    )
            await send(message)

        try:
            await self.app(scope, receive, send_with_timing)
        except Exception:
            if not response_started:
                duration_ms = (time.perf_counter() - started_at) * 1000.0
                self.logger.exception(
                    "request_failed method=%s path=%s duration_ms=%.1f",
                    scope.get("method"), scope.get("path"), duration_ms,
                )
            raise
