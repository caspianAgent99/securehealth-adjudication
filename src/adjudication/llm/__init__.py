"""Anthropic transport layer.

Direct callers should use `adjudication.services.LLMService` instead of importing
the provider from here. This module exists to declare the transport contract and
the concrete Anthropic implementation.
"""

from .anthropic_provider import AnthropicProvider, AnthropicProviderUnavailable
from .provider import LLMProvider
from .types import (
    ClaimCategoryClassification,
    PolicyProposal,
    PreExistingClassification,
)

__all__ = [
    "AnthropicProvider",
    "AnthropicProviderUnavailable",
    "LLMProvider",
    "PolicyProposal",
    "PreExistingClassification",
    "ClaimCategoryClassification",
]
