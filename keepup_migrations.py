from __future__ import annotations

import sqlite3
from collections.abc import Callable


Migration = Callable[[sqlite3.Cursor], None]


def _baseline(_cursor: sqlite3.Cursor) -> None:
    """Record the schema that predates explicit migrations."""


MIGRATIONS: tuple[tuple[int, Migration], ...] = ((1, _baseline),)
CURRENT_SCHEMA_VERSION = MIGRATIONS[-1][0]


def apply_schema_migrations(cursor: sqlite3.Cursor, applied_at: str) -> int:
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
            version INTEGER PRIMARY KEY,
            applied_at TEXT NOT NULL
        )
        """
    )
    row = cursor.execute("SELECT MAX(version) AS version FROM schema_migrations").fetchone()
    current = int(row["version"] or 0) if row else 0
    for version, migration in MIGRATIONS:
        if version <= current:
            continue
        migration(cursor)
        cursor.execute(
            "INSERT INTO schema_migrations (version, applied_at) VALUES (?, ?)",
            (version, applied_at),
        )
        current = version
    return current
