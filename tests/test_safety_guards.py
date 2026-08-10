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

                database.import_backup(payload)
                imported_monitor = database.get_monitor(monitor_id)
                self.assertEqual(imported_monitor["category"], "Websites")

                groups = database.get_monitor_group_summary()
                self.assertEqual(groups[0]["label"], "Websites")
                self.assertEqual(groups[0]["total"], 2)
                self.assertEqual(groups[0]["down"], 1)


if __name__ == "__main__":
    unittest.main()
