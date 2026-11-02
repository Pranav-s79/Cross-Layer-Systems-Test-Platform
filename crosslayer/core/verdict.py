"""Verdict vocabulary used by rules, contracts, and runs."""

from __future__ import annotations

from enum import StrEnum


class Verdict(StrEnum):
    PASS = "pass"
    """The contract was evaluated against sufficient evidence and held."""

    FAIL = "fail"
    """At least one concrete, evidence-backed violation was observed."""

    INCONCLUSIVE = "inconclusive"
    """Evidence was insufficient to confirm either outcome."""

    SKIPPED = "skipped"
    """The contract's precondition was not met, so it did not apply."""

    ERROR = "error"
    """Verification itself broke (bad expression, adapter crash, timeout)."""


# CLI / CI exit codes. Kept stable so pipelines can branch on them.
EXIT_CODES: dict[Verdict, int] = {
    Verdict.PASS: 0,
    Verdict.FAIL: 1,
    Verdict.INCONCLUSIVE: 2,
    Verdict.SKIPPED: 2,
    Verdict.ERROR: 3,
}
EXIT_CONFIG_ERROR = 4
