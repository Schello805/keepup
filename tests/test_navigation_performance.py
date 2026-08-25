import time
import unittest
from unittest.mock import AsyncMock, patch

from starlette.requests import Request

import main


def make_request(path: str) -> Request:
    return Request(
        {
            "type": "http",
            "method": "GET",
            "path": path,
            "query_string": b"",
            "headers": [],
            "server": ("testserver", 80),
            "scheme": "http",
        }
    )


class NavigationPerformanceTests(unittest.IsolatedAsyncioTestCase):
    async def test_dashboard_shell_with_40_monitors_stays_below_500ms(self):
        request = make_request("/")
        summary = {
            "total": 40,
            "up": 38,
            "down": 1,
            "unknown": 1,
            "paused": 0,
            "categories": [],
            "overall_status": "1 issue(s) detected",
            "overall_tone": "problem",
            "last_updated_at": "25.08.2026 12:00:00",
        }
        context = {
            "request": request,
            "settings": {"refresh_interval": 30},
            "app_version": "1.0.test",
            "changelog_preview": [],
            "active_page": "dashboard",
            "toast": None,
            "summary": summary,
        }
        with (
            patch.object(main, "build_dashboard_shell_context", return_value=context),
            patch.object(main, "ensure_dashboard_snapshot_refresh", new=AsyncMock()),
        ):
            started = time.perf_counter()
            response = await main.dashboard(request)
            elapsed_ms = (time.perf_counter() - started) * 1000

        self.assertEqual(response.status_code, 200)
        self.assertIn("40", response.body.decode())
        self.assertLess(elapsed_ms, 500, f"Dashboard shell took {elapsed_ms:.1f} ms")

    async def test_incident_shell_with_40_monitors_stays_below_500ms(self):
        request = make_request("/incidents")
        monitors = [{"id": index, "name": f"Monitor {index}"} for index in range(1, 41)]
        context = {
            "request": request,
            "settings": {},
            "app_version": "1.0.test",
            "changelog_preview": [],
            "active_page": "incidents",
            "toast": None,
            "monitors": monitors,
            "monitor_count": len(monitors),
            "filters": {"monitor_id": None, "status": "all", "days": 7, "page": 1},
            "incident_feed_url": "/api/incidents/feed",
        }
        with patch.object(main, "build_incidents_shell_context", return_value=context):
            started = time.perf_counter()
            response = await main.incidents_page(request)
            elapsed_ms = (time.perf_counter() - started) * 1000

        self.assertEqual(response.status_code, 200)
        self.assertIn("Monitor 40", response.body.decode())
        self.assertLess(elapsed_ms, 500, f"Incident shell took {elapsed_ms:.1f} ms")

    async def test_settings_shell_stays_below_500ms(self):
        request = make_request("/settings")
        context = {
            "request": request,
            "settings": {},
            "app_version": "1.0.test",
            "changelog_preview": [],
            "system_metrics": {},
            "timezone_options": ["UTC", "Europe/Berlin"],
            "active_page": "settings",
            "toast": None,
        }
        with patch.object(main, "build_settings_context", return_value=context):
            started = time.perf_counter()
            response = await main.settings_page(request)
            elapsed_ms = (time.perf_counter() - started) * 1000

        self.assertEqual(response.status_code, 200)
        self.assertIn("Einstellungen", response.body.decode())
        self.assertLess(elapsed_ms, 500, f"Settings shell took {elapsed_ms:.1f} ms")


if __name__ == "__main__":
    unittest.main()
