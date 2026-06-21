"""HTTP endpoints — thin wrappers over the library. No business logic here."""

from __future__ import annotations

import io
import json
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Body, Depends, File, HTTPException, UploadFile

from adjudication.config import SETTINGS
from adjudication.engine.adjudicator import adjudicate
from adjudication.enrichment import enrich_category_flags, enrich_preexisting_links
from adjudication.extraction.claim_pdf import PDFClaimExtractor
from adjudication.extraction.policy_llm import LLMPolicyExtractor
from adjudication.models.claim import Claim
from adjudication.models.policy import PolicyConfig
from adjudication.models.settlement import SettlementReport
from adjudication.reporting.json_report import report_to_json
from adjudication.reporting.table_report import report_to_table
from adjudication.validation.claim_validator import ClaimValidationError, validate_claim_rows
from adjudication.validation.policy_validator import PolicyValidationError, validate_policy_config

from .dependencies import (
    get_current_claim_path,
    get_default_claims,
    get_default_claims_with_member,
    get_draft_path,
    get_llm_service,
    get_policy,
    get_policy_path,
    reset_policy_cache,
)


router = APIRouter()


def _llm_error(e: Exception) -> HTTPException:
    """Translate any failure from the LLM layer (missing key, auth failure, bad model,
    unparseable response) into a clean 502 the UI can display, instead of a bare 500."""

    return HTTPException(
        status_code=502,
        detail={
            "error": "llm_unavailable",
            "message": str(e) or e.__class__.__name__,
            "hint": (
                "This step needs the Anthropic API. Set a valid ANTHROPIC_API_KEY "
                "(on Streamlit Cloud: Manage app → Settings → Secrets) and a valid "
                "ANTHROPIC_MODEL. The model currently configured is checked at call time."
            ),
        },
    )


def _llm_service_or_502():
    """Construct the LLMService, converting a missing/broken provider into a clean 502."""

    try:
        return get_llm_service()
    except HTTPException:
        raise
    except Exception as e:  # AnthropicProviderUnavailable, ImportError, etc.
        raise _llm_error(e) from e


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    tmp.replace(path)


@router.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/policy", response_model=PolicyConfig)
def get_policy_endpoint(policy: PolicyConfig = Depends(get_policy)) -> PolicyConfig:
    return policy


# ---------- HITL: draft + lock layer ----------

@router.get("/policy/state")
def policy_state() -> dict[str, Any]:
    """Single source of truth the UI uses to decide which screen to render.

    Returns one of:
        {"state": "empty"}                              — no draft, no frozen policy
        {"state": "draft", "draft": {...}}              — draft exists, awaiting lock
        {"state": "locked", "policy": {...}}            — frozen policy in force
    """

    policy_path = get_policy_path()
    draft_path = get_draft_path()
    if policy_path.exists():
        return {"state": "locked", "policy": json.loads(policy_path.read_text(encoding="utf-8"))}
    if draft_path.exists():
        return {"state": "draft", "draft": json.loads(draft_path.read_text(encoding="utf-8"))}
    return {"state": "empty"}


@router.get("/policy/draft")
def get_draft() -> dict[str, Any]:
    draft_path = get_draft_path()
    if not draft_path.exists():
        raise HTTPException(status_code=404, detail={"error": "no_draft"})
    return json.loads(draft_path.read_text(encoding="utf-8"))


@router.put("/policy/draft")
def put_draft(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
    """Persist the current HITL draft. Structural validation runs; semantic review is the user's job."""

    try:
        cfg = validate_policy_config(payload)
    except PolicyValidationError as e:
        raise HTTPException(status_code=400, detail={"validation_errors": e.errors}) from e
    draft_path = get_draft_path()
    _atomic_write_json(draft_path, cfg.model_dump(mode="json"))
    return {"saved_to": str(draft_path), "policy_ref": cfg.policy_ref}


@router.delete("/policy/draft")
def delete_draft() -> dict[str, Any]:
    draft_path = get_draft_path()
    existed = draft_path.exists()
    if existed:
        draft_path.unlink()
    return {"removed": existed}


@router.post("/policy/draft/from-pdf")
async def draft_from_pdf(file: UploadFile = File(...)) -> dict[str, Any]:
    """Run LLM extraction on an uploaded policy PDF and save the proposal as the editable draft.

    Returns the draft along with any validation errors so the UI can surface them inline.
    """

    content = await file.read()
    if file.filename and file.filename.lower().endswith(".pdf"):
        try:
            import pdfplumber

            with pdfplumber.open(io.BytesIO(content)) as pdf:
                text = "\n\n".join((page.extract_text() or "") for page in pdf.pages)
        except Exception as e:
            raise HTTPException(status_code=400, detail=f"failed to read PDF: {e}") from e
    else:
        try:
            text = content.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise HTTPException(status_code=400, detail="cannot decode policy file as UTF-8") from exc

    extractor = LLMPolicyExtractor(_llm_service_or_502())
    try:
        result = extractor.extract(text)
    except HTTPException:
        raise
    except Exception as e:
        raise _llm_error(e) from e
    if result.policy is None:
        raw = result.raw or {}
        # Save the raw proposal anyway so the user can fix structural issues in the editor.
        _atomic_write_json(get_draft_path(), raw)
        return {
            "draft": raw,
            "citations": result.citations,
            "validation_errors": result.validation_errors,
            "structurally_valid": False,
        }
    draft_dict = result.policy.model_dump(mode="json")
    _atomic_write_json(get_draft_path(), draft_dict)
    return {
        "draft": draft_dict,
        "citations": result.citations,
        "validation_errors": [],
        "structurally_valid": True,
    }


@router.post("/policy/lock")
def lock_policy() -> dict[str, Any]:
    """Promote the draft to the frozen policy. This is the HITL approval step."""

    draft_path = get_draft_path()
    if not draft_path.exists():
        raise HTTPException(status_code=404, detail={"error": "no_draft", "message": "Nothing to lock."})
    raw = json.loads(draft_path.read_text(encoding="utf-8"))
    try:
        cfg = validate_policy_config(raw)
    except PolicyValidationError as e:
        raise HTTPException(status_code=400, detail={"validation_errors": e.errors}) from e
    policy_path = get_policy_path()
    _atomic_write_json(policy_path, cfg.model_dump(mode="json"))
    draft_path.unlink(missing_ok=True)
    reset_policy_cache()
    # A re-lock implies the locked policy may have changed; previous claim run is now stale.
    current_claim = get_current_claim_path()
    if current_claim.exists():
        current_claim.unlink()
    return {"locked_to": str(policy_path), "policy_ref": cfg.policy_ref}


@router.post("/policy/unlock")
def unlock_policy() -> dict[str, Any]:
    """Move the frozen policy back into the editable draft slot (resume HITL editing)."""

    policy_path = get_policy_path()
    draft_path = get_draft_path()
    if not policy_path.exists():
        raise HTTPException(status_code=404, detail={"error": "no_policy_approved"})
    if draft_path.exists():
        raise HTTPException(
            status_code=409,
            detail={"error": "draft_exists", "message": "Discard the existing draft first (DELETE /policy/draft)."},
        )
    raw = json.loads(policy_path.read_text(encoding="utf-8"))
    _atomic_write_json(draft_path, raw)
    policy_path.unlink()
    reset_policy_cache()
    # Any persisted claim was adjudicated against the now-old policy → drop it.
    current_claim = get_current_claim_path()
    if current_claim.exists():
        current_claim.unlink()
    return {"draft_at": str(draft_path)}


@router.delete("/policy")
def delete_policy() -> dict[str, Any]:
    """Reset to the empty state — removes the locked policy AND any draft.

    No confirmation; the UI is responsible for prompting the user. Re-uploading
    the policy PDF is the way back. Cache is invalidated so the next request
    returns 503 `no_policy_approved`.
    """

    policy_path = get_policy_path()
    draft_path = get_draft_path()
    removed: dict[str, bool] = {"policy": False, "draft": False}
    if policy_path.exists():
        policy_path.unlink()
        removed["policy"] = True
    if draft_path.exists():
        draft_path.unlink()
        removed["draft"] = True
    # Resetting the policy invalidates any persisted claim adjudication.
    current_claim = get_current_claim_path()
    if current_claim.exists():
        current_claim.unlink()
        removed["claim"] = True
    reset_policy_cache()
    return {"removed": removed, "state": "empty"}


@router.get("/kb/{kb_id}")
def get_kb_row(kb_id: str) -> dict[str, Any]:
    """Look up a single KB row by id across both clinical KBs. 404 if unknown."""

    row = get_llm_service().find_kb_row(kb_id)
    if row is None:
        raise HTTPException(status_code=404, detail={"error": "kb_row_not_found", "id": kb_id})
    return row


@router.post("/policy/propose")
async def propose_policy(file: UploadFile = File(...)) -> dict[str, Any]:
    """Run LLM extraction on an uploaded policy doc. Does NOT freeze — that's /policy/approve."""

    content = await file.read()
    if file.filename and file.filename.lower().endswith(".pdf"):
        try:
            import pdfplumber

            with pdfplumber.open(io.BytesIO(content)) as pdf:
                text = "\n\n".join((page.extract_text() or "") for page in pdf.pages)
        except Exception as e:
            raise HTTPException(status_code=400, detail=f"failed to read PDF: {e}") from e
    else:
        try:
            text = content.decode("utf-8")
        except UnicodeDecodeError:
            raise HTTPException(status_code=400, detail="cannot decode policy file as UTF-8")

    extractor = LLMPolicyExtractor(_llm_service_or_502())
    try:
        result = extractor.extract(text)
    except HTTPException:
        raise
    except Exception as e:
        raise _llm_error(e) from e
    return {
        "proposed_policy": result.policy.model_dump(mode="json") if result.policy else None,
        "raw_proposal": result.raw,
        "citations": result.citations,
        "validation_errors": result.validation_errors,
    }


@router.post("/policy/approve")
def approve_policy(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
    """Freeze a reviewed config to disk."""

    try:
        cfg = validate_policy_config(payload)
    except PolicyValidationError as e:
        raise HTTPException(status_code=400, detail={"validation_errors": e.errors}) from e
    out_path = Path(SETTINGS.policy_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(cfg.model_dump(mode="json"), indent=2), encoding="utf-8")
    reset_policy_cache()
    return {"frozen_to": str(out_path), "policy_ref": cfg.policy_ref}


@router.post("/claims/extract")
async def extract_claims(
    file: UploadFile = File(...),
    policy: PolicyConfig = Depends(get_policy),
) -> dict[str, Any]:
    """Upload a claim PDF; return member context, normalized rows, and typed claims.

    PDF is the only supported claim format. Validation runs after extraction; the
    pre-existing flag is enriched via the LLM classifier (mock by default).
    """

    filename = (file.filename or "").lower()
    if not filename.endswith(".pdf"):
        raise HTTPException(status_code=400, detail="unsupported file type; only .pdf claim sheets are accepted")
    content = await file.read()
    sheet = PDFClaimExtractor().extract_from_bytes(content)

    try:
        claims = validate_claim_rows(sheet.rows, policy)
    except ClaimValidationError as e:
        raise HTTPException(status_code=400, detail={"validation_errors": e.errors}) from e

    service = _llm_service_or_502()
    try:
        if sheet.member.declared_chronic_conditions:
            claims = enrich_preexisting_links(claims, sheet.member, service)
        claims = enrich_category_flags(claims, policy, service)
    except HTTPException:
        raise
    except Exception as e:
        raise _llm_error(e) from e

    return {
        "member": sheet.member.model_dump(mode="json"),
        "rows": sheet.rows,
        "claims": [c.model_dump(mode="json") for c in claims],
    }


@router.post("/claims/run")
async def claims_run(
    file: UploadFile = File(...),
    policy: PolicyConfig = Depends(get_policy),
) -> dict[str, Any]:
    """Combined endpoint: upload a claim PDF, extract → validate → enrich → adjudicate →
    compute Q1-Q6 → persist the bundle to disk → return everything.

    Two reasons this exists over the two-step (`/claims/extract` + `/questions/answer`):
    1. The two-step flow runs LLM enrichment **twice** (once in extract, again in the
       adjudicate path) — this endpoint runs it once. Halves the Anthropic spend per upload.
    2. The bundle is persisted to `data/claims/_last_run.json`, so refreshing the UI
       (or restarting the backend) doesn't re-trigger LLM calls — the saved result is served.
    """

    from adjudication.services.questions_service import QuestionsService

    filename = (file.filename or "").lower()
    if not filename.endswith(".pdf"):
        raise HTTPException(status_code=400, detail="unsupported file type; only .pdf claim sheets are accepted")
    content = await file.read()
    sheet = PDFClaimExtractor().extract_from_bytes(content)

    try:
        claims = validate_claim_rows(sheet.rows, policy)
    except ClaimValidationError as e:
        raise HTTPException(status_code=400, detail={"validation_errors": e.errors}) from e

    service = _llm_service_or_502()
    try:
        if sheet.member.declared_chronic_conditions:
            claims = enrich_preexisting_links(claims, sheet.member, service)
        claims = enrich_category_flags(claims, policy, service)
    except HTTPException:
        raise
    except Exception as e:
        raise _llm_error(e) from e

    report = adjudicate(claims, policy, member=sheet.member)
    answers = QuestionsService.answer_all(policy, report)

    # Collect cited KB rows so the UI doesn't need extra round-trips.
    cited_ids: set[str] = set()
    for c in claims:
        if c.pre_existing_link:
            cited_ids.update(c.pre_existing_link.evidence_ids)
        cited_ids.update(c.category_flags_evidence_ids)
    kb_index: dict[str, dict] = {}
    for cid in sorted(cited_ids):
        row = service.find_kb_row(cid)
        if row is not None:
            kb_index[cid] = row

    bundle = {
        "filename": file.filename or "",
        "member": sheet.member.model_dump(mode="json"),
        "rows": sheet.rows,
        "claims": [c.model_dump(mode="json") for c in claims],
        "settlement": report.model_dump(mode="json"),
        "answers": answers,
        "kb_index": kb_index,
    }
    _atomic_write_json(get_current_claim_path(), bundle)
    return bundle


@router.get("/claims/current")
def get_current_claim() -> dict[str, Any]:
    """Return the most recent adjudication bundle persisted by `/claims/run`. 404 if none."""

    path = get_current_claim_path()
    if not path.exists():
        raise HTTPException(status_code=404, detail={"error": "no_current_claim"})
    return json.loads(path.read_text(encoding="utf-8"))


@router.delete("/claims/current")
def delete_current_claim() -> dict[str, Any]:
    """Clear the saved adjudication bundle. The UI re-shows the upload widget."""

    path = get_current_claim_path()
    existed = path.exists()
    if existed:
        path.unlink()
    return {"removed": existed}


def _claims_from_payload(payload: dict[str, Any], policy: PolicyConfig):
    """Resolve `(claims, member)` for an adjudication endpoint:
    body-supplied (with optional member context) → enriched typed claims, OR default bundled."""

    if not payload.get("claims"):
        return get_default_claims_with_member()
    try:
        claims = validate_claim_rows(payload["claims"], policy)
    except ClaimValidationError as e:
        raise HTTPException(status_code=400, detail={"validation_errors": e.errors}) from e
    service = _llm_service_or_502()
    from adjudication.models.member import MemberContext

    member = (
        MemberContext.model_validate(payload.get("member") or {})
        if payload.get("member")
        else MemberContext()
    )
    try:
        if member.declared_chronic_conditions:
            claims = enrich_preexisting_links(claims, member, service)
        claims = enrich_category_flags(claims, policy, service)
    except HTTPException:
        raise
    except Exception as e:
        raise _llm_error(e) from e
    return claims, member


@router.post("/adjudicate", response_model=SettlementReport)
def adjudicate_endpoint(
    payload: dict[str, Any] = Body(default_factory=dict),
    policy: PolicyConfig = Depends(get_policy),
) -> SettlementReport:
    """If `claims` is provided in the body use those; otherwise default to the bundled claims."""

    claims, member = _claims_from_payload(payload, policy)
    return adjudicate(claims, policy, member=member)


@router.post("/questions/answer")
def questions_answer(
    payload: dict[str, Any] = Body(default_factory=dict),
    policy: PolicyConfig = Depends(get_policy),
) -> dict[str, Any]:
    """Adjudicate + emit Q1–Q6 audit cards in a single round-trip.

    Body shape (same as /adjudicate): `{"claims": [...], "member": {...}}` — either field may be omitted.
    Returns `{"settlement": <SettlementReport>, "answers": [<QuestionCard>, ...]}` so the UI can
    render both panels off a single fetch.
    """

    from adjudication.services.questions_service import QuestionsService

    claims, member = _claims_from_payload(payload, policy)
    report = adjudicate(claims, policy, member=member)

    # Build a lookup of every KB row a classifier cited on these claims, so the UI
    # can render evidence ids as clickable popovers without extra round-trips.
    service = get_llm_service()
    cited_ids: set[str] = set()
    for c in claims:
        if c.pre_existing_link:
            cited_ids.update(c.pre_existing_link.evidence_ids)
        cited_ids.update(c.category_flags_evidence_ids)
    kb_index: dict[str, dict] = {}
    for cid in sorted(cited_ids):
        row = service.find_kb_row(cid)
        if row is not None:
            kb_index[cid] = row

    return {
        "settlement": report.model_dump(mode="json"),
        "answers": QuestionsService.answer_all(policy, report),
        "kb_index": kb_index,
    }


@router.get("/report/table")
def report_table(policy: PolicyConfig = Depends(get_policy)) -> dict[str, str]:
    claims = get_default_claims()
    report = adjudicate(claims, policy)
    return {"table": report_to_table(report)}


@router.get("/report/json")
def report_json_endpoint(policy: PolicyConfig = Depends(get_policy)) -> dict[str, Any]:
    claims = get_default_claims()
    report = adjudicate(claims, policy)
    return json.loads(report_to_json(report))


# -------- Q1..Q6 convenience endpoints --------

def _resolve_physio(policy: PolicyConfig) -> dict[str, Any]:
    base = policy.benefit("physiotherapy")
    effective = base.model_dump()
    applied: list[dict[str, Any]] = []
    for e in policy.endorsements_for("physiotherapy"):
        for k, v in e.overrides.items():
            applied.append({"field": k, "from": effective.get(k), "to": v, "source": e.source})
            effective[k] = v
    return {"base": base.model_dump(), "effective": effective, "endorsements_applied": applied}


@router.get("/questions/q1")
def q1(policy: PolicyConfig = Depends(get_policy)) -> dict[str, Any]:
    physio = _resolve_physio(policy)
    return {
        "question": "Member coinsurance % and annual sub-limit for Physiotherapy.",
        "answer": {
            "in_network_coinsurance": physio["effective"]["in_network_coinsurance"],
            "annual_sub_limit": physio["effective"]["annual_sub_limit"],
        },
        "derivation": {
            "base_from_Section_2": {
                "in_network_coinsurance": physio["base"]["in_network_coinsurance"],
                "annual_sub_limit": physio["base"]["annual_sub_limit"],
            },
            "overrides_from_Section_5_E1": physio["endorsements_applied"],
        },
    }


@router.get("/questions/q2")
def q2(policy: PolicyConfig = Depends(get_policy)) -> dict[str, Any]:
    return {
        "question": "Annual Aggregate Limit.",
        "answer": {"aggregate_annual_limit": policy.aggregate_annual_limit, "currency": "AED"},
        "source": "Section 2 — Table of Benefits header.",
    }


@router.get("/questions/q3")
def q3(policy: PolicyConfig = Depends(get_policy)) -> dict[str, Any]:
    claims = get_default_claims()
    report = adjudicate(claims, policy)
    s = next((s for s in report.settlements if s.claim_id == "C1"), None)
    if s is None:
        raise HTTPException(status_code=404, detail="C1 not found in default claims")
    return {
        "question": "C1 insurer payment and member out-of-pocket.",
        "answer": {"insurer_paid": s.insurer_paid, "member_paid": s.member_paid},
        "derivation": [step.model_dump() for step in s.reasoning],
        "decision": s.decision.value,
        "reason": s.reason,
    }


@router.get("/questions/q4")
def q4(policy: PolicyConfig = Depends(get_policy)) -> dict[str, Any]:
    claims = get_default_claims()
    report = adjudicate(claims, policy)
    # A claim is "not payable in full or in part" iff decision != PAYABLE
    # (i.e. EXCLUDED or PAYABLE_WITH_PENALTY).
    not_full = [
        {
            "claim_id": s.claim_id,
            "decision": s.decision.value,
            "reason": s.reason,
            "insurer_paid": s.insurer_paid,
            "member_paid": s.member_paid,
            "billed": s.billed_amount,
        }
        for s in report.settlements
        if s.decision.value != "payable"
    ]
    return {
        "question": "Claims not payable in full or in part, with clause.",
        "answer": not_full,
    }


@router.get("/questions/q5")
def q5(policy: PolicyConfig = Depends(get_policy)) -> dict[str, Any]:
    claims = get_default_claims()
    report = adjudicate(claims, policy)
    return {
        "question": "Total insurer-paid and total member out-of-pocket across all claims.",
        "answer": {
            "insurer_total": report.insurer_total,
            "member_total": report.member_total,
            "currency": "AED",
        },
        "breakdown": [
            {
                "claim_id": s.claim_id,
                "insurer_paid": s.insurer_paid,
                "member_paid": s.member_paid,
                "decision": s.decision.value,
            }
            for s in report.settlements
        ],
    }


@router.get("/questions/q6")
def q6(policy: PolicyConfig = Depends(get_policy)) -> dict[str, Any]:
    claims = get_default_claims()
    report = adjudicate(claims, policy)
    return {
        "question": "Structured settlement statement (JSON + human-readable table).",
        "json": json.loads(report_to_json(report)),
        "table": report_to_table(report),
    }
