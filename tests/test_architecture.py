import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

import database
from keepup_routes_system import router as system_router
from keepup_routes_navigation import router as navigation_router
from keepup_cache import DashboardCacheStore
from keepup_formatting import format_duration_compact, format_timestamp
from keepup_observability import RequestTimingMiddleware


class ArchitectureTests(unittest.TestCase):
    def test_system_endpoints_are_registered_from_dedicated_router(self):
        paths = {route.path for route in system_router.routes}
        self.assertIn("/health", paths)
        self.assertIn("/ready", paths)

    def test_navigation_pages_are_registered_from_dedicated_router(self):
        paths = {route.path for route in navigation_router.routes}
        self.assertEqual(paths, {"/", "/wall", "/settings", "/incidents", "/changelog"})

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

    def test_check_retention_cleans_down_monitors_in_small_batches(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "keepup.db"
            with patch.object(database, "DATABASE_URL", db_path):
                database.init_db()
                old = (datetime.now(timezone.utc) - timedelta(days=30)).replace(microsecond=0).isoformat()
                recent = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
                with database.get_db() as conn:
                    monitor_id = conn.execute(
                        "INSERT INTO monitors (name, type, target, status) VALUES (?, ?, ?, ?)",
                        ("Down target", "ping", "127.0.0.2", "down"),
                    ).lastrowid
                    conn.executemany(
                        "INSERT INTO checks (monitor_id, status, checked_at) VALUES (?, ?, ?)",
                        [(monitor_id, "down", old), (monitor_id, "down", old), (monitor_id, "down", recent)],
                    )
                    conn.commit()

                deleted = database.cleanup_old_checks(days=7, batch_size=100)

                with database.get_db() as conn:
                    remaining = conn.execute("SELECT checked_at FROM checks ORDER BY id").fetchall()
                self.assertEqual(deleted, 2)
                self.assertEqual([row["checked_at"] for row in remaining], [recent])

    def test_incident_check_foreign_keys_are_indexed_for_cleanup(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "keepup.db"
            with patch.object(database, "DATABASE_URL", db_path):
                database.init_db()
                with database.get_db() as conn:
                    indexes = {row["name"] for row in conn.execute("PRAGMA index_list('incidents')")}

                self.assertIn("idx_incidents_start_check", indexes)
                self.assertIn("idx_incidents_end_check", indexes)

    def test_database_metrics_do_not_scan_application_tables(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "keepup.db"
            with patch.object(database, "DATABASE_URL", db_path):
                database.init_db()
                database.update_settings({"retention_days": 14})
                metrics = database.get_database_metrics(cache_seconds=1)

                self.assertTrue(metrics["ok"])
                self.assertEqual(metrics["retention_days"], 14)
                self.assertEqual(metrics["journal_mode"], "WAL")
                self.assertRegex(metrics["engine"], r"^SQLite \d+")
                self.assertIsNotNone(metrics["response_time_ms"])
                self.assertIsNotNone(metrics["reusable_percent"])
                self.assertNotEqual(metrics["active_size"], "-")
                self.assertNotEqual(metrics["disk_free"], "-")
                self.assertNotEqual(metrics["database_size"], "-")

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
        self.assertIn("playSoundSequence", core)


if __name__ == "__main__":
    unittest.main()
