"""Verification and digit mapping logic."""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(slots=True)
class VerificationInput:
    osu_id: int
    username: str
    global_rank: int | None


def extract_osu_identifier(raw: str) -> str:
    """Extract username/id token from plain name or profile URL."""
    value = raw.strip()
    m = re.search(r"osu\.ppy\.sh/users/([^/?#]+)", value)
    if m:
        return m.group(1)
    return value


def compute_digit_value(
    verification_input: VerificationInput,
    mode: str,
    *,
    digit_modulus: int = 10,
) -> int:
    """Compute role digit according to configured mode."""
    if mode == "rank_digit_count":
        if verification_input.global_rank is None:
            raise ValueError("global_rank is required for rank_digit_count")
        return len(str(verification_input.global_rank))
    if mode == "last_digit_of_userid":
        return verification_input.osu_id % 10
    if mode == "sum_of_digits_mod_X":
        digits = [int(c) for c in str(verification_input.osu_id)]
        return sum(digits) % digit_modulus
    raise ValueError(f"Unsupported verification mode: {mode}")
