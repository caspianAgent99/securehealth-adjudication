"""Deterministic PDF table extractor — the **only** claim ingestion path.

Reads two tables from the SecureHealth claim scenario PDF:
  1. The member header (Field/Value pairs) → MemberContext (member id, declared chronic condition).
  2. The claim events table → normalized claim rows.

Both go through pdfplumber, which works against the text layer the brief's PDFs carry.
No OCR. Cell wrapping (newlines inside a cell) is handled by the normalizers.

Documented failure mode: column misalignment on truly empty / unrecognised cells. That is
why downstream validation still runs — bad cells turn into precise per-row error messages,
not silently-wrong numbers.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..models.member import MemberContext
from .row_schema import (
    normalize_benefit_label,
    normalize_network,
    normalize_preauth,
)


HEADER_ALIASES = {
    "claim": "claim_id",
    "claim id": "claim_id",
    "service date": "service_date",
    "date": "service_date",
    "benefit": "benefit_key",
    "network": "network_status",
    "billed": "billed_amount",
    "billed (aed)": "billed_amount",
    "billed aed": "billed_amount",
    "billed amount": "billed_amount",
    "eligible": "eligible_amount",
    "pre-auth": "preauth_status",
    "preauth": "preauth_status",
    "pre auth": "preauth_status",
    "diagnosis": "diagnosis",
    "diagnosis / note": "diagnosis",
    "diagnosis/note": "diagnosis",
    "diagnosis note": "diagnosis",
    "provider": "provider",
}


@dataclass
class ExtractedClaimSheet:
    """Bundle returned by PDFClaimExtractor — header context + claim rows from one document."""

    member: MemberContext
    rows: list[dict[str, Any]] = field(default_factory=list)


def _norm_header(h: str | None) -> str | None:
    """Normalize a header cell, robust to mid-word wrapping ('Clai\\nm' → 'claim')."""

    if h is None:
        return None
    spaced = " ".join(str(h).split()).strip().lower()     # collapses wraps to single spaces
    joined = spaced.replace(" ", "")                      # also try the no-space form, for 'clai m' → 'claim'
    if spaced in HEADER_ALIASES:
        return HEADER_ALIASES[spaced]
    if joined in HEADER_ALIASES:
        return HEADER_ALIASES[joined]
    return spaced.replace(" ", "_")


def _clean_cell(s: Any) -> str:
    """Collapse internal whitespace introduced by PDF cell wrapping."""

    if s is None:
        return ""
    return " ".join(str(s).split()).strip()


def _parse_money(s: str) -> str:
    out = []
    for ch in s:
        if ch.isdigit() or ch == ".":
            out.append(ch)
    return "".join(out)


def _parse_date_to_iso(s: str) -> str:
    from datetime import datetime

    s = s.strip()
    for fmt in ("%d %b %Y", "%d %B %Y", "%Y-%m-%d", "%d/%m/%Y", "%m/%d/%Y"):
        try:
            return datetime.strptime(s, fmt).date().isoformat()
        except ValueError:
            continue
    return s


def _is_member_header_table(table: list[list[str | None]]) -> bool:
    if not table or len(table[0]) != 2:
        return False
    first = " ".join((table[0][0] or "").split()).strip().lower()
    second = " ".join((table[0][1] or "").split()).strip().lower()
    return first == "field" and second == "value"


def _is_claims_table(table: list[list[str | None]]) -> bool:
    if not table:
        return False
    header = [_norm_header(c) for c in table[0]]
    return "claim_id" in header


_NO_CHRONIC_SENTINELS = frozenset(
    {
        "",
        "none",
        "none declared",
        "no chronic",
        "no chronic condition",
        "no chronic conditions",
        "n/a",
        "na",
        "not applicable",
        "not declared",
        "nil",
    }
)


def _extract_declared_conditions(value: str) -> list[str]:
    """Pull condition names from a free-form field like 'Asthma (declared at enrolment)'.

    Returns an empty list when the field is a "no chronic condition" sentinel
    (e.g. "None declared", "N/A"), so downstream classifiers aren't asked to
    classify against a meaningless string.
    """

    cleaned = value.split("(")[0].strip().lower()
    if cleaned in _NO_CHRONIC_SENTINELS:
        return []
    parts: list[str] = []
    for chunk in cleaned.replace(" and ", ",").split(","):
        c = chunk.strip()
        if c and c not in _NO_CHRONIC_SENTINELS:
            parts.append(c)
    return parts


def _parse_inception_date(value: str) -> "date | None":
    from datetime import date as _date, datetime

    s = (value or "").strip()
    if not s:
        return None
    for fmt in ("%d %B %Y", "%d %b %Y", "%Y-%m-%d", "%d/%m/%Y", "%m/%d/%Y"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def _member_from_header_table(table: list[list[str | None]]) -> MemberContext:
    member_id: str | None = None
    inception = None
    declared: list[str] = []
    for row in table[1:]:
        if len(row) < 2:
            continue
        label = _clean_cell(row[0]).lower()
        value = _clean_cell(row[1])
        if "inception" in label:
            inception = _parse_inception_date(value)
        elif "declared" in label or "chronic" in label or "pre-existing" in label:
            declared = _extract_declared_conditions(value)
        elif "member" in label:
            member_id = value or None
    return MemberContext(
        member_id=member_id,
        inception_date=inception,
        declared_chronic_conditions=declared,
    )


def _rows_from_claims_table(table: list[list[str | None]]) -> list[dict[str, Any]]:
    header = [_norm_header(c) for c in table[0]]
    rows: list[dict[str, Any]] = []
    for raw_row in table[1:]:
        if not raw_row or all((c is None or _clean_cell(c) == "") for c in raw_row):
            continue
        d: dict[str, Any] = {}
        for col, val in zip(header, raw_row):
            if col is None:
                continue
            d[col] = _clean_cell(val)
        if "benefit_key" in d:
            d["benefit_key"] = normalize_benefit_label(d["benefit_key"])
        if "network_status" in d:
            d["network_status"] = normalize_network(d["network_status"])
        if "preauth_status" in d:
            d["preauth_status"] = normalize_preauth(d["preauth_status"])
        if "billed_amount" in d:
            d["billed_amount"] = _parse_money(d["billed_amount"])
        if "eligible_amount" in d and d["eligible_amount"]:
            d["eligible_amount"] = _parse_money(d["eligible_amount"])
        if "service_date" in d:
            d["service_date"] = _parse_date_to_iso(d["service_date"])
        rows.append(d)
    return rows


class PDFClaimExtractor:
    """The only claim ingestion implementation. Extracts member context AND claim rows."""

    def extract(self, source: str | Path) -> ExtractedClaimSheet:
        try:
            import pdfplumber
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError(
                "pdfplumber not installed. `pip install pdfplumber` to use the PDF claim extractor."
            ) from exc

        path = Path(source)
        if not path.exists():
            raise FileNotFoundError(f"claim PDF not found: {path}")

        sheet = ExtractedClaimSheet(member=MemberContext(), rows=[])
        with pdfplumber.open(str(path)) as pdf:
            for page in pdf.pages:
                tables = page.extract_tables() or []
                for table in tables:
                    if _is_member_header_table(table):
                        sheet.member = _member_from_header_table(table)
                    elif _is_claims_table(table):
                        sheet.rows.extend(_rows_from_claims_table(table))
        return sheet

    def extract_from_bytes(self, content: bytes) -> ExtractedClaimSheet:
        """Helper for HTTP upload — writes a temp file and re-uses extract()."""

        import io
        import tempfile

        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=True) as tmp:
            tmp.write(content)
            tmp.flush()
            return self.extract(tmp.name)
