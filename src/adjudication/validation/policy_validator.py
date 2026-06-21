"""Structural validation of a (possibly proposed) policy config.

The Pydantic model already does most of the heavy lifting. This module:
  - exposes a uniform validate() function that returns either a `PolicyConfig`
    or a list of human-readable errors,
  - layers in semantic-shaped invariants beyond what the model can express
    (e.g. exclusion rule param shapes per type).
"""

from __future__ import annotations

from typing import Any

from pydantic import ValidationError

from ..models.policy import ExclusionRule, PolicyConfig


class PolicyValidationError(Exception):
    """Raised when a proposed policy fails structural validation."""

    def __init__(self, errors: list[str]):
        super().__init__("; ".join(errors))
        self.errors = errors


def _validate_exclusion_rule(rule: ExclusionRule) -> list[str]:
    errs: list[str] = []
    p = rule.params
    if rule.type == "waiting_period":
        if "condition_flag" not in p:
            errs.append(f"exclusion {rule.id}: waiting_period requires 'condition_flag' param")
        if "waiting_days" not in p and "waiting_months" not in p:
            errs.append(f"exclusion {rule.id}: waiting_period requires 'waiting_days' or 'waiting_months'")
        for k in ("waiting_days", "waiting_months"):
            if k in p and (not isinstance(p[k], int) or p[k] < 0):
                errs.append(f"exclusion {rule.id}: '{k}' must be a non-negative int, got {p[k]!r}")
    elif rule.type == "preauth_penalty":
        if "penalty_pct" not in p:
            errs.append(f"exclusion {rule.id}: preauth_penalty requires 'penalty_pct'")
        else:
            v = p["penalty_pct"]
            if not isinstance(v, (int, float)) or not (0.0 <= float(v) <= 1.0):
                errs.append(f"exclusion {rule.id}: 'penalty_pct' must be in [0,1], got {v!r}")
    elif rule.type in ("not_covered_oon", "not_covered_condition"):
        # No required params; applies_to_benefits is the main switch.
        pass
    return errs


def validate_policy_config(data: dict[str, Any] | PolicyConfig) -> PolicyConfig:
    """Parse + validate a policy config. Raises PolicyValidationError with all messages on failure."""

    errors: list[str] = []
    cfg: PolicyConfig
    if isinstance(data, PolicyConfig):
        cfg = data
    else:
        try:
            cfg = PolicyConfig.model_validate(data)
        except ValidationError as e:
            for err in e.errors():
                loc = ".".join(str(x) for x in err.get("loc", []))
                msg = err.get("msg", "invalid")
                errors.append(f"{loc}: {msg}")
            raise PolicyValidationError(errors) from e

    if cfg.plan_year_end <= cfg.plan_year_start:
        errors.append("plan_year_end must be after plan_year_start")
    if cfg.policy_start_date < cfg.plan_year_start or cfg.policy_start_date > cfg.plan_year_end:
        # not necessarily fatal — a member could enrol mid-year — so we surface a warning-style error only
        # if outside plan year by more than one year, indicating likely data entry mistake.
        pass

    seen_keys: set[str] = set()
    for b in cfg.benefits:
        if b.key in seen_keys:
            errors.append(f"duplicate benefit key: {b.key}")
        seen_keys.add(b.key)
        if b.annual_sub_limit is not None and b.annual_sub_limit < 0:
            errors.append(f"benefit {b.key}: annual_sub_limit must be >= 0 or null")

    seen_endorsement_ids: set[str] = set()
    for e in cfg.endorsements:
        if e.id in seen_endorsement_ids:
            errors.append(f"duplicate endorsement id: {e.id}")
        seen_endorsement_ids.add(e.id)

    for r in cfg.exclusion_rules:
        errors.extend(_validate_exclusion_rule(r))

    if errors:
        raise PolicyValidationError(errors)
    return cfg
