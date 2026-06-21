"""Process-wide configuration. Keep settings here so business code stays paths-agnostic."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

try:
    from dotenv import load_dotenv

    load_dotenv()
except Exception:
    pass

REPO_ROOT = Path(__file__).resolve().parents[2]


def _abs(path_str: str) -> Path:
    p = Path(path_str)
    return p if p.is_absolute() else (REPO_ROOT / p)


@dataclass(frozen=True)
class Settings:
    anthropic_api_key: str | None = os.getenv("ANTHROPIC_API_KEY") or None
    anthropic_model: str = os.getenv("ANTHROPIC_MODEL", "claude-opus-4-7")
    policy_path: Path = _abs(os.getenv("POLICY_PATH", "data/policy/securehealth_plan_b.json"))
    claims_path: Path = _abs(os.getenv("CLAIMS_PATH", "data/claims/03_Claim_Scenario_Main.pdf"))
    clinical_kb_path: Path = _abs(os.getenv("CLINICAL_KB_PATH", "data/clinical_kb/chronic_conditions.json"))
    exclusion_kb_path: Path = _abs(os.getenv("EXCLUSION_KB_PATH", "data/clinical_kb/exclusion_categories.json"))
    api_base_url: str = os.getenv("API_BASE_URL", "http://127.0.0.1:8000")


SETTINGS = Settings()
