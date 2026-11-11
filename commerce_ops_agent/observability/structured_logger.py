"""Cloud Logging–compatible Structured JSON Logger with Intent vs. Outcome capture.

Eliminates bare `print()` statements across the codebase and guarantees that every
log entry is machine-parseable JSON enriched with OpenTelemetry trace/span IDs and
PII-scrubbed metadata.
"""

from __future__ import annotations

import logging
import sys
from datetime import UTC, datetime
from typing import Any

from opentelemetry import trace
from pythonjsonlogger.json import JsonFormatter


class CloudTraceJsonFormatter(JsonFormatter):
    """Formats log records as structured JSON compatible with Google Cloud Logging."""

    def add_fields(
        self,
        log_record: dict[str, Any],
        record: logging.LogRecord,
        message_dict: dict[str, Any],
    ) -> None:
        super().add_fields(log_record, record, message_dict)
        log_record["timestamp"] = datetime.now(UTC).isoformat()
        log_record["severity"] = record.levelname
        log_record["logger"] = record.name

        span = trace.get_current_span()
        span_ctx = span.get_span_context() if span else None
        if span_ctx and span_ctx.is_valid:
            log_record["trace_id"] = format(span_ctx.trace_id, "032x")
            log_record["span_id"] = format(span_ctx.span_id, "016x")


_AUDIT_EVENTS_BUFFER: list[dict[str, Any]] = []


def get_audit_events() -> list[dict[str, Any]]:
    """Returns in-memory copy of emitted structured audit events (useful for evals/tests)."""
    return list(_AUDIT_EVENTS_BUFFER)


def clear_audit_events() -> None:
    """Clears the in-memory audit event buffer."""
    _AUDIT_EVENTS_BUFFER.clear()


def get_structured_logger(name: str = "commerce_ops_agent") -> logging.Logger:
    """Returns a singleton structured JSON logger configured for stderr output."""
    logger = logging.getLogger(name)
    if not logger.handlers:
        handler = logging.StreamHandler(sys.stderr)
        formatter = CloudTraceJsonFormatter("%(timestamp)s %(severity)s %(name)s %(message)s")
        handler.setFormatter(formatter)
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
        logger.propagate = False
    return logger


def record_intent_event(
    *,
    actor: str,
    tool_name: str,
    sanitized_args: dict[str, Any],
    rationale: str = "",
    invocation_id: str = "",
) -> dict[str, Any]:
    """Emits an explicit pre-execution `TOOL_INTENT` audit log record."""
    logger = get_structured_logger("commerce_ops_agent.audit")
    event = {
        "audit_phase": "TOOL_INTENT",
        "actor": actor,
        "tool_name": tool_name,
        "intended_arguments": sanitized_args,
        "rationale": rationale or f"Invoke {tool_name} within bounded authority",
        "invocation_id": invocation_id,
        "recorded_at": datetime.now(UTC).isoformat(),
    }
    _AUDIT_EVENTS_BUFFER.append(event)
    logger.info("Recorded agent tool intent before execution", extra=event)
    return event


def record_outcome_event(
    *,
    actor: str,
    tool_name: str,
    outcome_status: str,
    sanitized_summary: dict[str, Any],
    latency_ms: float,
    invocation_id: str = "",
) -> dict[str, Any]:
    """Emits an explicit post-execution `TOOL_OUTCOME` audit log record."""
    logger = get_structured_logger("commerce_ops_agent.audit")
    event = {
        "audit_phase": "TOOL_OUTCOME",
        "actor": actor,
        "tool_name": tool_name,
        "outcome_status": outcome_status,
        "actual_outcome": sanitized_summary,
        "latency_ms": round(latency_ms, 2),
        "invocation_id": invocation_id,
        "recorded_at": datetime.now(UTC).isoformat(),
    }
    _AUDIT_EVENTS_BUFFER.append(event)
    logger.info("Recorded agent tool outcome after execution", extra=event)
    return event
