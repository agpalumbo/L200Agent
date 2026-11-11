"""Governance, HITL confirmation, and persistent incident memory schemas."""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from .commerce_models import StrictSchema


class HumanApprovalDecision(StrictSchema):
    """Structured response schema for ADK Human-in-the-Loop confirmation pauses."""

    approved: bool = Field(..., description="Whether the human operator approves the mutation.")
    operator_note: str = Field(
        default="",
        max_length=250,
        description="Optional operator rationale recorded in the audit log.",
    )


class IncidentMemoryRecord(StrictSchema):
    """PII-free operational summary persisted asynchronously across sessions."""

    session_id: str
    recorded_at_iso: str
    severity: Literal["healthy", "warning", "action_required"]
    drifted_handles: list[str]
    flagged_order_pseudonyms: list[str]
    summary_text: str
