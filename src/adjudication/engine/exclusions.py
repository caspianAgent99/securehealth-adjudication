"""The exclusion gate.

Runs before any math. For each claim, evaluates the policy's ExclusionRules and emits a
reason-bearing outcome: payable, excluded (hard), or payable_with_modifier (e.g. preauth penalty).
"""

from __future__ import annotations

import calendar
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from ..models.claim import Claim, NetworkStatus, PreAuthStatus
from ..models.member import MemberContext
from ..models.policy import ExclusionRule, PolicyConfig
from ..models.settlement import ReasoningStep


def _add_months(d: date, months: int) -> date:
    """Add `months` calendar months to date d (clamps day to month length)."""

    total = d.month - 1 + months
    year = d.year + total // 12
    month = total % 12 + 1
    day = min(d.day, calendar.monthrange(year, month)[1])
    return date(year, month, day)


@dataclass(frozen=True)
class GateOutcome:
    excluded: bool
    reasons: list[str] = field(default_factory=list)
    modifiers: list[dict[str, Any]] = field(default_factory=list)
    reasoning: list[ReasoningStep] = field(default_factory=list)

    @property
    def has_penalty(self) -> bool:
        return any(m.get("kind") == "preauth_penalty" for m in self.modifiers)


def _rule_applies_to_benefit(rule: ExclusionRule, claim: Claim) -> bool:
    return rule.applies_to_benefits is None or claim.benefit_key in rule.applies_to_benefits


def _waiting_anchor(policy: PolicyConfig, member: MemberContext | None) -> date:
    """The §4.2 waiting period anchors on the *member's* inception date when present;
    otherwise we fall back to the policy-wide `policy_start_date`."""

    if member is not None and member.inception_date is not None:
        return member.inception_date
    return policy.policy_start_date


def _waiting_end_for_rule(
    rule: ExclusionRule,
    policy: PolicyConfig,
    member: MemberContext | None = None,
) -> date | None:
    anchor = _waiting_anchor(policy, member)
    if "waiting_months" in rule.params:
        return _add_months(anchor, int(rule.params["waiting_months"]))
    if "waiting_days" in rule.params:
        from datetime import timedelta

        return anchor + timedelta(days=int(rule.params["waiting_days"]))
    return None


def preexisting_verdict_is_material(
    claim: Claim,
    policy: PolicyConfig,
    member: MemberContext | None = None,
) -> bool:
    """Would flipping `pre_existing_link.is_related` actually change this claim's
    excluded/payable outcome? If yes, the LLM's uncertainty matters and the claim
    should be flagged for review. If no, the verdict was moot (e.g. the claim is
    excluded by OON-pharmacy regardless of pre-existing status, or it's past the
    waiting period so §4.2 cannot fire either way).
    """

    if claim.pre_existing_link is None:
        return False
    actual = evaluate_exclusions(claim, policy, member=member)
    flipped_link = claim.pre_existing_link.model_copy(
        update={"is_related": not claim.pre_existing_link.is_related}
    )
    flipped = evaluate_exclusions(
        claim.model_copy(update={"pre_existing_link": flipped_link}),
        policy,
        member=member,
    )
    return actual.excluded != flipped.excluded


def category_verdict_is_material(
    claim: Claim,
    policy: PolicyConfig,
    member: MemberContext | None = None,
) -> bool:
    """Would flipping `claim.category_flags` actually change the outcome?

    Two flips to consider:
      A. clear all current flags (we may have over-flagged → claim might be payable),
      B. set every configured non-pre_existing flag (we may have missed one → claim might be excluded).
    Material iff either flip would change `excluded`.
    """

    actual = evaluate_exclusions(claim, policy, member=member)

    # Case A: drop all flags
    flipped_a = evaluate_exclusions(
        claim.model_copy(update={"category_flags": []}),
        policy,
        member=member,
    )
    if actual.excluded != flipped_a.excluded:
        return True

    # Case B: set every configured non-pre_existing flag
    configured: list[str] = []
    for rule in policy.exclusion_rules:
        if rule.type != "not_covered_condition":
            continue
        flag = rule.params.get("condition_flag")
        if not flag or flag == "pre_existing":
            continue
        if flag not in configured:
            configured.append(flag)
    if not configured:
        return False
    flipped_b = evaluate_exclusions(
        claim.model_copy(update={"category_flags": configured}),
        policy,
        member=member,
    )
    return actual.excluded != flipped_b.excluded


def _format_reason(rule: ExclusionRule, extra: dict[str, Any] | None = None) -> str:
    params = dict(rule.params)
    if "penalty_pct" in params:
        params["penalty_pct_display"] = f"{int(round(float(params['penalty_pct']) * 100))}%"
    if extra:
        params.update(extra)
    try:
        return rule.reason_template.format(**params)
    except (KeyError, IndexError):
        return rule.reason_template


def _eval_waiting_period(
    rule: ExclusionRule,
    claim: Claim,
    policy: PolicyConfig,
    member: MemberContext | None = None,
) -> tuple[bool, str | None]:
    """Returns (excluded, reason). The rule's condition_flag matches `pre_existing` today.

    Waiting-period anchor is the member's `inception_date` when present, else
    `policy.policy_start_date`.
    """

    flag = rule.params.get("condition_flag", "pre_existing")
    if flag == "pre_existing":
        link = claim.pre_existing_link
        if link is None or not link.is_related:
            return False, None
    else:
        return False, None  # unknown flag => no-op rather than false-positive

    waiting_end = _waiting_end_for_rule(rule, policy, member)
    if waiting_end is None:
        return False, None
    # Policy wording: "not payable for the first six (6) months from the Inception Date".
    # Service date strictly before waiting_end => excluded; on/after => payable.
    if claim.service_date < waiting_end:
        return True, _format_reason(rule)
    return False, None


def _eval_not_covered_oon(rule: ExclusionRule, claim: Claim) -> tuple[bool, str | None]:
    if claim.network_status == NetworkStatus.OUT_OF_NETWORK:
        return True, _format_reason(rule)
    return False, None


def _eval_preauth_penalty(rule: ExclusionRule, claim: Claim, benefit_requires_preauth: bool) -> tuple[
    dict[str, Any] | None, str | None
]:
    if not benefit_requires_preauth:
        return None, None
    if claim.preauth_status == PreAuthStatus.NOT_OBTAINED:
        return (
            {
                "kind": "preauth_penalty",
                "rule_id": rule.id,
                "penalty_pct": float(rule.params.get("penalty_pct", 0.0)),
            },
            _format_reason(rule),
        )
    return None, None


def evaluate_exclusions(
    claim: Claim,
    policy: PolicyConfig,
    member: MemberContext | None = None,
) -> GateOutcome:
    """Run all exclusion rules over one claim. Pure.

    `member` is optional and only matters for §4.2 waiting-period rules; if absent,
    the engine falls back to `policy.policy_start_date`.
    """

    benefit = policy.benefit(claim.benefit_key)

    # OON not-covered fallback even if no explicit rule:
    # if a benefit declares out_of_network_coinsurance = None, OON IS not covered.
    if claim.network_status == NetworkStatus.OUT_OF_NETWORK and benefit.out_of_network_coinsurance is None:
        return GateOutcome(
            excluded=True,
            reasons=[f"Out-of-network is not covered for benefit '{benefit.name}'."],
            reasoning=[
                ReasoningStep(
                    label="oon_not_covered",
                    value=True,
                    source=f"benefit:{benefit.key}",
                    note="out_of_network_coinsurance is null in the benefit config",
                )
            ],
        )

    excluded = False
    reasons: list[str] = []
    modifiers: list[dict[str, Any]] = []
    steps: list[ReasoningStep] = []

    for rule in policy.exclusion_rules:
        if not _rule_applies_to_benefit(rule, claim):
            continue

        if rule.type == "waiting_period":
            hit, reason = _eval_waiting_period(rule, claim, policy, member)
            if hit and reason:
                excluded = True
                reasons.append(reason)
                steps.append(ReasoningStep(label=f"rule:{rule.id}", value="excluded", source=f"rule:{rule.id}", note=reason))

        elif rule.type == "not_covered_oon":
            hit, reason = _eval_not_covered_oon(rule, claim)
            if hit and reason:
                excluded = True
                reasons.append(reason)
                steps.append(ReasoningStep(label=f"rule:{rule.id}", value="excluded", source=f"rule:{rule.id}", note=reason))

        elif rule.type == "not_covered_condition":
            flag = rule.params.get("condition_flag")
            if not flag:
                continue
            hit = False
            if flag == "pre_existing":
                link = claim.pre_existing_link
                hit = bool(link and link.is_related)
            else:
                hit = flag in (claim.category_flags or [])
            if hit:
                excluded = True
                reason = _format_reason(rule)
                reasons.append(reason)
                steps.append(
                    ReasoningStep(
                        label=f"rule:{rule.id}",
                        value="excluded",
                        source=f"rule:{rule.id}",
                        note=f"condition_flag={flag}",
                    )
                )

        elif rule.type == "preauth_penalty":
            modifier, reason = _eval_preauth_penalty(rule, claim, benefit.requires_preauth)
            if modifier is not None and reason is not None:
                modifiers.append(modifier)
                reasons.append(reason)
                steps.append(
                    ReasoningStep(
                        label=f"rule:{rule.id}",
                        value=f"penalty {int(modifier['penalty_pct'] * 100)}%",
                        source=f"rule:{rule.id}",
                        note=reason,
                    )
                )

    return GateOutcome(excluded=excluded, reasons=reasons, modifiers=modifiers, reasoning=steps)
