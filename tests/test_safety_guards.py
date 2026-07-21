import unittest
from unittest.mock import patch

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
        self.assertEqual(payload["smtp_password"], "existing-smtp-password")

    async def test_limited_upload_rejects_oversized_content(self):
        upload = ChunkedUpload([b"a" * 6, b"b" * 6])

        with self.assertRaises(ValueError):
            await read_limited_upload(upload, max_bytes=10)

    async def test_limited_upload_accepts_content_within_limit(self):
        upload = ChunkedUpload([b"{", b"}"])

        content = await read_limited_upload(upload, max_bytes=10)

        self.assertEqual(content, b"{}")


if __name__ == "__main__":
    unittest.main()
