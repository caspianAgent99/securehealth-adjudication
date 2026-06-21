"""Machine-readable settlement output."""

from __future__ import annotations

import json
from typing import Any

from ..models.settlement import SettlementReport


def report_to_json(report: SettlementReport, *, indent: int | None = 2) -> str:
    payload: dict[str, Any] = report.model_dump(mode="json")
    return json.dumps(payload, indent=indent, default=str)
