"""Persistent Session State & Operational Incident Store.

Connects Google ADK's `DatabaseSessionService` (backed by SQLite locally or Cloud SQL
PostgreSQL in production) alongside a persistent `operational_incidents` table for
cross-session retrieval of past storefront drift and fulfillment SLA events.
"""

from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path
from typing import Any

from google.adk.sessions import DatabaseSessionService

from ..config import get_settings
from ..observability.structured_logger import get_structured_logger
from ..observability.telemetry import traced_operation
from ..schemas.audit_models import IncidentMemoryRecord
from ..security.pii_redactor import DEFAULT_PII_REDACTOR

logger = get_structured_logger(__name__)


def create_persistent_session_service(db_url: str | None = None) -> DatabaseSessionService:
    """Creates an ADK `DatabaseSessionService` backed by SQLite or Cloud SQL."""
    resolved_url = db_url or get_settings().session_db_url
    logger.info("Initializing persistent ADK DatabaseSessionService", extra={"db_url": resolved_url})
    return DatabaseSessionService(db_url=resolved_url)


class PersistentIncidentMemoryStore:
    """Durable SQLite-backed store for PII-redacted operational incident summaries."""

    def __init__(self, sqlite_path: str | Path = "./.adk_operational_memory.db") -> None:
        self.sqlite_path = Path(sqlite_path)
        self._ensure_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.sqlite_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _ensure_schema(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS operational_incidents (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL,
                    recorded_at_iso TEXT NOT NULL,
                    severity TEXT NOT NULL,
                    drifted_handles_json TEXT NOT NULL,
                    flagged_orders_json TEXT NOT NULL,
                    summary_text TEXT NOT NULL
                )
                """
            )
            conn.commit()
        if self.sqlite_path.exists():
            os.chmod(self.sqlite_path, 0o600)

    @traced_operation("memory.store_incident")
    def save_incident(self, record: IncidentMemoryRecord) -> int:
        """Persists a PII-scrubbed `IncidentMemoryRecord` and returns its row ID."""
        clean_summary = DEFAULT_PII_REDACTOR.redact_text(record.summary_text)
        with self._connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO operational_incidents (
                    session_id,
                    recorded_at_iso,
                    severity,
                    drifted_handles_json,
                    flagged_orders_json,
                    summary_text
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    record.session_id,
                    record.recorded_at_iso,
                    record.severity,
                    json.dumps(record.drifted_handles),
                    json.dumps(record.flagged_order_pseudonyms),
                    clean_summary,
                ),
            )
            conn.commit()
        if self.sqlite_path.exists():
            os.chmod(self.sqlite_path, 0o600)
            return int(cur.lastrowid or 0)

    @traced_operation("memory.search_incidents")
    def search_recent_incidents(self, query: str = "", limit: int = 5) -> list[dict[str, Any]]:
        """Retrieves recent PII-free operational incidents matching `query`."""
        with self._connect() as conn:
            if query.strip():
                pattern = f"%{query.strip()}%"
                rows = conn.execute(
                    """
                    SELECT * FROM operational_incidents
                    WHERE summary_text LIKE ? OR drifted_handles_json LIKE ? OR flagged_orders_json LIKE ?
                    ORDER BY id DESC LIMIT ?
                    """,
                    (pattern, pattern, pattern, limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM operational_incidents ORDER BY id DESC LIMIT ?",
                    (limit,),
                ).fetchall()

        results: list[dict[str, Any]] = []
        for row in rows:
            results.append(
                {
                    "id": row["id"],
                    "session_id": row["session_id"],
                    "recorded_at_iso": row["recorded_at_iso"],
                    "severity": row["severity"],
                    "drifted_handles": json.loads(row["drifted_handles_json"]),
                    "flagged_order_pseudonyms": json.loads(row["flagged_orders_json"]),
                    "summary_text": row["summary_text"],
                }
            )
        return results


DEFAULT_INCIDENT_STORE = PersistentIncidentMemoryStore()
