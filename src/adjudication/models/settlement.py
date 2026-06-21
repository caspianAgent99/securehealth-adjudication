"""Settlement output models — what the engine emits."""

from __future__ import annotations

from datetime import date
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from .claim import NetworkStatus


class Decision(str, Enum):
    PAYABLE = "payable"
    EXCLUDED = "excluded"
    PAYABLE_WITH_PENALTY = "payable_with_penalty"


class ReasoningStep(BaseModel):
    """One link in the audit chain. Every number a claim emits is preceded by these."""

    model_config = ConfigDict(extra="forbid")

    label: str
    value: Any
    source: str = Field(..., description="Where this came from: 'rule:<id>', 'benefit:<key>', 'endorsement:<id>', 'engine'.")
    note: str | None = None


class ClaimSettlement(BaseModel):
    model_config = ConfigDict(extra="forbid")

    claim_id: str
    service_date: date
    benefit_key: str
    network_status: NetworkStatus
    billed_amount: float
    eligible_amount: float
    deductible_applied: float
    coinsurance_member: float
    coinsurance_insurer: float
    penalty_amount: float = 0.0
    insurer_paid: float
    member_paid: float
    decision: Decision
    reason: str
    reasoning: list[ReasoningStep] = Field(default_factory=list)
    requires_review: bool = Field(
        default=False,
        description="True when the LLM classifier flagged the pre-existing decision for human review.",
    )
    review_reason: str | None = Field(
        default=None,
        description="Human-readable explanation of why review was flagged (confidence + KB evidence summary).",
    )


class SettlementReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    policy_ref: str
    plan_year_start: date
    plan_year_end: date
    settlements: list[ClaimSettlement]
    insurer_total: float
    member_total: float
    aggregate_limit: float
    aggregate_remaining: float
    metadata: dict[str, Any] = Field(default_factory=dict)
