"""Enrich claims with derived fields via the LLMService.

Two enrichers — both pure functions over (claims, context, service). Never overwrite raw fields.

  - enrich_preexisting_links: sets `pre_existing_link` based on diagnosis vs the member's
    declared chronic condition (drives §4.2 waiting-period rules). Carries confidence +
    KB evidence + requires_review.
  - enrich_category_flags:    sets `category_flags` based on diagnosis vs the policy's
    `not_covered_condition` rule categories (drives §4.1 general exclusions).
"""

from __future__ import annotations

from .models.claim import Claim, PreExistingLink
from .models.member import MemberContext
from .models.policy import PolicyConfig
from .services.llm_service import LLMService


def enrich_preexisting_links(
    claims: list[Claim],
    member: MemberContext,
    service: LLMService,
) -> list[Claim]:
    """For each claim with a diagnosis and no pre_existing_link, ask the service to classify."""

    if not member.declared_chronic_conditions:
        return claims
    out: list[Claim] = []
    for c in claims:
        if c.pre_existing_link is not None or not c.diagnosis:
            out.append(c)
            continue

        # Combine results across all declared conditions; any positive flips the answer.
        any_related = False
        worst_confidence = "high"
        any_review = False
        all_evidence: list[str] = []
        reasonings: list[str] = []
        for cond in member.declared_chronic_conditions:
            result = service.classify_preexisting_link(c.diagnosis, cond)
            reasonings.append(f"vs '{cond}': {result.reasoning}")
            all_evidence.extend(result.evidence_ids)
            if result.is_related:
                any_related = True
            if result.requires_review:
                any_review = True
            # Two-level confidence: any "low" across conditions makes the overall "low".
            if result.confidence != "high":
                worst_confidence = "low"

        out.append(
            c.model_copy(
                update={
                    "pre_existing_link": PreExistingLink(
                        is_related=any_related,
                        reasoning="; ".join(reasonings),
                        source=f"llm:{service.provider_name}",
                        confidence=worst_confidence,
                        evidence_ids=list(dict.fromkeys(all_evidence)),  # dedupe, preserve order
                        requires_review=any_review,
                    )
                }
            )
        )
    return out


def _category_flags_from_policy(policy: PolicyConfig) -> list[str]:
    """Collect the `condition_flag` values declared by `not_covered_condition` rules.

    Excludes 'pre_existing' — handled separately by enrich_preexisting_links.
    """

    flags: list[str] = []
    for r in policy.exclusion_rules:
        if r.type != "not_covered_condition":
            continue
        flag = r.params.get("condition_flag")
        if not flag or flag == "pre_existing":
            continue
        if flag not in flags:
            flags.append(flag)
    return flags


def enrich_category_flags(
    claims: list[Claim],
    policy: PolicyConfig,
    service: LLMService,
) -> list[Claim]:
    """Ask the classifier which `not_covered_condition` categories the diagnosis matches."""

    categories = _category_flags_from_policy(policy)
    if not categories:
        return claims
    out: list[Claim] = []
    for c in claims:
        if c.category_flags or not c.diagnosis:
            out.append(c)
            continue
        result = service.classify_claim_categories(c.diagnosis, categories)
        out.append(
            c.model_copy(
                update={
                    "category_flags": result.flags,
                    "category_flags_evidence_ids": result.evidence_ids,
                    "category_flags_confidence": result.confidence,
                    "category_flags_requires_review": result.requires_review,
                    "category_flags_reasoning": result.reasoning or None,
                }
            )
        )
    return out
