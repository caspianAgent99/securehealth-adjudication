"""The normalized row contract every claim extractor must emit.

String-typed at this stage; typing happens downstream in validation/claim_validator.py.
Raw fields (diagnosis, billed_amount) coexist with derived fields (is_related_to_preexisting,
preexisting_reasoning). Derived never overwrites raw.
"""

from __future__ import annotations

REQUIRED_CLAIM_ROW_FIELDS: tuple[str, ...] = (
    "claim_id",
    "service_date",
    "benefit_key",
    "network_status",
    "billed_amount",
    "preauth_status",
)

ALLOWED_CLAIM_ROW_FIELDS: tuple[str, ...] = REQUIRED_CLAIM_ROW_FIELDS + (
    "provider",
    "eligible_amount",
    "diagnosis",
    "is_related_to_preexisting",
    "preexisting_reasoning",
)

# Normalized mapping for free-form text benefit labels (e.g. from a PDF) to canonical keys.
BENEFIT_LABEL_MAP: dict[str, str] = {
    "outpatient consultation": "outpatient_consultation",
    "outpatient": "outpatient_consultation",
    "consultation": "outpatient_consultation",
    "diagnostics": "diagnostics",
    "diagnostics (lab & imaging)": "diagnostics",
    "lab": "diagnostics",
    "imaging": "diagnostics",
    "prescribed medication": "pharmacy",
    "prescribed medication (pharmacy)": "pharmacy",
    "pharmacy": "pharmacy",
    "medication": "pharmacy",
    "physiotherapy": "physiotherapy",
    "physio": "physiotherapy",
    "inpatient & surgery": "inpatient_surgery",
    "inpatient": "inpatient_surgery",
    "surgery": "inpatient_surgery",
    "inpatient and surgery": "inpatient_surgery",
}


def normalize_benefit_label(label: str) -> str:
    # collapse all whitespace (newlines from PDF-wrapped cells, tabs, repeats)
    s = " ".join(label.split()).strip().lower()
    if s in BENEFIT_LABEL_MAP:
        return BENEFIT_LABEL_MAP[s]
    # Fallback: snake_case the label as-is.
    return s.replace("&", "and").replace("(", "").replace(")", "").replace(" ", "_")


def normalize_network(label: str) -> str:
    # PDF wrap can split words mid-character, e.g. "Out-of-Netwo\nrk". Strip ALL whitespace
    # first to glue wrapped pieces back together, then check substring.
    s = "".join(label.split()).lower().replace("-", "").replace("_", "")
    if "out" in s and "network" in s:
        return "out_of_network"
    return "in_network"


def normalize_preauth(label: str) -> str:
    s = " ".join(label.split()).strip().lower()
    if s in ("n/a", "na", "not applicable", "not_applicable", "not required", "not_required", ""):
        return "not_applicable"
    if s in ("yes", "y", "obtained"):
        return "obtained"
    if s in ("no", "n", "not obtained", "not_obtained"):
        return "not_obtained"
    return s
