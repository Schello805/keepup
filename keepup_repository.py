from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

import database


@dataclass(frozen=True)
class MonitorRepository:
    """Persistence boundary for monitor lifecycle operations."""

    def get(self, monitor_id: int) -> Optional[dict[str, Any]]:
        return database.get_monitor(monitor_id)

    def create(self, **values: Any) -> int:
        return database.create_monitor(**values)

    def update(self, monitor_id: int, **values: Any) -> None:
        database.update_monitor(monitor_id=monitor_id, **values)

    def set_enabled(self, monitor_id: int, enabled: bool) -> None:
        database.set_monitor_enabled(monitor_id, enabled)

    def delete(self, monitor_id: int) -> None:
        database.delete_monitor(monitor_id)
