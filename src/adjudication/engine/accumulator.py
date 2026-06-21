"""The stateful limit ledger.

Processes claims in service-date order, tracks per-benefit sub-limits + the global aggregate,
clips each insurer payment to what remains, and records when a limit bites.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..models.policy import PolicyConfig
from ..models.settlement import ReasoningStep


@dataclass
class ClipOutcome:
    insurer_paid: float
    member_extra: float  # additional liability shifted to member because a limit clipped insurer share
    reasoning: list[ReasoningStep]


class LimitAccumulator:
    """In-memory ledger of remaining sub-limits + aggregate. Not reusable across plan years."""

    def __init__(self, policy: PolicyConfig):
        self.policy = policy
        self._sub_remaining: dict[str, float | None] = {}
        for b in policy.benefits:
            # Apply endorsement overrides to sub-limits if they bump them.
            effective_sub = b.annual_sub_limit
            for e in policy.endorsements:
                if e.benefit_key == b.key and "annual_sub_limit" in e.overrides:
                    effective_sub = e.overrides["annual_sub_limit"]
            self._sub_remaining[b.key] = effective_sub  # may be None (no sub-limit)
        self._aggregate_remaining: float = float(policy.aggregate_annual_limit)
        self._insurer_paid_per_benefit: dict[str, float] = {b.key: 0.0 for b in policy.benefits}

    @property
    def aggregate_remaining(self) -> float:
        return round(self._aggregate_remaining, 2)

    def sub_remaining(self, benefit_key: str) -> float | None:
        v = self._sub_remaining[benefit_key]
        return v if v is None else round(v, 2)

    def insurer_paid_for(self, benefit_key: str) -> float:
        return round(self._insurer_paid_per_benefit[benefit_key], 2)

    def clip(self, *, benefit_key: str, proposed_insurer_pay: float) -> ClipOutcome:
        """Clip an insurer payment to what remains in (a) the benefit sub-limit and (b) the aggregate."""

        steps: list[ReasoningStep] = []
        original = round(proposed_insurer_pay, 2)
        paid = original

        sub = self._sub_remaining[benefit_key]
        if sub is not None and paid > sub:
            steps.append(
                ReasoningStep(
                    label="clip:sub_limit",
                    value={"benefit": benefit_key, "proposed": paid, "clipped_to": round(sub, 2)},
                    source=f"benefit:{benefit_key}",
                    note="benefit annual sub-limit reached",
                )
            )
            paid = round(sub, 2)

        if paid > self._aggregate_remaining:
            steps.append(
                ReasoningStep(
                    label="clip:aggregate",
                    value={"proposed": paid, "clipped_to": round(self._aggregate_remaining, 2)},
                    source="policy:aggregate",
                    note="annual aggregate limit reached",
                )
            )
            paid = round(self._aggregate_remaining, 2)

        paid = max(0.0, round(paid, 2))
        member_extra = round(original - paid, 2)

        # Decrement state.
        if sub is not None:
            self._sub_remaining[benefit_key] = round(sub - paid, 2)
        self._aggregate_remaining = round(self._aggregate_remaining - paid, 2)
        self._insurer_paid_per_benefit[benefit_key] = round(self._insurer_paid_per_benefit[benefit_key] + paid, 2)

        if member_extra > 0:
            steps.append(
                ReasoningStep(
                    label="member_absorbs_overage",
                    value=member_extra,
                    source="engine",
                    note="member liable for eligible expense beyond a reached limit",
                )
            )

        return ClipOutcome(insurer_paid=paid, member_extra=member_extra, reasoning=steps)

    def snapshot(self) -> dict[str, Any]:
        return {
            "aggregate_remaining": self.aggregate_remaining,
            "sub_remaining": {k: self.sub_remaining(k) for k in self._sub_remaining},
            "insurer_paid_per_benefit": {k: self.insurer_paid_for(k) for k in self._insurer_paid_per_benefit},
        }
