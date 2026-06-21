"""Command-line entry point.

Runs the bundled claim PDF through the full pipeline:
    extract (pdfplumber) → validate (Pydantic) → enrich pre-existing flag (LLM classifier) → adjudicate.

The PDF is the only supported claim format.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .config import SETTINGS
from .engine.adjudicator import adjudicate
from .enrichment import enrich_category_flags, enrich_preexisting_links
from .extraction.claim_pdf import PDFClaimExtractor
from .reporting.json_report import report_to_json
from .reporting.table_report import report_to_table
from .services.llm_service import LLMService
from .validation.claim_validator import ClaimValidationError, validate_claim_rows
from .validation.policy_validator import PolicyValidationError, validate_policy_config


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="adjudicate", description="Run claim adjudication.")
    p.add_argument("--policy", default=str(SETTINGS.policy_path), help="Path to frozen policy JSON.")
    p.add_argument("--claims", default=str(SETTINGS.claims_path), help="Path to claim PDF.")
    p.add_argument("--format", choices=["table", "json", "both"], default="both")
    p.add_argument("--out-json", default=None, help="Optional path to write the JSON report.")
    p.add_argument(
        "--declared-chronic",
        action="append",
        default=[],
        help=(
            "Override declared chronic condition(s) (repeatable). When omitted, the conditions "
            "are read from the claim PDF's header table."
        ),
    )
    args = p.parse_args(argv)

    policy_path = Path(args.policy)
    try:
        policy = validate_policy_config(json.loads(policy_path.read_text(encoding="utf-8")))
    except (PolicyValidationError, FileNotFoundError, json.JSONDecodeError) as e:
        print(f"[policy] {e}", file=sys.stderr)
        return 2

    claims_path = Path(args.claims)
    if claims_path.suffix.lower() != ".pdf":
        print(f"[claims] unsupported file type {claims_path.suffix!r} — only .pdf is accepted.", file=sys.stderr)
        return 4

    sheet = PDFClaimExtractor().extract(claims_path)

    try:
        claims = validate_claim_rows(sheet.rows, policy)
    except ClaimValidationError as e:
        print("[claims] validation failed:", file=sys.stderr)
        for err in e.errors:
            print(f"  - {err}", file=sys.stderr)
        return 3

    member = sheet.member
    if args.declared_chronic:
        member = member.model_copy(update={"declared_chronic_conditions": args.declared_chronic})
    service = LLMService()
    if member.declared_chronic_conditions:
        claims = enrich_preexisting_links(claims, member, service)
    claims = enrich_category_flags(claims, policy, service)

    report = adjudicate(claims, policy, member=member)

    if args.format in ("table", "both"):
        print(report_to_table(report))
        print()
    if args.format in ("json", "both"):
        js = report_to_json(report)
        if args.out_json:
            Path(args.out_json).write_text(js, encoding="utf-8")
            print(f"Wrote JSON report to {args.out_json}")
        else:
            print(js)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
