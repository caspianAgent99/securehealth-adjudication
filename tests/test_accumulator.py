"""Sub-limit and aggregate clipping, order-dependent."""

from __future__ import annotations

from adjudication.engine.accumulator import LimitAccumulator


def test_sub_limit_clips_payment(policy):
    acc = LimitAccumulator(policy)
    # Physiotherapy effective sub-limit is 4000 after E1
    first = acc.clip(benefit_key="physiotherapy", proposed_insurer_pay=2700.0)
    assert first.insurer_paid == 2700.0
    assert first.member_extra == 0.0

    second = acc.clip(benefit_key="physiotherapy", proposed_insurer_pay=2000.0)
    # 4000 - 2700 = 1300 remaining
    assert second.insurer_paid == 1300.0
    assert second.member_extra == 700.0
    assert acc.sub_remaining("physiotherapy") == 0.0


def test_aggregate_limit_clips_payment(policy):
    acc = LimitAccumulator(policy)
    # Pay just under the aggregate
    acc.clip(benefit_key="inpatient_surgery", proposed_insurer_pay=249_000.0)
    assert acc.aggregate_remaining == 1000.0
    out = acc.clip(benefit_key="inpatient_surgery", proposed_insurer_pay=5000.0)
    assert out.insurer_paid == 1000.0
    assert out.member_extra == 4000.0
    assert acc.aggregate_remaining == 0.0


def test_inpatient_has_no_own_sub_limit(policy):
    acc = LimitAccumulator(policy)
    assert acc.sub_remaining("inpatient_surgery") is None
