"""Full pipeline on the Karim scenario — assert the six expected outcomes + year totals."""

from __future__ import annotations

from adjudication.engine.adjudicator import adjudicate
from adjudication.models.settlement import Decision


def _by_id(report):
    return {s.claim_id: s for s in report.settlements}


def test_karim_full_pipeline(policy, karim_claims):
    report = adjudicate(karim_claims, policy)
    s = _by_id(report)

    # C1 — Outpatient, IN, AED 300 -> insurer 225, member 75
    assert s["C1"].decision == Decision.PAYABLE
    assert s["C1"].insurer_paid == 225.00
    assert s["C1"].member_paid == 75.00

    # C2 — Outpatient consultation, asthma within 6mo waiting period -> EXCLUDED
    assert s["C2"].decision == Decision.EXCLUDED
    assert s["C2"].insurer_paid == 0.00
    assert s["C2"].member_paid == 400.00

    # C3 — Outpatient consultation, asthma but service date 5 Aug 2025 (after waiting period)
    assert s["C3"].decision == Decision.PAYABLE
    assert s["C3"].insurer_paid == 315.00
    assert s["C3"].member_paid == 85.00

    # C4 — Physiotherapy IN AED 3000, E1 override 10% -> insurer 2700, member 300
    assert s["C4"].decision == Decision.PAYABLE
    assert s["C4"].insurer_paid == 2700.00
    assert s["C4"].member_paid == 300.00

    # C5 — Inpatient elective without preauth, 20% penalty
    assert s["C5"].decision == Decision.PAYABLE_WITH_PENALTY
    assert s["C5"].insurer_paid == 14400.00
    assert s["C5"].member_paid == 3600.00
    assert s["C5"].penalty_amount == 3600.00

    # C6 — Pharmacy OON -> NOT COVERED
    assert s["C6"].decision == Decision.EXCLUDED
    assert s["C6"].insurer_paid == 0.00
    assert s["C6"].member_paid == 500.00

    # Totals
    assert report.insurer_total == 17640.00
    assert report.member_total == 4960.00
    assert report.insurer_total + report.member_total == sum(c.billed_amount for c in karim_claims)


def test_settlement_carries_reasoning_chain(policy, karim_claims):
    report = adjudicate(karim_claims, policy)
    for s in report.settlements:
        assert s.reasoning, f"claim {s.claim_id}: missing reasoning chain"


def test_category_classifier_review_surfaces_when_material(policy, llm_service):
    """A claim whose category classifier returned non-`high` confidence AND has a
    `not_covered_condition` rule in scope must propagate the review flag into the
    settlement. Karim's policy has §4.1 rules → categories are always material here."""

    from datetime import date

    from adjudication.engine.adjudicator import adjudicate
    from adjudication.enrichment import enrich_category_flags
    from adjudication.models.claim import Claim, NetworkStatus, PreAuthStatus

    # Ambiguity marker ("etiology unclear") tells the fake classifier to return low confidence
    # while a keyword still fires the flag — exactly the case a human should look at.
    ambiguous = Claim(
        claim_id="A1",
        service_date=date(2025, 8, 15),
        benefit_key="outpatient_consultation",
        network_status=NetworkStatus.IN_NETWORK,
        billed_amount=4500.0,
        preauth_status=PreAuthStatus.NOT_APPLICABLE,
        diagnosis="Elective cosmetic procedure, etiology unclear",
    )
    enriched = enrich_category_flags([ambiguous], policy, llm_service)
    assert enriched[0].category_flags == ["cosmetic"]
    assert enriched[0].category_flags_confidence == "low"
    assert enriched[0].category_flags_requires_review is True

    report = adjudicate(enriched, policy)
    s = report.settlements[0]
    assert s.requires_review is True
    assert s.review_reason is not None
    assert "Category classifier confidence=low" in s.review_reason


def test_review_flag_suppressed_when_verdict_is_not_material(policy, karim_claims):
    """Regression: a claim excluded by a non-pre-existing rule (e.g. OON pharmacy) must NOT
    be flagged for review just because the pre-existing classifier returned non-`high`
    confidence on a low-information diagnosis. The verdict had no effect on the outcome.
    """

    report = adjudicate(karim_claims, policy)
    s = next(s for s in report.settlements if s.claim_id == "C6")
    assert s.decision.value == "excluded"
    assert s.requires_review is False, (
        f"C6 (OON pharmacy) excluded by a deterministic rule — review flag must be "
        f"suppressed. Got review_reason={s.review_reason!r}"
    )
    # Same protection for any C-row past the waiting period.
    c3 = next(s for s in report.settlements if s.claim_id == "C3")
    if c3.decision.value == "payable" and c3.requires_review:
        raise AssertionError(
            "C3 service date is past the §4.2 waiting period; review flag must be suppressed."
        )
