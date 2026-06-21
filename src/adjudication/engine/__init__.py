"""Deterministic adjudication engine. No I/O, no LLM, no clock, no globals."""

from .adjudicator import adjudicate
from .accumulator import LimitAccumulator
from .calculation import calculate_claim
from .exclusions import GateOutcome, evaluate_exclusions

__all__ = [
    "adjudicate",
    "LimitAccumulator",
    "calculate_claim",
    "GateOutcome",
    "evaluate_exclusions",
]
