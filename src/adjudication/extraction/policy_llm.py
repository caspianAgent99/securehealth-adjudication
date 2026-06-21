"""LLM-proposes-config path. Returns a *proposed* PolicyConfig + citations.

Never trusted directly — the caller passes the result through validation and (in
the API/UI) a human-approve gate before freezing to disk.

Consumes an `LLMService` (not a raw provider) so all LLM calls in the app go through
the same facade.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..models.policy import PolicyConfig
from ..services.llm_service import LLMService
from ..validation.policy_validator import PolicyValidationError, validate_policy_config


@dataclass
class PolicyExtractionResult:
    policy: PolicyConfig | None
    citations: dict[str, str]
    validation_errors: list[str]
    raw: dict[str, Any]


class LLMPolicyExtractor:
    def __init__(self, service: LLMService):
        self.service = service

    def extract(self, policy_text: str) -> PolicyExtractionResult:
        proposal = self.service.propose_policy(policy_text)
        try:
            cfg = validate_policy_config(proposal.config)
            return PolicyExtractionResult(
                policy=cfg, citations=proposal.citations, validation_errors=[], raw=proposal.config
            )
        except PolicyValidationError as e:
            return PolicyExtractionResult(
                policy=None,
                citations=proposal.citations,
                validation_errors=e.errors,
                raw=proposal.config,
            )
