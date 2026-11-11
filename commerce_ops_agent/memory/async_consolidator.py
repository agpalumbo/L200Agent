"""Non-blocking Asynchronous Memory Consolidation Pipeline.

Offloads post-turn memory extraction, PII scrubbing, and SQLite persistence onto
background `asyncio` tasks so conversational latency and UI streaming never block.
"""

from __future__ import annotations

import asyncio
import re
from datetime import UTC, datetime

from ..observability.structured_logger import get_structured_logger
from ..observability.telemetry import traced_operation
from ..schemas.audit_models import IncidentMemoryRecord
from ..security.pii_redactor import DEFAULT_PII_REDACTOR
from .session_store import DEFAULT_INCIDENT_STORE, PersistentIncidentMemoryStore

logger = get_structured_logger(__name__)

_ORDER_PSEUDONYM_RE = re.compile(r"\bord_[0-9a-f]{12}\b")
_HANDLE_RE = re.compile(r"\bsignature-[a-z0-9-]+\b")

_BACKGROUND_TASKS: set[asyncio.Task[int]] = set()


@traced_operation("memory.async_consolidate_turn")
async def consolidate_turn_memory_async(
    *,
    session_id: str,
    final_response_text: str,
    store: PersistentIncidentMemoryStore | None = None,
) -> int:
    """Extracts operational findings and writes a PII-redacted memory record."""
    target_store = store or DEFAULT_INCIDENT_STORE
    scrubbed_text = DEFAULT_PII_REDACTOR.redact_text(final_response_text)

    flagged_orders = sorted(set(_ORDER_PSEUDONYM_RE.findall(scrubbed_text)))
    drifted_handles = sorted(set(_HANDLE_RE.findall(scrubbed_text)))

    lower = scrubbed_text.lower()
    if any(k in lower for k in ("breach", "stale", "drift", "awaiting", "approval")):
        severity = "action_required"
    elif "warning" in lower or flagged_orders:
        severity = "warning"
    else:
        severity = "healthy"

    record = IncidentMemoryRecord(
        session_id=session_id,
        recorded_at_iso=datetime.now(UTC).isoformat(),
        severity=severity,  # type: ignore[arg-type]
        drifted_handles=drifted_handles,
        flagged_order_pseudonyms=flagged_orders,
        summary_text=scrubbed_text[:600],
    )

    row_id = await asyncio.to_thread(target_store.save_incident, record)
    logger.info(
        "Consolidated operational turn memory in background task",
        extra={"session_id": session_id, "incident_row_id": row_id, "severity": severity},
    )
    return row_id


def schedule_async_memory_consolidation(
    *,
    session_id: str,
    final_response_text: str,
    store: PersistentIncidentMemoryStore | None = None,
) -> asyncio.Task[int] | None:
    """Schedules non-blocking memory consolidation on the active event loop."""
    if not final_response_text.strip():
        return None
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return None

    task = loop.create_task(
        consolidate_turn_memory_async(
            session_id=session_id,
            final_response_text=final_response_text,
            store=store,
        )
    )
    _BACKGROUND_TASKS.add(task)
    task.add_done_callback(_BACKGROUND_TASKS.discard)
    return task


async def drain_background_memory_tasks() -> None:
    """Awaits all pending background consolidation tasks (used in clean shutdown/tests)."""
    if _BACKGROUND_TASKS:
        await asyncio.gather(*list(_BACKGROUND_TASKS), return_exceptions=True)
