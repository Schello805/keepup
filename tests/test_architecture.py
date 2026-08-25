import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

import database
from keepup_routes_system import router as system_router
from keepup_cache import DashboardCacheStore
from keepup_formatting import format_duration_compact, format_timestamp
from keepup_observability import RequestTimingMiddleware


class ArchitectureTests(unittest.TestCase):
    def test_system_endpoints_are_registered_from_dedicated_router(self):
        paths = {route.path for route in system_router.routes}
        self.assertIn("/health", paths)
        self.assertIn("/ready", paths)

    def test_dashboard_cache_store_versions_and_resets_all_views(self):
        cache = DashboardCacheStore()
        cache.cards.update(expires_at=10.0, html="cards", generation=3)
        cache.snapshot.update(version=2, expires_at=10.0, payload={"ok": True})
        cache.status_wall.update(version=2, expires_at=10.0, payload={"ok": True})

        self.assertEqual(cache.advance_version(), 2)
        cache.reset()

        self.assertEqual(cache.version(), 2)
        self.assertIsNone(cache.cards["html"])
        self.assertIsNone(cache.snapshot["payload"])
        self.assertIsNone(cache.status_wall["payload"])

    def test_schema_version_is_recorded_idempotently(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "keepup.db"
            with patch.object(database, "DATABASE_URL", db_path):
                database.init_db()
                database.init_db()
                self.assertEqual(database.get_schema_version(), database.CURRENT_SCHEMA_VERSION)
                with database.get_db() as conn:
                    count = conn.execute("SELECT COUNT(*) FROM schema_migrations").fetchone()[0]
                self.assertEqual(count, 1)

    def test_request_timing_middleware_adds_server_timing_header(self):
        app = FastAPI()
        app.add_middleware(RequestTimingMiddleware, slow_request_seconds=10)

        @app.get("/health")
        async def health():
            return {"status": "ok"}

        response = TestClient(app).get("/health")
        self.assertEqual(response.status_code, 200)
        self.assertRegex(response.headers.get("server-timing", ""), r"^app;dur=\d+\.\d$")

    def test_shared_formatting_module_keeps_german_output(self):
        self.assertEqual(format_duration_compact(90061), "1 Tage 1 Std.")
        self.assertEqual(format_timestamp("2026-08-25T08:30:00+00:00", "UTC"), "25.08.2026 08:30:00")

    def test_frontend_dependencies_are_served_locally(self):
        project_root = Path(__file__).resolve().parents[1]
        template = (project_root / "templates" / "index.html").read_text(encoding="utf-8")
        self.assertIn('src="/static/htmx.min.js"', template)
        self.assertNotIn("unpkg.com/htmx", template)
        self.assertTrue((project_root / "static" / "htmx.min.js").is_file())
        core = (project_root / "static" / "dashboard-core.js").read_text(encoding="utf-8")
        self.assertIn("compareMonitorCards", core)


if __name__ == "__main__":
    unittest.main()
