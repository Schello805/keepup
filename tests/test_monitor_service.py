import asyncio
import unittest
from unittest.mock import AsyncMock, Mock

from keepup_monitor_service import MonitorService


class MonitorServiceTests(unittest.IsolatedAsyncioTestCase):
    async def test_create_coordinates_persistence_scheduler_cache_and_check(self):
        repository = Mock()
        repository.create.return_value = 7
        repository.get.return_value = {"id": 7, "name": "Router"}
        invalidate = Mock()
        mark_stale = Mock()
        reschedule = Mock()
        check = AsyncMock()
        service = MonitorService(
            repository=repository,
            invalidate_detail=invalidate,
            mark_dashboard_stale=mark_stale,
            reschedule_job=reschedule,
            remove_job=Mock(),
            check_and_refresh=check,
            refresh_dashboard=AsyncMock(),
            execute_check=AsyncMock(),
        )

        monitor = service.create(name="Router")
        await asyncio.sleep(0)

        self.assertEqual(monitor["id"], 7)
        repository.create.assert_called_once_with(name="Router")
        invalidate.assert_called_once_with(7)
        reschedule.assert_called_once_with(7)
        mark_stale.assert_called_once_with()
        check.assert_awaited_once_with(7)


if __name__ == "__main__":
    unittest.main()
