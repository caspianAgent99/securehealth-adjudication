"""Malformed config and misaligned rows must fail loudly with precise messages."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from adjudication.validation.claim_validator import ClaimValidationError, validate_claim_rows
from adjudication.validation.policy_validator import PolicyValidationError, validate_policy_config


def test_policy_validator_catches_bad_coinsurance(tmp_path: Path):
    bad = {
        "policy_ref": "test",
        "plan_year_start": "2025-01-01",
        "plan_year_end": "2025-12-31",
        "policy_start_date": "2025-01-01",
        "aggregate_annual_limit": 100.0,
        "approved_by": "ops",
        "approved_at": "2025-01-01",
        "benefits": [
            {
                "key": "x",
                "name": "X",
                "annual_sub_limit": 100.0,
                "in_network_coinsurance": 1.5,  # invalid > 1
                "out_of_network_coinsurance": 0.2,
                "deductible": 0.0,
                "requires_preauth": False,
            }
        ],
    }
    with pytest.raises(PolicyValidationError):
        validate_policy_config(bad)


def test_policy_validator_catches_endorsement_unknown_benefit():
    bad = {
        "policy_ref": "test",
        "plan_year_start": "2025-01-01",
        "plan_year_end": "2025-12-31",
        "policy_start_date": "2025-01-01",
        "aggregate_annual_limit": 100.0,
        "approved_by": "ops",
        "approved_at": "2025-01-01",
        "benefits": [
            {
                "key": "x",
                "name": "X",
                "annual_sub_limit": 100.0,
                "in_network_coinsurance": 0.1,
                "out_of_network_coinsurance": 0.2,
                "deductible": 0.0,
                "requires_preauth": False,
            }
        ],
        "endorsements": [{"id": "E1", "benefit_key": "DOES_NOT_EXIST", "overrides": {}, "source": "s"}],
    }
    with pytest.raises(PolicyValidationError):
        validate_policy_config(bad)


def test_claim_validator_catches_unknown_benefit(policy):
    rows = [
        {
            "claim_id": "X1",
            "service_date": "2025-02-15",
            "benefit_key": "UNKNOWN",
            "network_status": "in_network",
            "billed_amount": "100",
            "preauth_status": "not_applicable",
        }
    ]
    with pytest.raises(ClaimValidationError) as exc:
        validate_claim_rows(rows, policy)
    assert any("not found in policy" in m for m in exc.value.errors)


def test_claim_validator_catches_unparseable_date(policy):
    rows = [
        {
            "claim_id": "X1",
            "service_date": "not-a-date",
            "benefit_key": "outpatient_consultation",
            "network_status": "in_network",
            "billed_amount": "100",
            "preauth_status": "not_applicable",
        }
    ]
    with pytest.raises(ClaimValidationError) as exc:
        validate_claim_rows(rows, policy)
    assert any("date" in m.lower() for m in exc.value.errors)
