"""Claim-side models."""

from __future__ import annotations

from datetime import date
from enum import Enum

from pydantic import BaseModel, ConfigDict, Field


class PreAuthStatus(str, Enum):
    """Three states, not a boolean — 'n/a' (not applicable) and 'no' (required but missing) are distinct facts."""

    NOT_APPLICABLE = "not_applicable"   # 'n/a' in the PDF — pre-auth simply doesn't apply to this benefit
    OBTAINED = "obtained"
    NOT_OBTAINED = "not_obtained"


class NetworkStatus(str, Enum):
    IN_NETWORK = "in_network"
    OUT_OF_NETWORK = "out_of_network"


class PreExistingLink(BaseModel):
    """Derived: is this claim related to a pre-existing condition? With reasoning + KB audit trail."""

    model_config = ConfigDict(extra="forbid")

    is_related: bool
    reasoning: str
    source: str = Field(default="manual", description="'manual' | 'llm:<provider>' | 'rule'.")
    confidence: str = Field(
        default="high",
        description="'high' or 'low'. Drives `requires_review` (True iff confidence != 'high').",
    )
    evidence_ids: list[str] = Field(
        default_factory=list,
        description="Clinical-KB row ids the classifier cited as evidence.",
    )
    requires_review: bool = Field(
        default=False,
        description="True when the classifier flagged this for human attention (confidence != high).",
    )


class Claim(BaseModel):
    """One adjudicatable claim. Raw fields and derived fields coexist; derived never overwrites raw."""

    model_config = ConfigDict(extra="forbid")

    claim_id: str
    service_date: date
    benefit_key: str = Field(..., description="Must join to a PolicyConfig.benefits[].key.")
    network_status: NetworkStatus
    provider: str | None = None
    billed_amount: float = Field(..., ge=0.0)
    eligible_amount: float | None = Field(
        default=None,
        description="R&C-capped amount if known up front. If None the engine uses billed_amount.",
    )
    preauth_status: PreAuthStatus
    diagnosis: str | None = None
    pre_existing_link: PreExistingLink | None = Field(
        default=None,
        description="Derived flag with reasoning. Never overwrites the raw diagnosis text.",
    )
    category_flags: list[str] = Field(
        default_factory=list,
        description=(
            "Derived category labels detected in the diagnosis (e.g. 'cosmetic', "
            "'self_inflicted', 'experimental'). Driven by the policy's not_covered_condition rules."
        ),
    )
    category_flags_evidence_ids: list[str] = Field(
        default_factory=list,
        description="Exclusion-category KB row ids the classifier cited as evidence for category_flags.",
    )
    category_flags_confidence: str = Field(
        default="high",
        description="'high' or 'low' confidence the classifier reported for the category decision.",
    )
    category_flags_requires_review: bool = Field(
        default=False,
        description="True when the category classifier flagged the decision for human attention.",
    )
    category_flags_reasoning: str | None = Field(
        default=None,
        description="Reasoning text from the classifier when category_flags is non-empty.",
    )

    @property
    def effective_eligible(self) -> float:
        return self.eligible_amount if self.eligible_amount is not None else self.billed_amount
