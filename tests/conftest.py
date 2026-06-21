"""Shared fixtures.

Tests use the in-test `FakeAnthropicProvider` (see `tests/_fakes.py`) so the suite
runs offline. Production code talks to real Claude.
"""

from __future__ import annotations

import pytest

from adjudication.config import SETTINGS
from adjudication.enrichment import enrich_preexisting_links
from adjudication.extraction.claim_pdf import PDFClaimExtractor
from adjudication.models.policy import PolicyConfig
from adjudication.services.clinical_kb import ClinicalKB
from adjudication.services.llm_service import LLMService
from adjudication.validation.claim_validator import validate_claim_rows
from adjudication.validation.policy_validator import validate_policy_config

from ._fakes import PLAN_B_FIXTURE, FakeAnthropicProvider


@pytest.fixture(scope="session")
def policy() -> PolicyConfig:
    return validate_policy_config(PLAN_B_FIXTURE)


@pytest.fixture(scope="session")
def llm_service() -> LLMService:
    kb_path = SETTINGS.clinical_kb_path
    kb = ClinicalKB.load(kb_path) if kb_path.exists() else ClinicalKB.empty()
    return LLMService(provider=FakeAnthropicProvider(), kb=kb)


@pytest.fixture(scope="session")
def karim_sheet():
    return PDFClaimExtractor().extract(SETTINGS.claims_path)


@pytest.fixture(scope="session")
def karim_claims(policy: PolicyConfig, karim_sheet, llm_service: LLMService):
    claims = validate_claim_rows(karim_sheet.rows, policy)
    if karim_sheet.member.declared_chronic_conditions:
        claims = enrich_preexisting_links(claims, karim_sheet.member, llm_service)
    return claims
