"""Exclusion gate in isolation — fed claims designed to hit each rule."""

from __future__ import annotations

from datetime import date

import pytest

from adjudication.engine.exclusions import _add_months, evaluate_exclusions
from adjudication.models.claim import Claim, NetworkStatus, PreAuthStatus, PreExistingLink


def _claim(**overrides) -> Claim:
    defaults = dict(
        claim_id="X",
        service_date=date(2025, 2, 1),
        benefit_key="outpatient_consultation",
        network_status=NetworkStatus.IN_NETWORK,
        billed_amount=100.0,
        eligible_amount=100.0,
        preauth_status=PreAuthStatus.NOT_APPLICABLE,
        diagnosis="Acute viral illness",
        pre_existing_link=None,
    )
    defaults.update(overrides)
    return Claim(**defaults)


def test_waiting_period_blocks_preexisting_within_six_months(policy):
    # C2-like: 10 Mar 2025, asthma (pre-existing). Policy starts 1 Jan 2025.
    c = _claim(
        claim_id="T-WP",
        service_date=date(2025, 3, 10),
        pre_existing_link=PreExistingLink(is_related=True, reasoning="Asthma review", source="manual"),
    )
    out = evaluate_exclusions(c, policy)
    assert out.excluded is True
    assert any("waiting period" in r.lower() for r in out.reasons)


def test_waiting_period_lifts_after_six_months(policy):
    # C3-like: 5 Aug 2025, asthma — past the 1 Jul 2025 cutoff.
    c = _claim(
        claim_id="T-WP-OK",
        service_date=date(2025, 8, 5),
        pre_existing_link=PreExistingLink(is_related=True, reasoning="Asthma review", source="manual"),
    )
    out = evaluate_exclusions(c, policy)
    assert out.excluded is False


def test_waiting_period_not_applied_to_unrelated_acute_claim(policy):
    c = _claim(
        claim_id="T-ACUTE",
        service_date=date(2025, 2, 15),
        pre_existing_link=PreExistingLink(is_related=False, reasoning="Influenza is acute", source="manual"),
    )
    out = evaluate_exclusions(c, policy)
    assert out.excluded is False


def test_pharmacy_oon_is_not_covered(policy):
    c = _claim(
        claim_id="T-OON-RX",
        service_date=date(2025, 11, 20),
        benefit_key="pharmacy",
        network_status=NetworkStatus.OUT_OF_NETWORK,
    )
    out = evaluate_exclusions(c, policy)
    assert out.excluded is True
    assert any("out-of-network" in r.lower() or "not covered" in r.lower() for r in out.reasons)


def test_preauth_penalty_modifier_on_inpatient_without_preauth(policy):
    c = _claim(
        claim_id="T-PREAUTH",
        service_date=date(2025, 10, 3),
        benefit_key="inpatient_surgery",
        billed_amount=18000.0,
        eligible_amount=18000.0,
        preauth_status=PreAuthStatus.NOT_OBTAINED,
    )
    out = evaluate_exclusions(c, policy)
    assert out.excluded is False  # not a hard exclude
    assert out.has_penalty is True
    assert any(m["kind"] == "preauth_penalty" and abs(m["penalty_pct"] - 0.20) < 1e-9 for m in out.modifiers)


def test_add_months_handles_month_lengths():
    assert _add_months(date(2025, 1, 31), 1) == date(2025, 2, 28)
    assert _add_months(date(2025, 1, 1), 6) == date(2025, 7, 1)


def test_not_covered_condition_fires_on_category_flag(policy):
    c = _claim(
        claim_id="T-COSMETIC",
        service_date=date(2025, 5, 1),
        benefit_key="outpatient_consultation",
        diagnosis="Elective cosmetic rhinoplasty (purely aesthetic)",
        category_flags=["cosmetic"],
    )
    out = evaluate_exclusions(c, policy)
    assert out.excluded is True
    assert any("cosmetic" in r.lower() or "4.1" in r.lower() for r in out.reasons)


def test_not_covered_condition_does_not_fire_without_flag(policy):
    c = _claim(
        claim_id="T-NORMAL",
        service_date=date(2025, 5, 1),
        benefit_key="outpatient_consultation",
        diagnosis="Routine GP consultation",
        category_flags=[],
    )
    out = evaluate_exclusions(c, policy)
    assert out.excluded is False
