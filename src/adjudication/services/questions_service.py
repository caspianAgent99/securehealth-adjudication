"""QuestionsService — compute Q1–Q6 audit cards from (PolicyConfig, SettlementReport).

Each card is a tiny dict with `id, task, title, headline, answer, derivation, sources`.
Pure function; no I/O, no LLM. Drives the locked-state "Q1–Q6 audit" panel in the UI.
"""

from __future__ import annotations

from typing import Any

from ..models.policy import PolicyConfig
from ..models.settlement import ClaimSettlement, Decision, SettlementReport


def _aed(v: float | None) -> str:
    if v is None:
        return "—"
    return f"AED {float(v):,.2f}"


def _pct(v: float | None) -> str:
    if v is None:
        return "—"
    return f"{float(v) * 100:.0f}%"


def _physio_effective(policy: PolicyConfig) -> tuple[dict[str, Any], dict[str, Any], list[dict[str, Any]]]:
    base = policy.benefit("physiotherapy")
    base_view = {
        "in_network_coinsurance": base.in_network_coinsurance,
        "annual_sub_limit": base.annual_sub_limit,
    }
    effective: dict[str, Any] = dict(base_view)
    overrides_applied: list[dict[str, Any]] = []
    for e in policy.endorsements_for("physiotherapy"):
        for k, v in e.overrides.items():
            overrides_applied.append(
                {
                    "field": k,
                    "from": effective.get(k),
                    "to": v,
                    "source": e.source,
                    "endorsement_id": e.id,
                }
            )
            effective[k] = v
    return base_view, effective, overrides_applied


# ---------- per-question builders ----------

def _q1(policy: PolicyConfig) -> dict[str, Any]:
    base, effective, overrides = _physio_effective(policy)
    derivation: list[dict[str, Any]] = [
        {
            "label": "Base — Section 2 Table of Benefits",
            "value": f"{_pct(base['in_network_coinsurance'])} IN coinsurance · {_aed(base['annual_sub_limit'])} sub-limit",
            "source": "§2",
        }
    ]
    for o in overrides:
        derivation.append(
            {
                "label": f"Endorsement {o['endorsement_id']} override on `{o['field']}`",
                "value": f"{o['from']} → {o['to']}",
                "source": o["source"],
            }
        )
    derivation.append(
        {
            "label": "Effective for adjudication",
            "value": f"{_pct(effective['in_network_coinsurance'])} IN coinsurance · {_aed(effective['annual_sub_limit'])} sub-limit",
            "source": "engine (overrides applied)",
        }
    )
    return {
        "id": "q1",
        "task": "Extraction",
        "title": "Physiotherapy coinsurance and annual sub-limit",
        "headline": f"{_pct(effective['in_network_coinsurance'])} IN coinsurance · {_aed(effective['annual_sub_limit'])} sub-limit",
        "answer": {
            "in_network_coinsurance": effective["in_network_coinsurance"],
            "annual_sub_limit": effective["annual_sub_limit"],
        },
        "derivation": derivation,
        "sources": ["§2 Table of Benefits"] + [o["source"] for o in overrides],
    }


def _q2(policy: PolicyConfig) -> dict[str, Any]:
    return {
        "id": "q2",
        "task": "Extraction",
        "title": "Annual Aggregate Limit",
        "headline": _aed(policy.aggregate_annual_limit),
        "answer": {"aggregate_annual_limit": policy.aggregate_annual_limit, "currency": "AED"},
        "derivation": [
            {
                "label": "Read from policy header",
                "value": _aed(policy.aggregate_annual_limit),
                "source": "§2 Table of Benefits (Plan B) header",
            }
        ],
        "sources": ["§2 Table of Benefits (Plan B) header"],
    }


def _settlement_steps_to_derivation(s: ClaimSettlement) -> list[dict[str, Any]]:
    """Project the engine's per-claim reasoning chain into the card's derivation shape."""

    out: list[dict[str, Any]] = []
    for step in s.reasoning:
        out.append(
            {
                "label": step.label,
                "value": step.value,
                "source": step.source,
                "note": step.note or "",
            }
        )
    out.append(
        {
            "label": "Final",
            "value": f"insurer={_aed(s.insurer_paid)} · member={_aed(s.member_paid)}",
            "source": "engine",
        }
    )
    return out


def _pick_q3_target(report: SettlementReport) -> ClaimSettlement | None:
    """Use the claim explicitly named in the brief (C1) when present; else the first by date+id."""

    by_id = {s.claim_id.upper(): s for s in report.settlements}
    if "C1" in by_id:
        return by_id["C1"]
    if report.settlements:
        return sorted(report.settlements, key=lambda s: (s.service_date, s.claim_id))[0]
    return None


def _q3(report: SettlementReport) -> dict[str, Any]:
    s = _pick_q3_target(report)
    if s is None:
        return {
            "id": "q3",
            "task": "Single rule",
            "title": "C1 insurer payment and member out-of-pocket",
            "headline": "No claims to compute against.",
            "answer": None,
            "derivation": [],
            "sources": [],
        }
    return {
        "id": "q3",
        "task": "Single rule",
        "title": f"{s.claim_id} insurer payment and member out-of-pocket",
        "headline": f"Insurer pays {_aed(s.insurer_paid)} · Member pays {_aed(s.member_paid)}",
        "answer": {
            "claim_id": s.claim_id,
            "billed_amount": s.billed_amount,
            "eligible_amount": s.eligible_amount,
            "deductible_applied": s.deductible_applied,
            "coinsurance_member": s.coinsurance_member,
            "penalty_amount": s.penalty_amount,
            "insurer_paid": s.insurer_paid,
            "member_paid": s.member_paid,
            "decision": s.decision.value,
        },
        "derivation": _settlement_steps_to_derivation(s),
        "sources": ["GC-1 (calculation order)", f"benefit:{s.benefit_key}"],
    }


def _q4(report: SettlementReport) -> dict[str, Any]:
    affected = [s for s in report.settlements if s.decision != Decision.PAYABLE]
    rows = [
        {
            "claim_id": s.claim_id,
            "decision": s.decision.value,
            "billed_amount": s.billed_amount,
            "insurer_paid": s.insurer_paid,
            "member_paid": s.member_paid,
            "reason": s.reason,
            "requires_review": s.requires_review,
        }
        for s in affected
    ]
    headline = f"{len(rows)} claim(s) not payable in full" if rows else "All claims payable in full."
    return {
        "id": "q4",
        "task": "Exclusions",
        "title": "Claims not payable in full or in part",
        "headline": headline,
        "answer": rows,
        "derivation": [
            {"label": r["claim_id"], "value": f"{r['decision']} — {r['reason']}", "source": "engine.gate"}
            for r in rows
        ],
        "sources": list(dict.fromkeys(r["reason"] for r in rows)),
    }


def _q5(report: SettlementReport) -> dict[str, Any]:
    breakdown = [
        {
            "claim_id": s.claim_id,
            "decision": s.decision.value,
            "insurer_paid": s.insurer_paid,
            "member_paid": s.member_paid,
            "billed_amount": s.billed_amount,
        }
        for s in report.settlements
    ]
    return {
        "id": "q5",
        "task": "Full calc",
        "title": "Year totals (insurer / member)",
        "headline": f"Insurer {_aed(report.insurer_total)} · Member {_aed(report.member_total)}",
        "answer": {
            "insurer_total": report.insurer_total,
            "member_total": report.member_total,
            "currency": "AED",
            "claim_count": len(report.settlements),
        },
        "derivation": [
            {
                "label": b["claim_id"],
                "value": f"insurer={_aed(b['insurer_paid'])} · member={_aed(b['member_paid'])}  ({b['decision']})",
                "source": "engine.adjudicate",
            }
            for b in breakdown
        ]
        + [
            {
                "label": "TOTAL",
                "value": f"insurer={_aed(report.insurer_total)} · member={_aed(report.member_total)}",
                "source": "engine.adjudicate (sum)",
            }
        ],
        "sources": ["engine.adjudicate"],
    }


def _q6(report: SettlementReport) -> dict[str, Any]:
    rows = []
    for s in report.settlements:
        rows.append(
            {
                "claim_id": s.claim_id,
                "service_date": s.service_date.isoformat(),
                "benefit_key": s.benefit_key,
                "billed_amount": s.billed_amount,
                "eligible_amount": s.eligible_amount,
                "deductible_applied": s.deductible_applied,
                "coinsurance_member": s.coinsurance_member,
                "coinsurance_insurer": s.coinsurance_insurer,
                "penalty_amount": s.penalty_amount,
                "insurer_paid": s.insurer_paid,
                "member_paid": s.member_paid,
                "decision": s.decision.value,
                "reason": s.reason,
                "requires_review": s.requires_review,
            }
        )
    return {
        "id": "q6",
        "task": "Generation",
        "title": "Structured settlement statement",
        "headline": f"{len(rows)} claim row(s) · year totals attached",
        "answer": {
            "rows": rows,
            "totals": {
                "insurer_total": report.insurer_total,
                "member_total": report.member_total,
                "aggregate_limit": report.aggregate_limit,
                "aggregate_remaining": report.aggregate_remaining,
            },
        },
        "derivation": [],
        "sources": ["engine.adjudicate", "reporting/json_report.py", "reporting/table_report.py"],
    }


# ---------- service ----------

class QuestionsService:
    """Pure projection over (PolicyConfig, SettlementReport) → 6 audit cards.

    Stateless. All inputs are typed Pydantic models, all outputs are JSON-serialisable dicts.
    """

    @staticmethod
    def answer_all(policy: PolicyConfig, report: SettlementReport) -> list[dict[str, Any]]:
        return [
            _q1(policy),
            _q2(policy),
            _q3(report),
            _q4(report),
            _q5(report),
            _q6(report),
        ]
