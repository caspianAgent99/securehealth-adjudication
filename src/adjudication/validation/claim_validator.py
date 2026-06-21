"""Turn normalized extractor rows into typed Claim objects, failing loudly on bad data."""

from __future__ import annotations

from datetime import date, datetime
from typing import Any, Iterable

from pydantic import ValidationError

from ..models.claim import Claim, NetworkStatus, PreAuthStatus, PreExistingLink
from ..models.policy import PolicyConfig


class ClaimValidationError(Exception):
    def __init__(self, errors: list[str]):
        super().__init__("; ".join(errors))
        self.errors = errors


def _parse_date(s: Any, label: str) -> date:
    if isinstance(s, date) and not isinstance(s, datetime):
        return s
    if isinstance(s, datetime):
        return s.date()
    if not isinstance(s, str):
        raise ValueError(f"{label}: expected ISO date string, got {type(s).__name__}")
    s = s.strip()
    # try ISO first, then a few common alternatives
    fmts = ("%Y-%m-%d", "%d %b %Y", "%d %B %Y", "%d/%m/%Y", "%m/%d/%Y")
    for fmt in fmts:
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    raise ValueError(f"{label}: cannot parse date {s!r}")


def _to_bool(v: Any) -> bool:
    if isinstance(v, bool):
        return v
    if isinstance(v, str):
        s = v.strip().lower()
        if s in ("true", "yes", "y", "1"):
            return True
        if s in ("false", "no", "n", "0", ""):
            return False
    if isinstance(v, (int, float)):
        return bool(v)
    raise ValueError(f"cannot parse boolean from {v!r}")


def _to_float(v: Any, label: str) -> float:
    if v is None or v == "":
        raise ValueError(f"{label}: missing")
    try:
        return float(v)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label}: not numeric — {v!r}") from exc


def _to_optional_float(v: Any) -> float | None:
    if v is None or (isinstance(v, str) and v.strip() == ""):
        return None
    return float(v)


def validate_claim_row(row: dict[str, Any], policy: PolicyConfig, *, row_index: int | None = None) -> Claim:
    """Convert one normalized row to a typed Claim. Raises ClaimValidationError with all messages."""

    errors: list[str] = []
    where = f"row {row_index}" if row_index is not None else f"row {row.get('claim_id', '?')}"

    # claim_id
    claim_id = str(row.get("claim_id", "")).strip()
    if not claim_id:
        errors.append(f"{where}: claim_id is required")

    # service_date
    service_date: date | None = None
    try:
        service_date = _parse_date(row.get("service_date"), f"{where}.service_date")
    except ValueError as e:
        errors.append(str(e))

    # benefit_key must join to policy
    benefit_key = str(row.get("benefit_key", "")).strip()
    if not benefit_key:
        errors.append(f"{where}: benefit_key is required")
    else:
        if benefit_key not in {b.key for b in policy.benefits}:
            errors.append(
                f"{where}: benefit '{benefit_key}' not found in policy {policy.policy_ref}; "
                f"known: {[b.key for b in policy.benefits]}"
            )

    # network_status
    network_str = str(row.get("network_status", "")).strip().lower().replace("-", "_").replace(" ", "_")
    if network_str in ("in_network", "innetwork"):
        network = NetworkStatus.IN_NETWORK
    elif network_str in ("out_of_network", "oon", "outofnetwork"):
        network = NetworkStatus.OUT_OF_NETWORK
    else:
        network = NetworkStatus.IN_NETWORK  # placeholder; we have already recorded an error
        errors.append(f"{where}: unknown network_status {row.get('network_status')!r}")

    # preauth
    preauth_str = str(row.get("preauth_status", "")).strip().lower().replace("-", "_").replace(" ", "_")
    if preauth_str in ("not_applicable", "notapplicable", "not_required", "n/a", "na"):
        preauth = PreAuthStatus.NOT_APPLICABLE
    elif preauth_str in ("obtained", "yes", "y"):
        preauth = PreAuthStatus.OBTAINED
    elif preauth_str in ("not_obtained", "no", "n"):
        preauth = PreAuthStatus.NOT_OBTAINED
    else:
        preauth = PreAuthStatus.NOT_APPLICABLE
        errors.append(f"{where}: unknown preauth_status {row.get('preauth_status')!r}")

    # amounts
    billed = 0.0
    try:
        billed = _to_float(row.get("billed_amount"), f"{where}.billed_amount")
        if billed < 0:
            errors.append(f"{where}: billed_amount must be >= 0")
    except ValueError as e:
        errors.append(str(e))

    eligible: float | None = None
    try:
        eligible = _to_optional_float(row.get("eligible_amount"))
        if eligible is not None and eligible < 0:
            errors.append(f"{where}: eligible_amount must be >= 0")
    except ValueError as e:
        errors.append(str(e))

    # derived pre-existing link
    pre_link: PreExistingLink | None = None
    raw_flag = row.get("is_related_to_preexisting")
    if raw_flag is not None and str(raw_flag).strip() != "":
        try:
            flag = _to_bool(raw_flag)
            reasoning = str(row.get("preexisting_reasoning", "")).strip() or (
                "Linked to declared chronic condition." if flag else "Not linked to any declared chronic condition."
            )
            pre_link = PreExistingLink(is_related=flag, reasoning=reasoning, source="manual")
        except ValueError as e:
            errors.append(f"{where}: {e}")

    if errors:
        raise ClaimValidationError(errors)

    assert service_date is not None
    try:
        return Claim(
            claim_id=claim_id,
            service_date=service_date,
            benefit_key=benefit_key,
            network_status=network,
            provider=row.get("provider") or None,
            billed_amount=billed,
            eligible_amount=eligible,
            preauth_status=preauth,
            diagnosis=row.get("diagnosis") or None,
            pre_existing_link=pre_link,
        )
    except ValidationError as e:
        raise ClaimValidationError([f"{where}: {err.get('msg')}" for err in e.errors()]) from e


def validate_claim_rows(rows: Iterable[dict[str, Any]], policy: PolicyConfig) -> list[Claim]:
    """Validate many rows; raise ClaimValidationError with EVERY problem found across all rows."""

    out: list[Claim] = []
    all_errors: list[str] = []
    for i, row in enumerate(rows):
        try:
            out.append(validate_claim_row(row, policy, row_index=i))
        except ClaimValidationError as e:
            all_errors.extend(e.errors)
    if all_errors:
        raise ClaimValidationError(all_errors)
    return out
