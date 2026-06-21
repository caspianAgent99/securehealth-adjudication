"""LLMProvider — the transport contract between the service layer and Anthropic.

Providers receive structured inputs and return **raw dicts**. They do NOT parse into
typed dataclasses, do NOT compute derived signals like `requires_review`, do NOT load
the clinical KB. All of that is the `LLMService`'s job (`services/llm_service.py`).
"""

from __future__ import annotations

from typing import Any, Protocol

from .types import PolicyProposal


class LLMProvider(Protocol):
    name: str

    def propose_policy(self, policy_text: str) -> PolicyProposal: ...

    def classify_preexisting_link(
        self,
        diagnosis: str,
        declared_condition: str,
        kb_block: str,
    ) -> dict[str, Any]:
        """Return a raw dict {is_related, confidence, evidence_ids, reasoning}."""

    def classify_claim_categories(
        self,
        diagnosis: str,
        categories: list[str],
        kb_block: str,
    ) -> dict[str, Any]:
        """Return a raw dict {flags, evidence_ids, reasoning}.

        `kb_block` is the pre-formatted exclusion-category knowledge base for the
        requested categories. The classifier MUST cite KB row ids in `evidence_ids`
        when a flag is supported by a direct match.
        """

    def classify_admission_type(
        self,
        diagnosis: str,
        benefit_name: str,
    ) -> dict[str, Any]:
        """Return a raw dict {admission_type, confidence, reasoning}.

        Decides whether an Inpatient & Surgery admission was 'elective' or an
        'emergency' (GC-3's pre-auth penalty exempts emergencies). No KB is used —
        the signal is read from the diagnosis text.
        """
