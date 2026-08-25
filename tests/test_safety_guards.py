import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import database
from main import build_notification_settings_payload, read_limited_upload


class ChunkedUpload:
    def __init__(self, chunks):
        self._chunks = list(chunks)

    async def read(self, _size):
        if not self._chunks:
            return b""
        return self._chunks.pop(0)


class SafetyGuardTests(unittest.IsolatedAsyncioTestCase):
    def test_wal_mode_is_only_configured_during_database_initialization(self):
        source = (Path(__file__).parents[1] / "database.py").read_text(encoding="utf-8")
        get_db_source = source.split("def get_db()", 1)[1].split("def init_db()", 1)[0]
        init_db_source = source.split("def init_db()", 1)[1].split("def _ensure_monitor_columns", 1)[0]
        self.assertNotIn("journal_mode", get_db_source.lower())
        self.assertIn("pragma journal_mode = wal", init_db_source.lower())

    def test_dashboard_does_not_use_bulk_detail_prefetch(self):
        template = (Path(__file__).parents[1] / "templates" / "index.html").read_text(encoding="utf-8")
        self.assertNotIn('fetch("/api/monitor-details"', template)

    def test_active_filter_hint_is_a_floating_dashboard_button(self):
        template = (Path(__file__).parents[1] / "templates" / "index.html").read_text(encoding="utf-8")
        self.assertIn('id="active-dashboard-filters" type="button"', template)
        self.assertIn("fixed right-0 top-1/2", template)
        self.assertIn("-translate-y-1/2", template)
        self.assertIn("outline-none", template)
        self.assertIn("focus-visible:ring-inset", template)
        filter_button = template.split('id="active-dashboard-filters"', 1)[1].split(">", 1)[0]
        self.assertNotIn(" border ", filter_button)
        self.assertIn('document.body.appendChild(shell)', template)
        self.assertIn('document.body.appendChild(nextFilter)', template)
        self.assertIn('onclick="clearDashboardFilters()"', template)

    def test_status_wall_supports_system_light_and_dark_themes(self):
        template = (Path(__file__).parents[1] / "templates" / "status_wall.html").read_text(encoding="utf-8")
        self.assertIn('id="wall-theme-toggle"', template)
        self.assertIn('["system", "light", "dark"]', template)
        self.assertIn('window.matchMedia("(prefers-color-scheme: dark)")', template)
        self.assertIn('body[data-wall-theme="light"]', template)
        self.assertIn('#wall-summary .text-emerald-200', template)
        self.assertIn('id="wall-size-controls"', template)
        self.assertIn('window.localStorage.setItem("keepupWallTheme", selected)', template)

    def test_card_click_restarts_stale_detail_request_with_loading_bar(self):
        template = (Path(__file__).parents[1] / "templates" / "index.html").read_text(encoding="utf-8")
        self.assertIn("window.__keepupDetailControllers.get(key).abort()", template)
        self.assertIn("detail-loading-bar", template)
        self.assertIn("loadMonitorDetailHtml(monitorId, Boolean(cached", template)
        self.assertIn(", true);", template)

    def test_blank_secret_fields_keep_existing_values(self):
        with patch(
            "main.get_settings",
            return_value={
                "telegram_bot_token": "existing-telegram-token",
                "ntfy_token": "existing-ntfy-token",
                "ntfy_password": "existing-ntfy-password",
                "smtp_password": "existing-smtp-password",
            },
        ):
            payload = build_notification_settings_payload(
                keepup_base_url="",
                app_timezone="UTC",
                default_monitor_interval=60,
                global_monitor_interval_override=0,
                down_failures_threshold=3,
                up_successes_threshold=1,
                retention_days=7,
                flapping_window_minutes=15,
                flapping_transition_threshold=3,
                notification_batch_window_seconds=30,
                scheduler_jitter_seconds=10,
                telegram_enabled="on",
                telegram_bot_token="",
                telegram_chat_id="123",
                ntfy_enabled="on",
                ntfy_server_url="https://ntfy.example.test",
                ntfy_topic="keepup",
                ntfy_token="",
                ntfy_username="keepup",
                ntfy_password="",
                ntfy_priority=3,
                smtp_enabled="on",
                smtp_host="smtp.example.test",
                smtp_port=587,
                smtp_username="keepup@example.test",
                smtp_password="",
                smtp_from_email="keepup@example.test",
                smtp_to_email="admin@example.test",
                smtp_use_tls="on",
                smtp_use_ssl=None,
            )

        self.assertEqual(payload["telegram_bot_token"], "existing-telegram-token")
        self.assertEqual(payload["ntfy_token"], "existing-ntfy-token")
        self.assertEqual(payload["ntfy_password"], "existing-ntfy-password")
        self.assertEqual(payload["smtp_password"], "existing-smtp-password")

    async def test_limited_upload_rejects_oversized_content(self):
        upload = ChunkedUpload([b"a" * 6, b"b" * 6])

        with self.assertRaises(ValueError):
            await read_limited_upload(upload, max_bytes=10)

    async def test_limited_upload_accepts_content_within_limit(self):
        upload = ChunkedUpload([b"{", b"}"])

        content = await read_limited_upload(upload, max_bytes=10)

        self.assertEqual(content, b"{}")

    def test_ntfy_settings_are_exported_without_secrets_and_imported(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "keepup-test.db"
            with patch.object(database, "DATABASE_URL", db_path):
                database.init_db()
                database.update_settings(
                    {
                        "ntfy_enabled": True,
                        "ntfy_server_url": "https://ntfy.example.test",
                        "ntfy_topic": "keepup-alerts",
                        "ntfy_username": "keepup",
                        "ntfy_token": "secret-token",
                        "ntfy_password": "secret-password",
                        "ntfy_priority": 4,
                    }
                )

                payload = database.export_backup()

                exported_settings = payload["settings"]
                self.assertTrue(exported_settings["ntfy_enabled"])
                self.assertEqual(exported_settings["ntfy_server_url"], "https://ntfy.example.test")
                self.assertEqual(exported_settings["ntfy_topic"], "keepup-alerts")
                self.assertEqual(exported_settings["ntfy_username"], "keepup")
                self.assertEqual(exported_settings["ntfy_priority"], 4)
                self.assertNotIn("ntfy_token", exported_settings)
                self.assertNotIn("ntfy_password", exported_settings)

                database.import_backup(payload)
                imported_settings = database.get_settings()

                self.assertTrue(imported_settings["ntfy_enabled"])
                self.assertEqual(imported_settings["ntfy_server_url"], "https://ntfy.example.test")
                self.assertEqual(imported_settings["ntfy_topic"], "keepup-alerts")
                self.assertEqual(imported_settings["ntfy_username"], "keepup")
                self.assertEqual(imported_settings["ntfy_priority"], 4)
                self.assertEqual(imported_settings["ntfy_token"], "")
                self.assertEqual(imported_settings["ntfy_password"], "")

    def test_monitor_categories_are_exported_imported_and_grouped(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "keepup-test.db"
            with patch.object(database, "DATABASE_URL", db_path):
                database.init_db()
                database.update_settings({"down_failures_threshold": 1})
                monitor_id = database.create_monitor(
                    name="Nextcloud",
                    category="Websites",
                    categories=["Websites", "Docker"],
                    monitor_type="http",
                    target="https://cloud.example.test",
                    ping_enabled=False,
                    ping_mode="or",
                    ping_target="",
                    http_method="GET",
                    retry_count=2,
                    interval=60,
                    timeout=10,
                )
                database.log_check_result(monitor_id, "down", 120.0, "HTTP-Status 502", "http_status")
                database.create_monitor(
                    name="Blog",
                    category="websites",
                    monitor_type="http",
                    target="https://blog.example.test",
                    ping_enabled=False,
                    ping_mode="or",
                    ping_target="",
                    http_method="GET",
                    retry_count=2,
                    interval=60,
                    timeout=10,
                )

                payload = database.export_backup()
                self.assertEqual(payload["monitors"][0]["category"], "Websites")
                self.assertEqual(payload["monitors"][0]["categories"], ["Websites", "Docker"])

                database.import_backup(payload)
                imported_monitor = database.get_monitor(monitor_id)
                self.assertEqual(imported_monitor["category"], "Websites")
                self.assertEqual(imported_monitor["categories"], ["Websites", "Docker"])

                groups = database.get_monitor_group_summary()
                self.assertEqual(groups[0]["label"], "Websites")
                self.assertEqual(groups[0]["total"], 2)
                self.assertEqual(groups[0]["down"], 1)
                docker_group = next(group for group in groups if group["label"] == "Docker")
                self.assertEqual(docker_group["total"], 1)
                self.assertEqual(docker_group["down"], 1)

    def test_legacy_single_category_is_migrated(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "keepup-legacy.db"
            with patch.object(database, "DATABASE_URL", db_path):
                database.init_db()
                with database.get_db() as conn:
                    cursor = conn.execute(
                        """
                        INSERT INTO monitors (name, category, type, target, interval, timeout)
                        VALUES ('Router', 'Netzwerk', 'ping', '192.168.1.1', 60, 10)
                        """
                    )
                    monitor_id = int(cursor.lastrowid)
                    conn.execute("DELETE FROM monitor_categories WHERE monitor_id = ?", (monitor_id,))
                    conn.commit()

                database.init_db()
                monitor = database.get_monitor(monitor_id)
                self.assertEqual(monitor["categories"], ["Netzwerk"])


if __name__ == "__main__":
    unittest.main()
