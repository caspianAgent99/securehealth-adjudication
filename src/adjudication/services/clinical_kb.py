"""Clinical & policy-exclusion knowledge bases — the auditable data behind the LLM classifiers.

Two flat JSON files, two near-identical loaders:

  - `ClinicalKB`           — chronic conditions × clinical indicators (drives §4.2 grounding).
  - `ExclusionCategoryKB`  — §4.1 exclusion categories × diagnostic indicators
                              (cosmetic, self-inflicted, experimental).

Each row is self-contained, so a future swap to embedding-based retrieval (RAG) is
a single component change behind the same `for_*()` interface.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class KBRow:
    id: str
    group: str       # the categorical key (chronic condition OR exclusion category)
    indicator: str
    relation: str

    @classmethod
    def from_dict(cls, d: dict[str, Any], *, group_field: str) -> "KBRow":
        return cls(
            id=str(d["id"]),
            group=str(d[group_field]).strip().lower(),
            indicator=str(d["indicator"]),
            relation=str(d.get("relation", "")),
        )


class _IndicatorKB:
    """Generic loader/indexer. Concrete KBs are thin subclasses that pick the group field."""

    GROUP_FIELD: str = "group"

    def __init__(self, rows: list[KBRow]):
        self._rows = rows
        self._by_group: dict[str, list[KBRow]] = {}
        self._by_id: dict[str, KBRow] = {}
        for r in rows:
            self._by_group.setdefault(r.group, []).append(r)
            self._by_id[r.id] = r

    def get_row(self, kb_id: str) -> "KBRow | None":
        """O(1) lookup by row id. Used by the API to embed cited rows in responses."""

        return self._by_id.get(kb_id)

    @classmethod
    def load(cls, path: Path):
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        if not isinstance(data, list):
            raise ValueError(f"KB at {path} must be a JSON array of rows")
        return cls([KBRow.from_dict(r, group_field=cls.GROUP_FIELD) for r in data])

    @classmethod
    def empty(cls):
        return cls([])

    def _filter(self, key: str) -> list[KBRow]:
        return list(self._by_group.get(key.strip().lower(), []))

    def groups(self) -> list[str]:
        return sorted(self._by_group.keys())

    def total_rows(self) -> int:
        return len(self._rows)


class ClinicalKB(_IndicatorKB):
    """Chronic-condition → clinical-indicator KB (drives §4.2 grounding)."""

    GROUP_FIELD = "chronic"

    def for_condition(self, condition: str) -> list[KBRow]:
        return self._filter(condition)

    def conditions(self) -> list[str]:
        return self.groups()


class ExclusionCategoryKB(_IndicatorKB):
    """§4.1 exclusion-category → indicator KB (drives `classify_claim_categories` grounding)."""

    GROUP_FIELD = "category"

    def for_category(self, category: str) -> list[KBRow]:
        return self._filter(category)

    def for_categories(self, categories: list[str]) -> list[KBRow]:
        out: list[KBRow] = []
        seen: set[str] = set()
        for cat in categories:
            for r in self._filter(cat):
                if r.id not in seen:
                    out.append(r)
                    seen.add(r.id)
        return out

    def categories(self) -> list[str]:
        return self.groups()
