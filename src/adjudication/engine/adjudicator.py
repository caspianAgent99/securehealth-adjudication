"""Orchestrates gate -> calc -> accumulate. The only thing that knows about all three.

Threads claims (in service-date order) through the pipeline and assembles a SettlementReport.
"""

from __future__ import annotations

from ..models.claim import Claim
from ..models.member import MemberContext
from ..models.policy import PolicyConfig
from ..models.settlement import ClaimSettlement, Decision, ReasoningStep, SettlementReport

from .accumulator import LimitAccumulator
from .calculation import calculate_claim
from .exclusions import (
    admission_verdict_is_material,
    category_verdict_is_material,
    evaluate_exclusions,
    preexisting_verdict_is_material,
)


def _preexisting_review_summary(
    claim: Claim, policy: PolicyConfig, member: MemberContext | None
) -> str | None:
    link = claim.pre_existing_link
    if not link or not link.requires_review:
        return None
    if not preexisting_verdict_is_material(claim, policy, member):
        return None
    evidence = f", evidence={link.evidence_ids}" if link.evidence_ids else ""
    return (
        f"Pre-existing classifier confidence={link.confidence}{evidence}. "
        f"is_related={link.is_related}. Reasoning: {link.reasoning or '(none)'}"
    )


def _category_review_summary(
    claim: Claim, policy: PolicyConfig, member: MemberContext | None
) -> str | None:
    if not claim.category_flags_requires_review:
        return None
    if not category_verdict_is_material(claim, policy, member):
        return None
    evidence = f", evidence={claim.category_flags_evidence_ids}" if claim.category_flags_evidence_ids else ""
    return (
        f"Category classifier confidence={claim.category_flags_confidence}{evidence}. "
        f"flags={claim.category_flags or '[]'}. "
        f"Reasoning: {claim.category_flags_reasoning or '(none)'}"
    )


def _admission_review_summary(
    claim: Claim, policy: PolicyConfig, member: MemberContext | None
) -> str | None:
    if not claim.admission_type_requires_review:
        return None
    if not admission_verdict_is_material(claim, policy, member):
        return None
    return (
        f"Admission classifier confidence={claim.admission_type_confidence}. "
        f"admission_type={claim.admission_type.value}. "
        f"Reasoning: {claim.admission_type_reasoning or '(none)'}"
    )


def _review_metadata(
    claim: Claim, policy: PolicyConfig, member: MemberContext | None = None
) -> tuple[bool, str | None]:
    """Decide whether this claim should be flagged for human review.

    Flag if the pre-existing, category, OR admission classifier returned a non-`high`
    confidence verdict AND that verdict was material (the policy has a rule that actually
    consumes it for this claim). When several fire, all summaries are surfaced.
    """

    summaries: list[str] = []
    pre = _preexisting_review_summary(claim, policy, member)
    if pre:
        summaries.append(pre)
    cat = _category_review_summary(claim, policy, member)
    if cat:
        summaries.append(cat)
    adm = _admission_review_summary(claim, policy, member)
    if adm:
        summaries.append(adm)
    if not summaries:
        return False, None
    return True, " | ".join(summaries)


def _excluded_settlement(
    claim: Claim,
    policy: PolicyConfig,
    reasons: list[str],
    extra_steps: list[ReasoningStep],
    member: MemberContext | None = None,
) -> ClaimSettlement:
    reason_text = " | ".join(reasons) if reasons else "Excluded by policy."
    requires_review, review_reason = _review_metadata(claim, policy, member)
    return ClaimSettlement(
        claim_id=claim.claim_id,
        service_date=claim.service_date,
        benefit_key=claim.benefit_key,
        network_status=claim.network_status,
        billed_amount=round(float(claim.billed_amount), 2),
        eligible_amount=0.0,
        deductible_applied=0.0,
        coinsurance_member=0.0,
        coinsurance_insurer=0.0,
        penalty_amount=0.0,
        insurer_paid=0.0,
        member_paid=round(float(claim.billed_amount), 2),
        decision=Decision.EXCLUDED,
        reason=reason_text,
        reasoning=extra_steps,
        requires_review=requires_review,
        review_reason=review_reason,
    )


def adjudicate(
    claims: list[Claim],
    policy: PolicyConfig,
    member: MemberContext | None = None,
) -> SettlementReport:
    """Run the full pipeline. Pure given (claims, policy, member). Output is service-date order.

    `member` carries per-member context (inception_date, declared chronic conditions).
    When omitted, the engine falls back to `policy.policy_start_date` for §4.2 anchoring —
    correct only for members whose inception coincides with the policy default.
    """

    ordered = sorted(claims, key=lambda c: (c.service_date, c.claim_id))
    accumulator = LimitAccumulator(policy)
    settlements: list[ClaimSettlement] = []

    insurer_total = 0.0
    member_total = 0.0

    for claim in ordered:
        gate = evaluate_exclusions(claim, policy, member=member)

        if gate.excluded:
            settlements.append(_excluded_settlement(claim, policy, gate.reasons, gate.reasoning, member))
            member_total = round(member_total + float(claim.billed_amount), 2)
            continue

        calc = calculate_claim(claim, policy, penalty_modifiers=gate.modifiers)

        clip = accumulator.clip(benefit_key=claim.benefit_key, proposed_insurer_pay=calc.insurer_after_penalty)

        insurer_paid = clip.insurer_paid
        member_paid = round(calc.member_total_pre_limits + clip.member_extra, 2)

        if gate.has_penalty:
            decision = Decision.PAYABLE_WITH_PENALTY
            reason = " | ".join(gate.reasons) if gate.reasons else "Payable with penalty."
        else:
            decision = Decision.PAYABLE
            reason = "Payable per Table of Benefits."
            if clip.member_extra > 0:
                reason = "Payable; member absorbs amount beyond benefit sub-limit / aggregate."

        reasoning: list[ReasoningStep] = []
        reasoning.extend(gate.reasoning)
        reasoning.extend(calc.reasoning)
        reasoning.extend(clip.reasoning)
        reasoning.append(
            ReasoningStep(
                label="totals",
                value={
                    "billed": calc.billed,
                    "eligible": calc.eligible,
                    "deductible": calc.deductible_applied,
                    "member_coinsurance": calc.member_coinsurance_amount,
                    "penalty": calc.penalty_amount,
                    "insurer_paid": insurer_paid,
                    "member_paid": member_paid,
                },
                source="engine",
            )
        )

        requires_review, review_reason = _review_metadata(claim, policy, member)
        settlements.append(
            ClaimSettlement(
                claim_id=claim.claim_id,
                service_date=claim.service_date,
                benefit_key=claim.benefit_key,
                network_status=claim.network_status,
                billed_amount=calc.billed,
                eligible_amount=calc.eligible,
                deductible_applied=calc.deductible_applied,
                coinsurance_member=calc.member_coinsurance_amount,
                coinsurance_insurer=calc.insurer_after_penalty,
                penalty_amount=calc.penalty_amount,
                insurer_paid=insurer_paid,
                member_paid=member_paid,
                decision=decision,
                reason=reason,
                reasoning=reasoning,
                requires_review=requires_review,
                review_reason=review_reason,
            )
        )

        insurer_total = round(insurer_total + insurer_paid, 2)
        member_total = round(member_total + member_paid, 2)

    return SettlementReport(
        policy_ref=policy.policy_ref,
        plan_year_start=policy.plan_year_start,
        plan_year_end=policy.plan_year_end,
        settlements=settlements,
        insurer_total=insurer_total,
        member_total=member_total,
        aggregate_limit=policy.aggregate_annual_limit,
        aggregate_remaining=accumulator.aggregate_remaining,
        metadata={
            "accumulator_snapshot": accumulator.snapshot(),
            "claim_count": len(ordered),
        },
    )
