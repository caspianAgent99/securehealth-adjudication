"""Enrich claims with derived fields via the LLMService.

Two enrichers — both pure functions over (claims, context, service). Never overwrite raw fields.

  - enrich_preexisting_links: sets `pre_existing_link` based on diagnosis vs the member's
    declared chronic condition (drives §4.2 waiting-period rules). Carries confidence +
    KB evidence + requires_review.
  - enrich_category_flags:    sets `category_flags` based on diagnosis vs the policy's
    `not_covered_condition` rule categories (drives §4.1 general exclusions).
  - enrich_admission_type:    sets `admission_type` (elective/emergency) for claims under a
    pre-auth-required benefit (drives GC-3's "emergencies excepted" carve-out).
"""

from __future__ import annotations

from .models.claim import AdmissionType, Claim, PreExistingLink
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


def enrich_admission_type(
    claims: list[Claim],
    policy: PolicyConfig,
    service: LLMService,
) -> list[Claim]:
    """Classify elective vs emergency for claims under a pre-auth-required benefit.

    Only those claims can attract the GC-3 no-pre-auth penalty, and only an emergency is
    exempt — so that is the only place the distinction changes an outcome. Claims under
    other benefits (or already classified, or with no diagnosis) are left untouched at the
    UNKNOWN default, which the engine treats as penalisable.
    """

    out: list[Claim] = []
    for c in claims:
        try:
            benefit = policy.benefit(c.benefit_key)
        except KeyError:
            out.append(c)
            continue
        if (
            not benefit.requires_preauth
            or c.admission_type is not AdmissionType.UNKNOWN
            or not c.diagnosis
        ):
            out.append(c)
            continue
        result = service.classify_admission_type(c.diagnosis, benefit.name)
        out.append(
            c.model_copy(
                update={
                    "admission_type": AdmissionType(result.admission_type),
                    "admission_type_confidence": result.confidence,
                    "admission_type_requires_review": result.requires_review,
                    "admission_type_reasoning": result.reasoning or None,
                }
            )
        )
    return out
