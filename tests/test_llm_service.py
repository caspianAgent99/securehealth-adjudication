"""Tests for the LLMService facade — clinical-KB grounding + confidence + review flag."""

from __future__ import annotations

from adjudication.services.clinical_kb import ClinicalKB, ExclusionCategoryKB, KBRow
from adjudication.services.llm_service import LLMService

from ._fakes import FakeAnthropicProvider


def _kb_with_asthma() -> ClinicalKB:
    return ClinicalKB(
        [
            KBRow(id="ASTHMA-001", group="asthma", indicator="wheezing", relation="symptom"),
            KBRow(id="ASTHMA-006", group="asthma", indicator="asthma review", relation="monitoring"),
            KBRow(id="ASTHMA-007", group="asthma", indicator="inhaler or bronchodilator refill", relation="treatment"),
        ]
    )


def _service() -> LLMService:
    return LLMService(provider=FakeAnthropicProvider(), kb=_kb_with_asthma())


def test_high_confidence_when_kb_indicator_matches():
    svc = _service()
    # Diagnosis mentions both 'asthma review' (ASTHMA-006) and 'wheezing' (ASTHMA-001),
    # both of which are full-substring KB indicators in our test KB.
    r = svc.classify_preexisting_link("Routine asthma review for episodic wheezing", "asthma")
    assert r.is_related is True
    assert r.confidence == "high"
    assert r.requires_review is False
    assert "ASTHMA-006" in r.evidence_ids
    assert "ASTHMA-001" in r.evidence_ids


def test_low_when_condition_named_but_no_kb_indicator_matched():
    svc = _service()
    r = svc.classify_preexisting_link("Asthma-related concern, no detail provided", "asthma")
    assert r.is_related is True
    assert r.confidence == "low"
    assert r.requires_review is True
    assert r.evidence_ids == []


def test_high_negation_takes_precedence():
    svc = _service()
    r = svc.classify_preexisting_link("Acute influenza unrelated to asthma", "asthma")
    assert r.is_related is False
    assert r.confidence == "high"
    assert r.requires_review is False


def test_low_when_no_overlap_at_all():
    svc = _service()
    r = svc.classify_preexisting_link("Wrist sprain after fall", "asthma")
    assert r.is_related is False
    assert r.confidence == "low"
    assert r.requires_review is True


def test_evidence_ids_are_filtered_against_known_kb():
    # If a provider hallucinates a KB id, the service drops it.
    class HallucinatingProvider:
        name = "test"

        def propose_policy(self, text): raise NotImplementedError
        def classify_claim_categories(self, dx, cats, kb_block): raise NotImplementedError
        def classify_preexisting_link(self, dx, cond, kb_block):
            return {
                "is_related": True,
                "confidence": "high",
                "evidence_ids": ["ASTHMA-001", "MADE-UP-999"],
                "reasoning": "test",
            }

    svc = LLMService(provider=HallucinatingProvider(), kb=_kb_with_asthma())
    r = svc.classify_preexisting_link("wheezing", "asthma")
    assert r.evidence_ids == ["ASTHMA-001"]


def test_confidence_normalised_to_low_on_any_non_high_string():
    """Two-level rule: anything that isn't the literal string "high" collapses to "low".

    Covers garbage, unexpected enums, mid-stream legacy values — every non-"high" string
    routes to review. The service has only one anchor: the literal "high".
    """

    class NonHighProvider:
        name = "test"

        def __init__(self, value):
            self._value = value

        def propose_policy(self, text): raise NotImplementedError
        def classify_claim_categories(self, dx, cats, kb_block): raise NotImplementedError
        def classify_preexisting_link(self, dx, cond, kb_block):
            return {"is_related": True, "confidence": self._value, "evidence_ids": [], "reasoning": ""}

    for non_high in ("WHATEVER", "moderate", "MAYBE", "uncertain", "0.85", ""):
        svc = LLMService(provider=NonHighProvider(non_high), kb=_kb_with_asthma())
        r = svc.classify_preexisting_link("wheezing", "asthma")
        assert r.confidence == "low", f"expected 'low' for input {non_high!r}, got {r.confidence!r}"
        assert r.requires_review is True


# -----------------------------------------------------------------------------
# Category classifier (KB-grounded, parallel to the pre-existing classifier)
# -----------------------------------------------------------------------------

def _exclusion_kb_with_cosmetic_and_experimental() -> ExclusionCategoryKB:
    return ExclusionCategoryKB(
        [
            KBRow(id="COSMETIC-001", group="cosmetic",
                  indicator="rhinoplasty (cosmetic)", relation="procedure"),
            KBRow(id="COSMETIC-002", group="cosmetic",
                  indicator="liposuction", relation="procedure"),
            KBRow(id="EXPERIM-001", group="experimental",
                  indicator="experimental therapy", relation="treatment"),
        ]
    )


def _service_with_exclusion_kb() -> LLMService:
    return LLMService(
        provider=FakeAnthropicProvider(),
        kb=_kb_with_asthma(),
        exclusion_kb=_exclusion_kb_with_cosmetic_and_experimental(),
    )


def test_category_classifier_cites_kb_evidence():
    svc = _service_with_exclusion_kb()
    r = svc.classify_claim_categories(
        "Elective rhinoplasty (cosmetic) requested by patient",
        categories=["cosmetic", "self_inflicted", "experimental"],
    )
    assert "cosmetic" in r.flags
    assert "COSMETIC-001" in r.evidence_ids
    assert r.kb_size == 3   # KB had 3 rows across all requested categories


def test_category_classifier_evidence_filtered_to_known_kb_ids():
    class HallucinatingCategoryProvider:
        name = "test"

        def propose_policy(self, text): raise NotImplementedError
        def classify_preexisting_link(self, dx, cond, kb_block): raise NotImplementedError
        def classify_claim_categories(self, dx, cats, kb_block):
            return {
                "flags": ["cosmetic"],
                "evidence_ids": ["COSMETIC-001", "MADE-UP-CAT-999"],
                "reasoning": "test",
            }

    svc = LLMService(
        provider=HallucinatingCategoryProvider(),
        kb=_kb_with_asthma(),
        exclusion_kb=_exclusion_kb_with_cosmetic_and_experimental(),
    )
    r = svc.classify_claim_categories("anything", categories=["cosmetic"])
    assert r.flags == ["cosmetic"]
    assert r.evidence_ids == ["COSMETIC-001"]


def test_category_classifier_flags_filtered_to_requested():
    class OverreachingProvider:
        name = "test"

        def propose_policy(self, text): raise NotImplementedError
        def classify_preexisting_link(self, dx, cond, kb_block): raise NotImplementedError
        def classify_claim_categories(self, dx, cats, kb_block):
            return {
                "flags": ["cosmetic", "dental_uncovered_extra"],   # second not in cats
                "evidence_ids": [],
                "reasoning": "test",
            }

    svc = LLMService(
        provider=OverreachingProvider(),
        kb=_kb_with_asthma(),
        exclusion_kb=_exclusion_kb_with_cosmetic_and_experimental(),
    )
    r = svc.classify_claim_categories("anything", categories=["cosmetic"])
    assert r.flags == ["cosmetic"]
    assert "dental_uncovered_extra" not in r.flags


def test_category_classifier_empty_categories_short_circuits():
    svc = _service_with_exclusion_kb()
    r = svc.classify_claim_categories("Elective rhinoplasty", categories=[])
    assert r.flags == []
    assert r.evidence_ids == []
    assert r.kb_size == 0
