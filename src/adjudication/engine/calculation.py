"""GC-1 per-claim calculation core. Pure function. No state.

Implements the policy's calculation_order step-by-step and records each step as a
ReasoningStep so the math is auditable.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..models.claim import Claim, NetworkStatus
from ..models.policy import Benefit, PolicyConfig
from ..models.settlement import ReasoningStep


@dataclass(frozen=True)
class CalcResult:
    billed: float
    eligible: float
    deductible_applied: float
    post_deductible: float
    member_coinsurance_pct: float
    insurer_pre_penalty: float  # insurer share before any preauth penalty / accumulator clip
    member_coinsurance_amount: float  # excludes deductible
    penalty_amount: float
    insurer_after_penalty: float
    member_total_pre_limits: float  # deductible + member coinsurance + penalty
    reasoning: list[ReasoningStep]
    effective_benefit_values: dict[str, Any]


def _resolve_endorsed_benefit(policy: PolicyConfig, claim: Claim) -> tuple[Benefit, dict[str, Any], list[ReasoningStep]]:
    """Compute effective benefit fields, recording base vs override for the audit trail.

    Endorsements stay separate in the config; we resolve them here at runtime.
    """

    base = policy.benefit(claim.benefit_key)
    overrides_applied: dict[str, Any] = {}
    steps: list[ReasoningStep] = []
    effective: dict[str, Any] = base.model_dump()

    for end in policy.endorsements_for(base.key, as_of=claim.service_date):
        for field_name, new_value in end.overrides.items():
            if field_name not in effective:
                continue
            old = effective[field_name]
            effective[field_name] = new_value
            overrides_applied[field_name] = {"from": old, "to": new_value, "source": end.source}
            steps.append(
                ReasoningStep(
                    label=f"endorsement:{end.id}:{field_name}",
                    value={"from": old, "to": new_value},
                    source=f"endorsement:{end.id}",
                    note=end.source,
                )
            )

    return base, effective, steps


def _coinsurance_pct(effective: dict[str, Any], claim: Claim) -> float | None:
    if claim.network_status == NetworkStatus.IN_NETWORK:
        return float(effective["in_network_coinsurance"])
    v = effective.get("out_of_network_coinsurance")
    return None if v is None else float(v)


def calculate_claim(
    claim: Claim,
    policy: PolicyConfig,
    *,
    penalty_modifiers: list[dict[str, Any]] | None = None,
) -> CalcResult:
    """Apply GC-1 to one claim. Returns a CalcResult with every step recorded.

    `penalty_modifiers` are gate outputs (e.g. preauth penalty) applied in GC-1 step (d) on the insurer share.
    """

    penalty_modifiers = penalty_modifiers or []
    steps: list[ReasoningStep] = []

    base, effective, end_steps = _resolve_endorsed_benefit(policy, claim)
    steps.extend(end_steps)

    coins_pct = _coinsurance_pct(effective, claim)
    if coins_pct is None:
        # OON not covered for this benefit — the gate should have caught this, but be defensive.
        return CalcResult(
            billed=claim.billed_amount,
            eligible=0.0,
            deductible_applied=0.0,
            post_deductible=0.0,
            member_coinsurance_pct=0.0,
            insurer_pre_penalty=0.0,
            member_coinsurance_amount=0.0,
            penalty_amount=0.0,
            insurer_after_penalty=0.0,
            member_total_pre_limits=claim.billed_amount,
            reasoning=steps + [ReasoningStep(label="oon_not_covered", value=True, source=f"benefit:{base.key}")],
            effective_benefit_values=effective,
        )

    billed = float(claim.billed_amount)
    eligible_cap = claim.effective_eligible
    deductible_cfg = float(effective.get("deductible", 0.0) or 0.0)

    eligible = billed
    deductible_applied = 0.0
    post_deductible = eligible

    for step in policy.calculation_order:
        if step == "cap_to_eligible":
            eligible = min(billed, float(eligible_cap))
            steps.append(
                ReasoningStep(
                    label="cap_to_eligible",
                    value=round(eligible, 2),
                    source="engine",
                    note=f"min(billed={billed:.2f}, eligible_cap={eligible_cap:.2f})",
                )
            )
            post_deductible = eligible

        elif step == "apply_deductible":
            deductible_applied = min(deductible_cfg, eligible)
            post_deductible = max(0.0, eligible - deductible_applied)
            steps.append(
                ReasoningStep(
                    label="apply_deductible",
                    value=round(deductible_applied, 2),
                    source=f"benefit:{base.key}",
                    note=f"deductible={deductible_cfg:.2f}; post_deductible={post_deductible:.2f}",
                )
            )

        elif step == "apply_coinsurance":
            member_share = round(coins_pct * post_deductible, 2)
            insurer_share = round(post_deductible - member_share, 2)
            steps.append(
                ReasoningStep(
                    label="apply_coinsurance",
                    value={"member_pct": coins_pct, "member": member_share, "insurer": insurer_share},
                    source=f"benefit:{base.key}",
                    note=f"member_pct={coins_pct:.2%} of {post_deductible:.2f}",
                )
            )

    member_share = round(coins_pct * post_deductible, 2)
    insurer_pre_penalty = round(post_deductible - member_share, 2)

    penalty_amount = 0.0
    insurer_after = insurer_pre_penalty
    for mod in penalty_modifiers:
        if mod.get("kind") == "preauth_penalty":
            pct = float(mod.get("penalty_pct", 0.0))
            reduction = round(insurer_after * pct, 2)
            penalty_amount += reduction
            insurer_after = round(insurer_after - reduction, 2)
            steps.append(
                ReasoningStep(
                    label=f"penalty:{mod.get('rule_id', 'preauth_penalty')}",
                    value={"penalty_pct": pct, "reduction": reduction},
                    source=f"rule:{mod.get('rule_id', 'preauth_penalty')}",
                    note=f"insurer share reduced from {insurer_pre_penalty:.2f} to {insurer_after:.2f}; member bears the reduction",
                )
            )

    member_total = round(deductible_applied + member_share + penalty_amount, 2)

    return CalcResult(
        billed=round(billed, 2),
        eligible=round(eligible, 2),
        deductible_applied=round(deductible_applied, 2),
        post_deductible=round(post_deductible, 2),
        member_coinsurance_pct=coins_pct,
        insurer_pre_penalty=insurer_pre_penalty,
        member_coinsurance_amount=member_share,
        penalty_amount=round(penalty_amount, 2),
        insurer_after_penalty=round(insurer_after, 2),
        member_total_pre_limits=member_total,
        reasoning=steps,
        effective_benefit_values=effective,
    )
