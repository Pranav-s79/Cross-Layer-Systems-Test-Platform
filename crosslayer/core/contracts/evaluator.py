"""Evaluate contracts against an event stream and aggregate a run verdict."""

from __future__ import annotations

import traceback
from typing import Sequence

from crosslayer.core.contracts.model import (
    BoundaryContract,
    ContractResult,
    FailurePolicy,
)
from crosslayer.core.telemetry.events import NormalizedEvent
from crosslayer.core.verdict import Verdict


def evaluate_contract(contract: BoundaryContract, events: Sequence[NormalizedEvent]) -> ContractResult:
    """Evaluate one contract. ``events`` must already be time-ordered.

    Order of decisions:

    1. Precondition not observed -> SKIPPED (the contract does not apply).
    2. Rule raises -> ERROR (the verification is broken, not the system).
    3. Required evidence missing -> INCONCLUSIVE, whatever the rule said.
       The rule's own verdict is kept in ``rule_verdict`` for investigation.
    4. Otherwise the rule's verdict stands.
    """
    if contract.precondition is not None and not any(contract.precondition.matches(e) for e in events):
        return ContractResult(
            contract=contract,
            verdict=Verdict.SKIPPED,
            summary=f"precondition not observed ({contract.precondition.describe()})",
        )

    try:
        outcome = contract.rule.evaluate(events)
    except Exception as exc:  # noqa: BLE001 - any rule crash must become a recorded ERROR
        return ContractResult(
            contract=contract,
            verdict=Verdict.ERROR,
            summary=f"rule evaluation failed: {type(exc).__name__}: {exc}",
            error="".join(traceback.format_exception(exc)),
        )

    missing = [sel.to_dict() for sel in contract.evidence if not any(sel.matches(e) for e in events)]
    if missing:
        return ContractResult(
            contract=contract,
            verdict=Verdict.INCONCLUSIVE,
            summary=(
                f"required evidence not observed ({len(missing)} selector(s)); "
                f"rule alone would say {outcome.verdict.upper()}: {outcome.summary}"
            ),
            findings=outcome.findings,
            stats=outcome.stats,
            missing_evidence=missing,
            rule_verdict=outcome.verdict,
        )

    return ContractResult(
        contract=contract,
        verdict=outcome.verdict,
        summary=outcome.summary,
        findings=outcome.findings,
        stats=outcome.stats,
        rule_verdict=outcome.verdict,
    )


def evaluate_contracts(
    contracts: Sequence[BoundaryContract], events: Sequence[NormalizedEvent]
) -> list[ContractResult]:
    return [evaluate_contract(c, events) for c in contracts]


def aggregate_verdict(results: Sequence[ContractResult]) -> Verdict:
    """Combine contract verdicts into one run verdict.

    * Any contract ERROR -> ERROR.
    * Any ``fail_run`` contract FAIL -> FAIL.
    * Any ``fail_run`` contract INCONCLUSIVE -> INCONCLUSIVE.
    * No ``fail_run`` contract reached PASS (none defined, or all skipped)
      -> INCONCLUSIVE: nothing was actually verified.
    * Otherwise PASS. ``warn`` contracts never change the verdict.
    """
    if any(r.verdict is Verdict.ERROR for r in results):
        return Verdict.ERROR
    gating = [r for r in results if r.contract.on_fail is FailurePolicy.FAIL_RUN]
    if any(r.verdict is Verdict.FAIL for r in gating):
        return Verdict.FAIL
    if any(r.verdict is Verdict.INCONCLUSIVE for r in gating):
        return Verdict.INCONCLUSIVE
    if not any(r.verdict is Verdict.PASS for r in gating):
        return Verdict.INCONCLUSIVE
    return Verdict.PASS


def warnings(results: Sequence[ContractResult]) -> list[ContractResult]:
    return [
        r
        for r in results
        if r.contract.on_fail is FailurePolicy.WARN and r.verdict in (Verdict.FAIL, Verdict.INCONCLUSIVE)
    ]
