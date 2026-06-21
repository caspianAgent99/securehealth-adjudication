"""The ingestion edge — quarantined behind a normalized row contract.

Single supported claim format: PDF tables, via `PDFClaimExtractor`.
"""

from .base import Extractor
from .claim_pdf import ExtractedClaimSheet, PDFClaimExtractor
from .policy_llm import LLMPolicyExtractor
from .row_schema import ALLOWED_CLAIM_ROW_FIELDS, REQUIRED_CLAIM_ROW_FIELDS

__all__ = [
    "Extractor",
    "PDFClaimExtractor",
    "ExtractedClaimSheet",
    "LLMPolicyExtractor",
    "REQUIRED_CLAIM_ROW_FIELDS",
    "ALLOWED_CLAIM_ROW_FIELDS",
]
