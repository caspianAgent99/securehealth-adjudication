"""Test-only fakes — kept out of `src/` because production code is Anthropic-only.

`FakeAnthropicProvider` satisfies the `LLMProvider` protocol with deterministic
keyword-matching logic so the test suite stays offline. The canonical Plan B
proposal is kept here as test data only (no production code reads it).
"""

from __future__ import annotations

import copy
import re
from typing import Any

from adjudication.llm.types import PolicyProposal


PLAN_B_FIXTURE: dict[str, Any] = {
    "policy_ref": "Gulf Falcon Insurance — SecureHealth Plan B (GF-SH-B/2025)",
    "plan_year_start": "2025-01-01",
    "plan_year_end": "2025-12-31",
    "policy_start_date": "2025-01-01",
    "aggregate_annual_limit": 250000.0,
    "approved_by": "test-fixture",
    "approved_at": "2025-01-01",
    "calculation_order": ["cap_to_eligible", "apply_deductible", "apply_coinsurance"],
    "benefits": [
        {
            "key": "outpatient_consultation",
            "name": "Outpatient Consultation",
            "annual_sub_limit": 8000.0,
            "in_network_coinsurance": 0.10,
            "out_of_network_coinsurance": 0.30,
            "deductible": 50.0,
            "requires_preauth": False,
            "notes": "AED 50 deductible per visit (GC-4).",
        },
        {
            "key": "diagnostics",
            "name": "Diagnostics (lab & imaging)",
            "annual_sub_limit": 10000.0,
            "in_network_coinsurance": 0.10,
            "out_of_network_coinsurance": 0.30,
            "deductible": 0.0,
            "requires_preauth": False,
            "notes": "No deductible.",
        },
        {
            "key": "pharmacy",
            "name": "Prescribed Medication (Pharmacy)",
            "annual_sub_limit": 6000.0,
            "in_network_coinsurance": 0.20,
            "out_of_network_coinsurance": None,
            "deductible": 0.0,
            "requires_preauth": False,
            "notes": "Out-of-network pharmacy is NOT COVERED.",
        },
        {
            "key": "physiotherapy",
            "name": "Physiotherapy",
            "annual_sub_limit": 2500.0,
            "in_network_coinsurance": 0.20,
            "out_of_network_coinsurance": 0.30,
            "deductible": 0.0,
            "requires_preauth": False,
            "notes": "Base; overridden by Endorsement E1.",
        },
        {
            "key": "inpatient_surgery",
            "name": "Inpatient & Surgery",
            "annual_sub_limit": None,
            "in_network_coinsurance": 0.0,
            "out_of_network_coinsurance": 0.20,
            "deductible": 0.0,
            "requires_preauth": True,
            "notes": "Within aggregate; pre-auth required (GC-3).",
        },
    ],
    "endorsements": [
        {
            "id": "E1",
            "benefit_key": "physiotherapy",
            "overrides": {"in_network_coinsurance": 0.10, "annual_sub_limit": 4000.0},
            "source": "Section 5 — Endorsement E1",
            "effective_from": "2025-01-01",
            "effective_to": "2025-12-31",
        }
    ],
    "exclusion_rules": [
        {
            "id": "WP-PREEX-6MO",
            "type": "waiting_period",
            "params": {"condition_flag": "pre_existing", "waiting_months": 6},
            "applies_to_benefits": None,
            "reason_template": (
                "Excluded under Section 4.2: chronic/pre-existing condition within "
                "the {waiting_months}-month waiting period from the Inception Date."
            ),
        },
        {
            "id": "PREAUTH-PENALTY-20",
            "type": "preauth_penalty",
            "params": {"penalty_pct": 0.20},
            "applies_to_benefits": ["inpatient_surgery"],
            "reason_template": (
                "GC-3: Pre-authorisation not obtained for elective Inpatient & Surgery; "
                "insurer reduces amount payable by {penalty_pct_display}."
            ),
        },
        {
            "id": "OON-PHARMACY-NOT-COVERED",
            "type": "not_covered_oon",
            "params": {},
            "applies_to_benefits": ["pharmacy"],
            "reason_template": "Section 2: Out-of-network Prescribed Medication (Pharmacy) is not covered.",
        },
        {
            "id": "EX-4.1-COSMETIC",
            "type": "not_covered_condition",
            "params": {"condition_flag": "cosmetic"},
            "applies_to_benefits": None,
            "reason_template": "Section 4.1: cosmetic treatment is not payable.",
        },
        {
            "id": "EX-4.1-SELF-INFLICTED",
            "type": "not_covered_condition",
            "params": {"condition_flag": "self_inflicted"},
            "applies_to_benefits": None,
            "reason_template": "Section 4.1: self-inflicted injury is not payable.",
        },
        {
            "id": "EX-4.1-EXPERIMENTAL",
            "type": "not_covered_condition",
            "params": {"condition_flag": "experimental"},
            "applies_to_benefits": None,
            "reason_template": "Section 4.1: experimental or unproven treatment is not payable.",
        },
    ],
    "metadata": {"source_doc": "test-fixture", "doc_ref": "GF-SH-B/2025", "currency": "AED"},
}


_CATEGORY_KEYWORDS: dict[str, tuple[str, ...]] = {
    "cosmetic": ("cosmetic", "rhinoplasty", "liposuction", "botox", "elective cosmetic"),
    "self_inflicted": ("self-inflicted", "self inflicted", "intentional self-harm", "self-harm"),
    "experimental": ("experimental", "unproven", "investigational", "off-label trial"),
}


def _parse_kb_block(kb_block: str) -> list[tuple[str, str]]:
    rows: list[tuple[str, str]] = []
    for line in kb_block.splitlines():
        m = re.match(r"^\s*(\S+)\s+\S+.*?—\s*(.+?)\s*\(", line)
        if m:
            rows.append((m.group(1), m.group(2).strip().lower()))
    return rows


def _negation_hits(diagnosis: str, condition: str) -> list[str]:
    patterns = (
        f"unrelated to {condition}",
        f"not related to {condition}",
        f"no relation to {condition}",
        f"not linked to {condition}",
        "unrelated to the declared",
        "not related to the declared",
    )
    return [p for p in patterns if p in diagnosis]


class FakeAnthropicProvider:
    """Satisfies LLMProvider for tests; never touches the network."""

    name = "fake-anthropic"

    def propose_policy(self, policy_text: str) -> PolicyProposal:
        return PolicyProposal(config=copy.deepcopy(PLAN_B_FIXTURE), citations={})

    def classify_preexisting_link(
        self, diagnosis: str, declared_condition: str, kb_block: str
    ) -> dict[str, Any]:
        dx = (diagnosis or "").strip().lower()
        cond = (declared_condition or "").strip().lower()
        if not dx or not cond:
            return {"is_related": False, "confidence": "low", "evidence_ids": [],
                    "reasoning": "No diagnosis or declared condition provided."}
        negs = _negation_hits(dx, cond)
        if negs:
            return {"is_related": False, "confidence": "high", "evidence_ids": [],
                    "reasoning": f"Explicit negation: {negs[0]!r}."}
        hits = [kb_id for kb_id, indicator in _parse_kb_block(kb_block) if indicator and indicator in dx]
        if hits:
            return {"is_related": True, "confidence": "high", "evidence_ids": hits,
                    "reasoning": f"Matched KB entries {hits} for '{cond}'."}
        if cond in dx:
            return {"is_related": True, "confidence": "low", "evidence_ids": [],
                    "reasoning": f"Diagnosis mentions '{cond}' but no KB indicator matched."}
        return {"is_related": False, "confidence": "low", "evidence_ids": [],
                "reasoning": f"No overlap with '{cond}'."}

    def classify_claim_categories(
        self, diagnosis: str, categories: list[str], kb_block: str
    ) -> dict[str, Any]:
        dx = (diagnosis or "").strip().lower()
        if not dx:
            return {
                "flags": [], "evidence_ids": [], "confidence": "low",
                "reasoning": "No diagnosis text provided.",
            }
        flags: list[str] = []
        evidence_ids: list[str] = []
        notes: list[str] = []
        keyword_only_flag = False     # at least one flag fired without KB evidence
        ambiguity_seen = False        # set when the diagnosis carries an ambiguity marker

        if any(marker in dx for marker in ("etiology unclear", "uncertain", "could be", "may be")):
            ambiguity_seen = True

        for line in kb_block.splitlines():
            m = re.match(r"^\s*(\S+)\s+(\S+)\s+—\s+(.+?)\s+\(", line)
            if not m:
                continue
            kb_id, cat, indicator = m.group(1), m.group(2).strip().lower(), m.group(3).strip().lower()
            if cat not in categories or not indicator:
                continue
            if indicator in dx:
                if cat not in flags:
                    flags.append(cat)
                if kb_id not in evidence_ids:
                    evidence_ids.append(kb_id)
                notes.append(f"'{cat}' matched KB row '{kb_id}' on '{indicator}'.")

        for cat in categories:
            if cat in flags:
                continue
            kws = _CATEGORY_KEYWORDS.get(cat, (cat.replace("_", " "),))
            hit = next((kw for kw in kws if kw in dx), None)
            if not hit:
                continue
            if any(neg in dx for neg in (f"non-{hit}", f"non {hit}", f"no {hit}", f"not {hit}")):
                notes.append(f"'{cat}' suppressed by negation around '{hit}'.")
                continue
            flags.append(cat)
            keyword_only_flag = True
            notes.append(f"'{cat}' matched on '{hit}' (no KB row).")

        # Two-level confidence: "high" only when every flag was backed by a KB hit (or no
        # category fires at all on a clearly-unrelated diagnosis); otherwise "low".
        if ambiguity_seen or keyword_only_flag:
            confidence = "low"
        else:
            confidence = "high"
        return {
            "flags": flags,
            "evidence_ids": evidence_ids,
            "confidence": confidence,
            "reasoning": " ; ".join(notes) if notes else "No categories matched.",
        }
