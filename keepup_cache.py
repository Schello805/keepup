from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Any


@dataclass
class DashboardCacheStore:
    """Own all dashboard cache state and its synchronization primitives."""

    cards: dict[str, Any] = field(
        default_factory=lambda: {"expires_at": 0.0, "html": None, "generation": 0}
    )
    snapshot: dict[str, Any] = field(
        default_factory=lambda: {"version": 0, "expires_at": 0.0, "payload": None}
    )
    status_wall: dict[str, Any] = field(
        default_factory=lambda: {"version": 0, "expires_at": 0.0, "payload": None}
    )
    cards_lock: threading.Lock = field(default_factory=threading.Lock)
    snapshot_lock: threading.Lock = field(default_factory=threading.Lock)
    status_wall_lock: threading.Lock = field(default_factory=threading.Lock)
    _version: int = 1
    _version_lock: threading.Lock = field(default_factory=threading.Lock)

    def version(self) -> int:
        with self._version_lock:
            return self._version

    def advance_version(self) -> int:
        with self._version_lock:
            self._version += 1
            return self._version

    def reset(self) -> None:
        with self.cards_lock:
            self.cards.update(expires_at=0.0, html=None, generation=0)
        with self.snapshot_lock:
            self.snapshot.update(version=0, expires_at=0.0, payload=None)
        with self.status_wall_lock:
            self.status_wall.update(version=0, expires_at=0.0, payload=None)
