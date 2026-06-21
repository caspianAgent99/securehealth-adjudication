"""Typed return shapes the LLMService produces. Providers return raw dicts; the
service parses them into these. Kept here (not in models/) because they're an
implementation detail of the LLM layer, not domain models."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal


Confidence = Literal["high", "low"]


@dataclass
class PolicyProposal:
    """LLM's proposed policy config + per-field citations back into the source document."""

    config: dict[str, Any]
    citations: dict[str, str] = field(default_factory=dict)


@dataclass
class PreExistingClassification:
    is_related: bool
    confidence: str                       # one of "high" | "low"
    evidence_ids: list[str]               # KB row ids cited by the model (validated against the KB)
    reasoning: str
    requires_review: bool                 # derived: confidence != "high"  (i.e. confidence == "low")
    kb_size: int = 0                      # how many KB rows were available for this condition


@dataclass
class ClaimCategoryClassification:
    flags: list[str]                      # subset of the categories that the diagnosis matched
    evidence_ids: list[str]               # KB row ids cited by the classifier (validated against the KB)
    confidence: str                       # one of "high" | "low"
    reasoning: str
    requires_review: bool                 # derived: confidence != "high"  (i.e. confidence == "low")
    kb_size: int = 0                      # how many KB rows were available for these categories
