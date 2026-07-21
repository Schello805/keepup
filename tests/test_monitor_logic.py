import unittest
from unittest.mock import AsyncMock, patch

from monitor import check_ping_http_target_raw


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


if __name__ == "__main__":
    unittest.main()
