"""FastAPI dependency providers — single place where we load the frozen artifacts and the LLMService."""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

from fastapi import HTTPException

from adjudication.config import SETTINGS
from adjudication.enrichment import (
    enrich_admission_type,
    enrich_category_flags,
    enrich_preexisting_links,
)
from adjudication.extraction.claim_pdf import PDFClaimExtractor
from adjudication.models.policy import PolicyConfig
from adjudication.services.llm_service import LLMService
from adjudication.validation.claim_validator import ClaimValidationError, validate_claim_rows
from adjudication.validation.policy_validator import PolicyValidationError, validate_policy_config


def _read_policy_from_disk(path: Path) -> PolicyConfig:
    if not path.exists():
        raise HTTPException(
            status_code=503,
            detail={
                "error": "no_policy_approved",
                "message": (
                    "No policy is approved yet. Upload a policy PDF via POST /policy/draft/from-pdf "
                    "and lock it via POST /policy/lock."
                ),
                "expected_path": str(path),
            },
        )
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise HTTPException(status_code=500, detail=f"frozen policy invalid JSON: {e}") from e
    try:
        return validate_policy_config(data)
    except PolicyValidationError as e:
        raise HTTPException(status_code=500, detail={"policy_validation_errors": e.errors}) from e


@lru_cache(maxsize=1)
def get_policy() -> PolicyConfig:
    return _read_policy_from_disk(Path(SETTINGS.policy_path))


def get_policy_path() -> Path:
    return Path(SETTINGS.policy_path)


def get_draft_path() -> Path:
    return Path(SETTINGS.policy_path).with_name("_draft.json")


def get_current_claim_path() -> Path:
    """Where the most recent adjudication bundle is persisted, so refreshing the UI
    doesn't trigger another expensive LLM round-trip."""

    return Path(SETTINGS.claims_path).with_name("_last_run.json")


@lru_cache(maxsize=1)
def get_llm_service() -> LLMService:
    """Single LLMService for the whole process. Loads the clinical KB on first call."""

    return LLMService()


def get_default_claims_with_member():
    """Extract → validate → enrich. Returns (claims, member) so the adjudicator can
    use the member's inception date as the §4.2 anchor."""

    policy = get_policy()
    sheet = PDFClaimExtractor().extract(SETTINGS.claims_path)
    try:
        claims = validate_claim_rows(sheet.rows, policy)
    except ClaimValidationError as e:
        raise HTTPException(status_code=400, detail={"claim_validation_errors": e.errors}) from e
    service = get_llm_service()
    if sheet.member.declared_chronic_conditions:
        claims = enrich_preexisting_links(claims, sheet.member, service)
    claims = enrich_category_flags(claims, policy, service)
    claims = enrich_admission_type(claims, policy, service)
    return claims, sheet.member


def get_default_claims():
    """Back-compat shim: returns just the claims (no member). Older endpoints can keep using this."""

    claims, _ = get_default_claims_with_member()
    return claims


def reset_policy_cache() -> None:
    get_policy.cache_clear()
