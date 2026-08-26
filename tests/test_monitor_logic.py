import unittest
from unittest.mock import AsyncMock, patch

import httpx

from monitor import (
    _notification_failure_reason,
    _ntfy_target_url,
    check_ping_http_target_raw,
    describe_http_error,
    send_ntfy_text,
    send_test_ntfy_rich_notification,
)


class HttpErrorReasonTests(unittest.TestCase):
    def test_read_timeout_reason_is_useful_when_exception_message_is_empty(self):
        request = httpx.Request("GET", "https://example.test")

        reason = describe_http_error(httpx.ReadTimeout("", request=request), 10)

        self.assertEqual(reason, "Zeitüberschreitung beim Lesen der Serverantwort (10 Sek.).")

    def test_notification_uses_error_category_when_detail_is_missing(self):
        reason = _notification_failure_reason({"error_msg": None, "error_category": "timeout"})

        self.assertEqual(reason, "Zeitüberschreitung")


class PingHttpModeTests(unittest.IsolatedAsyncioTestCase):
    async def test_or_mode_is_up_when_http_succeeds(self):
        monitor = {"target": "https://example.test/status", "timeout": 2, "ping_mode": "or"}

        with (
            patch("monitor.check_ping_target_raw", AsyncMock(return_value=("down", 20.0, "Ping failed", "ping"))),
            patch("monitor.check_http_target_raw", AsyncMock(return_value=("up", 120.0, None, None))),
        ):
            status, response_time, error, category = await check_ping_http_target_raw(monitor)

        self.assertEqual(status, "up")
        self.assertEqual(response_time, 120.0)
        self.assertIsNone(error)
        self.assertIsNone(category)

    async def test_or_mode_is_down_only_when_both_checks_fail(self):
        monitor = {"target": "https://example.test/status", "timeout": 2, "ping_mode": "or"}

        with (
            patch("monitor.check_ping_target_raw", AsyncMock(return_value=("down", 20.0, "Ping failed", "ping"))),
            patch("monitor.check_http_target_raw", AsyncMock(return_value=("down", 120.0, "HTTP-Status 502", "http_status"))),
        ):
            status, response_time, error, category = await check_ping_http_target_raw(monitor)

        self.assertEqual(status, "down")
        self.assertEqual(response_time, 120.0)
        self.assertIn("Ping (example.test): Ping failed", error)
        self.assertIn("HTTP: HTTP-Status 502", error)
        self.assertEqual(category, "ping")

    async def test_and_mode_requires_both_checks_to_succeed(self):
        monitor = {"target": "https://example.test/status", "timeout": 2, "ping_mode": "and"}

        with (
            patch("monitor.check_ping_target_raw", AsyncMock(return_value=("up", 20.0, None, None))),
            patch("monitor.check_http_target_raw", AsyncMock(return_value=("down", 120.0, "HTTP-Status 502", "http_status"))),
        ):
            status, response_time, error, category = await check_ping_http_target_raw(monitor)

        self.assertEqual(status, "down")
        self.assertEqual(response_time, 120.0)
        self.assertEqual(error, "HTTP: HTTP-Status 502")
        self.assertEqual(category, "http_status")

    async def test_and_mode_is_up_when_both_checks_succeed(self):
        monitor = {"target": "https://example.test/status", "timeout": 2, "ping_mode": "and"}

        with (
            patch("monitor.check_ping_target_raw", AsyncMock(return_value=("up", 20.0, None, None))),
            patch("monitor.check_http_target_raw", AsyncMock(return_value=("up", 120.0, None, None))),
        ):
            status, response_time, error, category = await check_ping_http_target_raw(monitor)

        self.assertEqual(status, "up")
        self.assertEqual(response_time, 120.0)
        self.assertIsNone(error)
        self.assertIsNone(category)


class NtfyNotificationTests(unittest.IsolatedAsyncioTestCase):
    def test_ntfy_target_url_joins_server_and_topic(self):
        url = _ntfy_target_url(
            {
                "ntfy_server_url": "https://ntfy.example.test/",
                "ntfy_topic": "/keepup-alerts/",
            }
        )

        self.assertEqual(url, "https://ntfy.example.test/keepup-alerts")

    async def test_rich_ntfy_test_uses_layout_headers(self):
        settings = {
            "ntfy_server_url": "https://ntfy.example.test",
            "ntfy_topic": "keepup",
            "ntfy_priority": 4,
            "keepup_base_url": "https://keepup.example.test",
            "app_timezone": "UTC",
        }

        with patch("monitor.send_ntfy_text", AsyncMock()) as send_mock:
            await send_test_ntfy_rich_notification(settings)

        args, kwargs = send_mock.call_args
        self.assertEqual(args[1], "KeepUp ntfy Layout-Test")
        self.assertIn("strukturierte ntfy-Nachricht", args[2])
        self.assertEqual(kwargs["tags"], "test_tube,sparkles,white_check_mark")

    async def test_ntfy_headers_are_ascii_safe_when_app_link_is_set(self):
        settings = {
            "ntfy_server_url": "https://ntfy.example.test",
            "ntfy_topic": "keepup",
            "ntfy_priority": 3,
            "keepup_base_url": "https://keepup.example.test",
        }

        class FakeResponse:
            def raise_for_status(self):
                return None

        class FakeClient:
            last_headers = {}

            def __init__(self, *args, **kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return None

            async def post(self, *args, **kwargs):
                FakeClient.last_headers = kwargs["headers"]
                return FakeResponse()

        with patch("monitor.httpx.AsyncClient", FakeClient):
            await send_ntfy_text(settings, "KeepUp ntfy Test", "Öffnen bleibt im Body erlaubt.")

        for value in FakeClient.last_headers.values():
            value.encode("ascii")
        self.assertIn("KeepUp oeffnen", FakeClient.last_headers["Actions"])


if __name__ == "__main__":
    unittest.main()
