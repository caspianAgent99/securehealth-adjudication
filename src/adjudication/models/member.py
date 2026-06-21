"""Member-side context. Kept separate from PolicyConfig — it's per-member, not per-plan."""

from __future__ import annotations

from datetime import date

from pydantic import BaseModel, ConfigDict, Field


class MemberContext(BaseModel):
    model_config = ConfigDict(extra="forbid")

    member_id: str | None = None
    inception_date: date | None = Field(
        default=None,
        description=(
            "The member's policy inception date (per the claim PDF header). "
            "Anchors §4.2 waiting-period math. Falls back to PolicyConfig.policy_start_date if absent."
        ),
    )
    declared_chronic_conditions: list[str] = Field(default_factory=list)
