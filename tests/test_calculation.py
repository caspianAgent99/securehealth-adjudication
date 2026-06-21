"""Per-claim math + endorsement override, hand-picked numbers."""

from __future__ import annotations

from datetime import date

from adjudication.engine.calculation import calculate_claim
from adjudication.models.claim import Claim, NetworkStatus, PreAuthStatus


def _make(benefit_key: str, billed: float, **overrides) -> Claim:
    defaults = dict(
        claim_id="X",
        service_date=date(2025, 2, 15),
        benefit_key=benefit_key,
        network_status=NetworkStatus.IN_NETWORK,
        billed_amount=billed,
        eligible_amount=billed,
        preauth_status=PreAuthStatus.NOT_APPLICABLE,
        diagnosis=None,
        pre_existing_link=None,
    )
    defaults.update(overrides)
    return Claim(**defaults)


def test_outpatient_consultation_in_network_with_deductible(policy):
    # AED 300, deductible 50, IN coins 10% -> insurer 225, member 75
    claim = _make("outpatient_consultation", 300.0)
    r = calculate_claim(claim, policy)
    assert r.eligible == 300.0
    assert r.deductible_applied == 50.0
    assert r.post_deductible == 250.0
    assert r.member_coinsurance_amount == 25.0
    assert r.insurer_pre_penalty == 225.0
    assert r.member_total_pre_limits == 75.0


def test_physiotherapy_E1_override_applied(policy):
    # Base 20% in-network; E1 -> 10%. AED 3000 -> insurer 2700, member 300.
    claim = _make("physiotherapy", 3000.0)
    r = calculate_claim(claim, policy)
    assert r.eligible == 3000.0
    assert r.deductible_applied == 0.0
    assert r.member_coinsurance_pct == 0.10
    assert r.insurer_pre_penalty == 2700.0
    assert r.member_total_pre_limits == 300.0
    # endorsement steps must be present in the reasoning
    labels = [s.label for s in r.reasoning]
    assert any(lbl.startswith("endorsement:E1:") for lbl in labels)


def test_inpatient_full_cover_in_network(policy):
    # 0% coins, no deductible -> insurer 18000, member 0 (before penalty)
    claim = _make("inpatient_surgery", 18000.0, preauth_status=PreAuthStatus.OBTAINED)
    r = calculate_claim(claim, policy)
    assert r.member_coinsurance_pct == 0.0
    assert r.insurer_pre_penalty == 18000.0
    assert r.member_total_pre_limits == 0.0


def test_inpatient_with_20pct_penalty(policy):
    # Penalty modifier reduces insurer share by 20% of post-deductible insurer share.
    claim = _make("inpatient_surgery", 18000.0, preauth_status=PreAuthStatus.NOT_OBTAINED)
    r = calculate_claim(
        claim, policy, penalty_modifiers=[{"kind": "preauth_penalty", "rule_id": "PREAUTH-PENALTY-20", "penalty_pct": 0.20}]
    )
    assert r.insurer_pre_penalty == 18000.0
    assert r.penalty_amount == 3600.0
    assert r.insurer_after_penalty == 14400.0
    assert r.member_total_pre_limits == 3600.0
