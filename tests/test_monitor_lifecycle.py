import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import database
import main


class MonitorLifecycleIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_create_edit_toggle_delete_stays_persistent_and_invalidates_cache(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "keepup.db"
            with patch.object(database, "DATABASE_URL", db_path):
                database.init_db()
                request = SimpleNamespace(headers={"accept": "application/json"})
                initial_version = main.get_dashboard_snapshot_version()

                with (
                    patch.object(main, "reschedule_monitor_job"),
                    patch.object(main, "remove_monitor_job"),
                    patch.object(main, "execute_monitor_check_and_refresh_cards", new=AsyncMock()),
                    patch.object(main, "refresh_dashboard_cards_cache", new=AsyncMock()),
                ):
                    created = await main.create_monitor_route(
                        request=request,
                        name="Integration Web",
                        category="Webseiten",
                        categories=["Webseiten"],
                        category_custom="",
                        monitor_type="http",
                        target="https://example.test",
                        ping_target="",
                        http_method="GET",
                        retry_count=2,
                        interval=60,
                        timeout=10,
                        expected_text="",
                        forbidden_text="",
                    )
                    monitor_id = int(database.list_monitors()[0]["id"])
                    self.assertEqual(created.status_code, 200)
                    self.assertGreater(main.get_dashboard_snapshot_version(), initial_version)

                    edited = await main.edit_monitor_route(
                        request=request,
                        monitor_id=monitor_id,
                        name="Integration API",
                        category="Dienste",
                        categories=["Dienste", "Webseiten"],
                        category_custom="",
                        monitor_type="http",
                        target="https://api.example.test",
                        ping_target="",
                        http_method="HEAD",
                        retry_count=1,
                        interval=120,
                        timeout=8,
                        expected_text="",
                        forbidden_text="",
                    )
                    self.assertEqual(edited.status_code, 200)
                    persisted = main.monitor_repository.get(monitor_id)
                    self.assertEqual(persisted["name"], "Integration API")
                    self.assertEqual(persisted["categories"], ["Dienste", "Webseiten"])

                    toggled = await main.toggle_monitor_route(monitor_id, request)
                    self.assertEqual(toggled.status_code, 200)
                    self.assertFalse(bool(main.monitor_repository.get(monitor_id)["enabled"]))

                    deleted = await main.delete_monitor_route(monitor_id, request)
                    self.assertEqual(deleted.status_code, 200)
                    self.assertIsNone(main.monitor_repository.get(monitor_id))

                await asyncio.sleep(0)


if __name__ == "__main__":
    unittest.main()
