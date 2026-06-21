"""Extractor protocol.

The claim ingestion path currently has a single implementation (PDF tables).
If a future format is added it must produce the same `ExtractedClaimSheet`.
"""

from __future__ import annotations

from typing import Any, Protocol


class Extractor(Protocol):
    def extract(self, source: Any) -> Any: ...
