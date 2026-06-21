"""LLMService — the single entry point for every LLM-touching capability in the app.

All callers (API routes, enrichment, CLI) depend on this class and never on a raw
`LLMProvider`. Providers are transports; this service owns prompt assembly, the
clinical knowledge base, response parsing, and derived signals like `requires_review`.
"""

from __future__ import annotations

from pathlib import Path

from ..config import SETTINGS
from ..llm.provider import LLMProvider
from ..llm.types import (
    ClaimCategoryClassification,
    PolicyProposal,
    PreExistingClassification,
)
from .clinical_kb import ClinicalKB, ExclusionCategoryKB, KBRow


def _format_kb_block(rows: list[KBRow]) -> str:
    if not rows:
        return "(no KB entries for this scope — rely on established medical knowledge.)"
    return "\n".join(f"{r.id}  {r.group} — {r.indicator}  ({r.relation})" for r in rows)


class LLMService:
    """Facade over an `LLMProvider`. Single point of LLM access for the rest of the codebase."""

    def __init__(
        self,
        provider: LLMProvider | None = None,
        kb: ClinicalKB | None = None,
        exclusion_kb: ExclusionCategoryKB | None = None,
    ):
        if provider is None:
            from ..llm.anthropic_provider import AnthropicProvider

            provider = AnthropicProvider()
        self._provider = provider
        if kb is not None:
            self._kb = kb
        else:
            kb_path = Path(SETTINGS.clinical_kb_path)
            self._kb = ClinicalKB.load(kb_path) if kb_path.exists() else ClinicalKB.empty()
        if exclusion_kb is not None:
            self._exclusion_kb = exclusion_kb
        else:
            ex_path = Path(SETTINGS.exclusion_kb_path)
            self._exclusion_kb = ExclusionCategoryKB.load(ex_path) if ex_path.exists() else ExclusionCategoryKB.empty()

    @property
    def provider_name(self) -> str:
        return getattr(self._provider, "name", "unknown")

    @property
    def kb(self) -> ClinicalKB:
        return self._kb

    @property
    def exclusion_kb(self) -> ExclusionCategoryKB:
        return self._exclusion_kb

    def find_kb_row(self, kb_id: str) -> dict | None:
        """Look up a row by id across both KBs. Returns a plain dict or None."""

        for kb in (self._kb, self._exclusion_kb):
            row = kb.get_row(kb_id)
            if row is not None:
                return {
                    "id": row.id,
                    "group": row.group,
                    "indicator": row.indicator,
                    "relation": row.relation,
                }
        return None

    # ---------- public capabilities ----------

    def propose_policy(self, policy_text: str) -> PolicyProposal:
        return self._provider.propose_policy(policy_text)

    def classify_preexisting_link(
        self,
        diagnosis: str,
        declared_condition: str,
    ) -> PreExistingClassification:
        """Decide whether the claim's diagnosis is related to the declared chronic condition.

        Provider receives the formatted KB block for that condition. Service parses the raw
        response, normalises confidence to one of {"high", "low"} — anything that isn't the
        literal string "high" collapses to "low" — and computes `requires_review`
        = (confidence != "high").
        """

        rows = self._kb.for_condition(declared_condition)
        kb_block = _format_kb_block(rows)
        raw = self._provider.classify_preexisting_link(diagnosis, declared_condition, kb_block)

        is_related = bool(raw.get("is_related", False))
        confidence = str(raw.get("confidence", "low")).strip().lower()
        if confidence != "high":
            confidence = "low"
        evidence_ids = [str(x) for x in raw.get("evidence_ids", []) if str(x).strip()]
        # Only retain evidence IDs that actually exist in the KB — defends against hallucinated ids.
        known_ids = {r.id for r in rows}
        evidence_ids = [eid for eid in evidence_ids if eid in known_ids]
        reasoning = str(raw.get("reasoning", "")).strip()
        return PreExistingClassification(
            is_related=is_related,
            confidence=confidence,
            evidence_ids=evidence_ids,
            reasoning=reasoning,
            requires_review=confidence != "high",
            kb_size=len(rows),
        )

    def classify_claim_categories(
        self,
        diagnosis: str,
        categories: list[str],
    ) -> ClaimCategoryClassification:
        if not categories:
            return ClaimCategoryClassification(
                flags=[],
                evidence_ids=[],
                confidence="high",
                reasoning="No categories requested.",
                requires_review=False,
                kb_size=0,
            )
        rows = self._exclusion_kb.for_categories(categories)
        kb_block = _format_kb_block(rows)
        raw = self._provider.classify_claim_categories(diagnosis, categories, kb_block)

        flags = [f for f in raw.get("flags", []) if f in categories]
        known_ids = {r.id for r in rows}
        evidence_ids = [str(x) for x in raw.get("evidence_ids", []) if str(x) in known_ids]
        confidence = str(raw.get("confidence", "low")).strip().lower()
        if confidence != "high":
            confidence = "low"
        reasoning = str(raw.get("reasoning", "")).strip()
        return ClaimCategoryClassification(
            flags=flags,
            evidence_ids=evidence_ids,
            confidence=confidence,
            reasoning=reasoning,
            requires_review=confidence != "high",
            kb_size=len(rows),
        )
