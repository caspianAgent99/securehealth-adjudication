"""Policy-side models.

These describe a SecureHealth-style benefits plan as data. The engine never
sees a string-typed policy field — it only ever sees these models.
"""

from __future__ import annotations

from datetime import date
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class Benefit(BaseModel):
    """One row in the Table of Benefits."""

    model_config = ConfigDict(extra="forbid")

    key: str = Field(..., description="Stable identifier joined from claims (e.g. 'outpatient_consultation').")
    name: str = Field(..., description="Human-readable name.")
    annual_sub_limit: float | None = Field(
        default=None,
        description="Annual cap for this benefit. None means 'no sub-limit of its own' (e.g. inpatient).",
    )
    in_network_coinsurance: float = Field(..., ge=0.0, le=1.0, description="Member share in-network, 0..1.")
    out_of_network_coinsurance: float | None = Field(
        default=None,
        ge=0.0,
        le=1.0,
        description="Member share OON, 0..1. None means OON not covered at all (distinct from 0).",
    )
    deductible: float = Field(default=0.0, ge=0.0)
    requires_preauth: bool = False
    notes: str | None = None


class Endorsement(BaseModel):
    """A layered override on top of a base Benefit. Stored separately, never merged in."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(..., description="Endorsement id (e.g. 'E1').")
    benefit_key: str = Field(..., description="Which Benefit.key this overrides.")
    overrides: dict[str, Any] = Field(
        default_factory=dict,
        description="Map of Benefit field name -> new value. E.g. {'in_network_coinsurance': 0.10, 'annual_sub_limit': 4000}.",
    )
    source: str = Field(..., description="Citation back to the policy doc (e.g. 'Section 5, E1').")
    effective_from: date | None = None
    effective_to: date | None = None


ExclusionType = Literal[
    "waiting_period",
    "preauth_penalty",
    "not_covered_condition",
    "not_covered_oon",
]


class ExclusionRule(BaseModel):
    """A named, parameterized exclusion or modifier rule.

    Three families today:
      - waiting_period: bars claims for `condition_flag` within `waiting_days` of policy_start_date.
      - preauth_penalty: applies `penalty_pct` to the member share when preauth was required but not obtained.
      - not_covered_oon: marks a benefit as excluded when the claim is out-of-network.
      - not_covered_condition: hard exclusion when a configured condition flag is set.
    """

    model_config = ConfigDict(extra="forbid")

    id: str
    type: ExclusionType
    params: dict[str, Any] = Field(default_factory=dict)
    applies_to_benefits: list[str] | None = Field(
        default=None,
        description="If set, the rule only applies when claim.benefit_key is in this list. None = all benefits.",
    )
    reason_template: str = Field(
        ...,
        description="Human-readable reason template; may interpolate params using {name} placeholders.",
    )


class PolicyConfig(BaseModel):
    """The frozen, human-approved plan configuration."""

    model_config = ConfigDict(extra="forbid")

    policy_ref: str = Field(..., description="Stable reference, e.g. 'SecureHealth Plan B / 2026'.")
    plan_year_start: date
    plan_year_end: date
    policy_start_date: date = Field(..., description="When this member's cover began — anchors waiting periods.")
    aggregate_annual_limit: float = Field(..., gt=0, description="Cap on insurer payments per plan year.")
    benefits: list[Benefit]
    endorsements: list[Endorsement] = Field(default_factory=list)
    exclusion_rules: list[ExclusionRule] = Field(default_factory=list)
    calculation_order: list[str] = Field(
        default_factory=lambda: ["cap_to_eligible", "apply_deductible", "apply_coinsurance"],
        description="GC-1 calculation steps in order. Engine reads this as data.",
    )
    approved_by: str
    approved_at: date
    metadata: dict[str, Any] = Field(default_factory=dict)

    # --- structural sanity (these are enforced both here AND in the policy_validator) ---

    @field_validator("calculation_order")
    @classmethod
    def _known_steps(cls, v: list[str]) -> list[str]:
        allowed = {"cap_to_eligible", "apply_deductible", "apply_coinsurance"}
        unknown = [s for s in v if s not in allowed]
        if unknown:
            raise ValueError(f"unknown calculation steps: {unknown}; allowed: {sorted(allowed)}")
        return v

    @model_validator(mode="after")
    def _endorsements_target_real_benefits(self) -> "PolicyConfig":
        keys = {b.key for b in self.benefits}
        for e in self.endorsements:
            if e.benefit_key not in keys:
                raise ValueError(f"endorsement {e.id} targets unknown benefit '{e.benefit_key}'")
        for r in self.exclusion_rules:
            if r.applies_to_benefits:
                missing = [k for k in r.applies_to_benefits if k not in keys]
                if missing:
                    raise ValueError(f"exclusion {r.id} references unknown benefits: {missing}")
        return self

    # --- convenience helpers ---

    def benefit(self, key: str) -> Benefit:
        for b in self.benefits:
            if b.key == key:
                return b
        raise KeyError(f"benefit '{key}' not found in policy {self.policy_ref}")

    def endorsements_for(self, benefit_key: str, as_of: date | None = None) -> list[Endorsement]:
        out: list[Endorsement] = []
        for e in self.endorsements:
            if e.benefit_key != benefit_key:
                continue
            if as_of is not None:
                if e.effective_from and as_of < e.effective_from:
                    continue
                if e.effective_to and as_of > e.effective_to:
                    continue
            out.append(e)
        return out
