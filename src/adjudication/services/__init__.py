"""Service layer — high-level facades that compose providers, models, and data.

Naming convention: anything in this package is the *only* thing the API/UI/engine call.
Providers and raw models are dependencies of services, not of callers.
"""

from .clinical_kb import ClinicalKB, ExclusionCategoryKB
from .llm_service import LLMService
from .questions_service import QuestionsService

__all__ = ["ClinicalKB", "ExclusionCategoryKB", "LLMService", "QuestionsService"]
