from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from keepup_repository import MonitorRepository


@dataclass(frozen=True)
class MonitorService:
    repository: MonitorRepository
    invalidate_detail: Callable[[int], None]
    mark_dashboard_stale: Callable[[], None]
    reschedule_job: Callable[[int], None]
    remove_job: Callable[[int], None]
    check_and_refresh: Callable[[int], Awaitable[Any]]
    refresh_dashboard: Callable[[], Awaitable[Any]]
    execute_check: Callable[[int], Awaitable[Any]]

    def _schedule(self, awaitable: Awaitable[Any]) -> None:
        asyncio.create_task(awaitable)

    def create(self, **values: Any) -> dict[str, Any]:
        monitor_id = self.repository.create(**values)
        self.invalidate_detail(monitor_id)
        self.reschedule_job(monitor_id)
        self.mark_dashboard_stale()
        self._schedule(self.check_and_refresh(monitor_id))
        return self.repository.get(monitor_id) or {"id": monitor_id}

    def update(self, monitor_id: int, **values: Any) -> dict[str, Any]:
        self.repository.update(monitor_id, **values)
        self.invalidate_detail(monitor_id)
        self.reschedule_job(monitor_id)
        self.mark_dashboard_stale()
        self._schedule(self.check_and_refresh(monitor_id))
        return self.repository.get(monitor_id) or {"id": monitor_id}

    def toggle(self, monitor_id: int) -> bool:
        monitor = self.repository.get(monitor_id)
        if not monitor:
            raise KeyError(monitor_id)
        enabled = not bool(monitor.get("enabled", 1))
        self.repository.set_enabled(monitor_id, enabled)
        self.invalidate_detail(monitor_id)
        self.reschedule_job(monitor_id)
        self.mark_dashboard_stale()
        self._schedule(self.refresh_dashboard())
        return enabled

    def delete(self, monitor_id: int) -> None:
        self.repository.delete(monitor_id)
        self.invalidate_detail(monitor_id)
        self.remove_job(monitor_id)
        self.mark_dashboard_stale()
        self._schedule(self.refresh_dashboard())

    async def run(self, monitor_id: int) -> Any:
        result = await self.execute_check(monitor_id)
        self.invalidate_detail(monitor_id)
        self.mark_dashboard_stale()
        self._schedule(self.refresh_dashboard())
        return result
