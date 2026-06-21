"""Claude provider — the only LLM transport in production.

Transport only: returns raw dicts to `LLMService`. The service handles parsing,
KB assembly, and confidence normalisation. Misconfiguration (missing SDK or API
key) fails loudly rather than silently degrading.
"""

from __future__ import annotations

import json
from typing import Any

from ..config import SETTINGS
from .types import PolicyProposal

POLICY_SYSTEM_PROMPT = """You are an insurance policy extraction engine.
Read one health-insurance policy wording and return ONLY a JSON object that matches
the schema below. You do NOT compute claim outcomes; you only structure rules.

Top-level shape:
{
  "policy_ref": str,
  "plan_year_start": "YYYY-MM-DD",
  "plan_year_end": "YYYY-MM-DD",
  "policy_start_date": "YYYY-MM-DD",
  "aggregate_annual_limit": number,
  "approved_by": "llm-proposal",
  "approved_at": "YYYY-MM-DD",
  "calculation_order": ["cap_to_eligible","apply_deductible","apply_coinsurance"],
  "benefits": [
    {"key": str (snake_case),
     "name": str,
     "annual_sub_limit": number | null,
     "in_network_coinsurance": number (0..1),
     "out_of_network_coinsurance": number (0..1) | null,
     "deductible": number,
     "requires_preauth": bool,
     "notes": str}
  ],
  "endorsements": [
    {"id": str,
     "benefit_key": str,
     "overrides": object (subset of benefit fields with their new values),
     "source": str,
     "effective_from": "YYYY-MM-DD"|null,
     "effective_to": "YYYY-MM-DD"|null}
  ],
  "exclusion_rules": [ … see below … ],
  "metadata": {"source_doc": str, "doc_ref": str, "currency": "AED"}
}

EXCLUSION RULES — each object MUST include id, type, params, applies_to_benefits, reason_template.
params MUST use the exact keys below; do not invent synonyms.

Type "waiting_period":
  {
    "id": "WP-PREEX-6MO" or similar,
    "type": "waiting_period",
    "params": { "condition_flag": "pre_existing", "waiting_months": int },
    "applies_to_benefits": null,
    "reason_template": str describing the exclusion clause
  }

Type "preauth_penalty":
  {
    "id": "PREAUTH-PENALTY-20" or similar,
    "type": "preauth_penalty",
    "params": { "penalty_pct": number (0..1) },
    "applies_to_benefits": ["inpatient_surgery"],
    "reason_template": str
  }

Type "not_covered_oon":
  {
    "id": "OON-PHARMACY-NOT-COVERED" or similar,
    "type": "not_covered_oon",
    "params": {},
    "applies_to_benefits": ["pharmacy"],
    "reason_template": str
  }

Type "not_covered_condition":
  {
    "id": "EX-4.1-COSMETIC" or similar,
    "type": "not_covered_condition",
    "params": { "condition_flag": "<snake_case_category>" },
    "applies_to_benefits": null,
    "reason_template": str citing the clause
  }
  USE THIS TYPE for §4.1 general exclusions: cosmetic treatment, self-inflicted
  injury, experimental / unproven treatment. One rule per category. Canonical flags:
    - cosmetic
    - self_inflicted
    - experimental
  Do NOT emit rules for "outside the Policy Year" or "charges above R&C" (handled mechanically).

General rules:
- "Not covered" for OON => the benefit's out_of_network_coinsurance MUST be null (not 0).
- Benefit `key` must be snake_case and stable.
- Output ONLY the JSON. No prose, no markdown fences.
"""

PREEX_SYSTEM_PROMPT_TEMPLATE = """You decide whether a single claim diagnosis is linked to a declared chronic/pre-existing condition.

You are given a CLINICAL KNOWLEDGE BASE block of allowed associations for the declared condition.
Cite the row ids you used as evidence.

CLINICAL KNOWLEDGE BASE (id  chronic — indicator (relation)):
{kb_block}

You will receive JSON: {{"diagnosis": str, "declared_condition": str}}.
Return ONLY this JSON shape:
{{
  "is_related": bool,
  "confidence": "high" | "low",
  "evidence_ids": [str, ...],
  "reasoning": str
}}

Rules for `confidence` (compute deterministically — only two levels are allowed):
- "high" = STRONG evidence in either direction. Use this when AT LEAST ONE of these holds:
   (a) a KB row directly matches the diagnosis (cite ids in evidence_ids), OR
   (b) the source text contains an explicit negation like "unrelated to <condition>", OR
   (c) the diagnosis is clearly outside the medical area of the declared condition
       (e.g. "acute viral influenza" vs declared "asthma" — distinct disease families).
- "low"  = anything else. Use this whenever you must rely on parametric medical knowledge
           alone (no KB match AND no explicit negation AND not clearly out-of-area), OR
           when the diagnosis is genuinely ambiguous. `low` always flags for human review.

Be conservative. Output ONLY the JSON, no prose, no markdown fences.
"""

CATEGORY_SYSTEM_PROMPT_TEMPLATE = """You classify a single claim diagnosis against a fixed list of policy exclusion categories.

You are given an EXCLUSION-CATEGORY KNOWLEDGE BASE block. Use it as your primary evidence
and cite KB row ids when a flag is supported by a direct match.

EXCLUSION KB (id  category — indicator (relation)):
{kb_block}

Input JSON: {{"diagnosis": str, "categories": [str, ...]}}.
Return ONLY this JSON shape:
{{
  "flags": [str, ...],
  "evidence_ids": [str, ...],
  "confidence": "high" | "low",
  "reasoning": str
}}

Rules for `flags`:
- `flags` MUST be a subset of `categories`.
- For every flag you set, include in `evidence_ids` the ids of the KB rows that supported it.
  A flag MAY be set without a KB match if established medical knowledge strongly implies the
  category, but be conservative — return [] when unsure.
- Treat negations ("non-cosmetic", "not experimental", "no experimental") as suppressing the match.

Rules for `confidence` (compute deterministically — only two levels are allowed):
- "high" = STRONG evidence in either direction. Use this when EVERY decision is backed by AT LEAST ONE of:
   (a) a KB row directly matches the diagnosis for one of the categories (cite ids), OR
   (b) the diagnosis text contains an explicit textual signal (e.g. "elective cosmetic"), OR
   (c) the diagnosis is clearly outside the medical area of every requested category
       (e.g. "acute sinusitis" vs cosmetic / self-inflicted / experimental — clearly an
       infectious-disease diagnosis, not in scope for any §4.1 category).
- "low"  = anything else. Use this when AT LEAST ONE decision relies on parametric reasoning
           with no KB match AND no explicit textual signal AND not clearly out-of-area, OR
           when the diagnosis is genuinely ambiguous. `low` always flags for human review.

Be conservative. Output ONLY the JSON. No prose, no markdown fences.
"""


class AnthropicProviderUnavailable(RuntimeError):
    """Raised at construction time when the Anthropic SDK or API key is missing."""


class AnthropicProvider:
    name = "anthropic"

    def __init__(self) -> None:
        if not SETTINGS.anthropic_api_key:
            raise AnthropicProviderUnavailable(
                "ANTHROPIC_API_KEY is not set. Add it to .env or the environment."
            )
        try:
            import anthropic
        except ImportError as exc:
            raise AnthropicProviderUnavailable(
                "The `anthropic` SDK is not installed in this environment."
            ) from exc
        self._client = anthropic.Anthropic(api_key=SETTINGS.anthropic_api_key)
        self._model = SETTINGS.anthropic_model

    def _complete_json(self, system: str, user: str) -> dict[str, Any]:
        msg = self._client.messages.create(
            model=self._model,
            max_tokens=4096,
            system=system,
            messages=[{"role": "user", "content": user}],
        )
        text = "".join(b.text for b in msg.content if getattr(b, "type", "") == "text").strip()
        if text.startswith("```"):
            text = text.split("\n", 1)[1] if "\n" in text else text
            if text.endswith("```"):
                text = text[: -3]
        return json.loads(text)

    def propose_policy(self, policy_text: str) -> PolicyProposal:
        config = self._complete_json(POLICY_SYSTEM_PROMPT, policy_text)
        citations = config.pop("__citations__", {}) if isinstance(config, dict) else {}
        return PolicyProposal(config=config, citations=citations)

    def classify_preexisting_link(
        self, diagnosis: str, declared_condition: str, kb_block: str
    ) -> dict[str, Any]:
        system = PREEX_SYSTEM_PROMPT_TEMPLATE.format(kb_block=kb_block)
        user = json.dumps({"diagnosis": diagnosis, "declared_condition": declared_condition})
        return self._complete_json(system, user)

    def classify_claim_categories(
        self, diagnosis: str, categories: list[str], kb_block: str
    ) -> dict[str, Any]:
        if not categories:
            return {"flags": [], "evidence_ids": [], "reasoning": "No categories requested."}
        system = CATEGORY_SYSTEM_PROMPT_TEMPLATE.format(kb_block=kb_block)
        user = json.dumps({"diagnosis": diagnosis, "categories": categories})
        return self._complete_json(system, user)
