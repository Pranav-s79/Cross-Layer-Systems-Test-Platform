"""Static checks for contracts. Nothing runs; mistakes surface before a run.

Without an event catalog the linter checks contracts on their own (state
machines, statistics, deadlines). With an adapter's catalog it also checks
every selector and key against what the adapter can actually emit, which
catches misspelled event types, attributes, and correlation ids: mistakes
that otherwise show up only as a contract that is mysteriously never
satisfied.

| Code  | Level   | Finding |
|-------|---------|---------|
| CL001 | error   | contract endpoint is not a declared subsystem |
| CL002 | warning | critical contract only warns on failure |
| CL003 | warning | state unreachable from the initial states |
| CL004 | info    | state machine has no initial states |
| CL005 | info    | percentile computed over too few samples to differ from the maximum |
| CL006 | warning | response with no deadline and no required end-of-run evidence |
| CL007 | warning | response deadline is not shorter than the run timeout |
| CL008 | error   | final state cannot be reached from the initial states |
| CL010 | error   | selector matches no event type the adapter emits |
| CL011 | error   | key is never present on the events a selector matches |
| CL012 | warning | key is missing on some of the events a selector matches |
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from enum import StrEnum
from fnmatch import fnmatchcase
from typing import TYPE_CHECKING, Iterable, Sequence

from crosslayer.core.contracts.model import BoundaryContract, Criticality, FailurePolicy
from crosslayer.core.contracts.rules import ResponseRule, ThresholdRule, TransitionsRule
from crosslayer.core.contracts.selectors import Selector
from crosslayer.core.units import NS_PER_S

if TYPE_CHECKING:  # avoid an import cycle with adapters.base
    from crosslayer.core.adapters.base import EventSpec


class LintLevel(StrEnum):
    ERROR = "error"
    WARNING = "warning"
    INFO = "info"


_LEVEL_ORDER = {LintLevel.ERROR: 0, LintLevel.WARNING: 1, LintLevel.INFO: 2}


@dataclass(frozen=True)
class LintFinding:
    code: str
    level: LintLevel
    contract_id: str
    message: str

    def __str__(self) -> str:
        return f"{self.level.upper():<7} {self.code} {self.contract_id}: {self.message}"


def _candidates(selector: Selector, catalog: Sequence[EventSpec]) -> list[EventSpec]:
    """Catalog entries a selector could match, by event type and subsystem."""
    out = []
    for spec in catalog:
        if selector.event_types and not any(fnmatchcase(spec.event_type, p) for p in selector.event_types):
            continue
        if selector.subsystems and not any(
            fnmatchcase(sub, p) for sub in spec.subsystems for p in selector.subsystems
        ):
            continue
        out.append(spec)
    return out


def _has(spec: EventSpec, field: str, key: str) -> bool:
    if field == "attributes":
        return any(fnmatchcase(key, pattern) for pattern in spec.attributes)
    return key in spec.correlation_ids


def _label(field: str) -> str:
    return "attribute" if field == "attributes" else "correlation id"


def _reachable(initial: Iterable[str], allowed: dict[str, frozenset[str]]) -> set[str]:
    seen = set(initial)
    queue = deque(seen)
    while queue:
        for nxt in allowed.get(queue.popleft(), ()):
            if nxt not in seen:
                seen.add(nxt)
                queue.append(nxt)
    return seen


def _lint_rule(c: BoundaryContract, timeout_s: float | None) -> list[LintFinding]:
    out: list[LintFinding] = []
    rule = c.rule
    if isinstance(rule, TransitionsRule):
        if rule.initial is None:
            out.append(LintFinding("CL004", LintLevel.INFO, c.id,
                                   "no 'initial' states, so an entity's first state is never checked"))
        else:
            unreachable = sorted(rule.known_states - _reachable(rule.initial, rule.allowed))
            if unreachable:
                out.append(LintFinding("CL003", LintLevel.WARNING, c.id,
                                       f"states {unreachable} cannot be reached from {sorted(rule.initial)}"))
            if rule.final is not None:
                stranded = sorted(rule.final - _reachable(rule.initial, rule.allowed))
                if stranded:
                    out.append(LintFinding("CL008", LintLevel.ERROR, c.id,
                                           f"final states {stranded} cannot be reached from {sorted(rule.initial)}"))
    elif isinstance(rule, ThresholdRule) and rule.aggregate.startswith("p"):
        pct = int(rule.aggregate[1:])
        needed = 100 // (100 - pct)  # below this many samples, nearest-rank pN is the maximum
        if rule.min_samples < needed:
            out.append(LintFinding("CL005", LintLevel.INFO, c.id,
                                   f"min_samples is {rule.min_samples}; with fewer than {needed} samples "
                                   f"{rule.aggregate} equals the maximum sample"))
    elif isinstance(rule, ResponseRule):
        if rule.within_ns is None and not c.evidence:
            out.append(LintFinding("CL006", LintLevel.WARNING, c.id,
                                   "no deadline and no required end-of-run evidence: a truncated log will "
                                   "report missing responses as violations"))
        if rule.within_ns is not None and timeout_s is not None and rule.within_ns >= timeout_s * NS_PER_S:
            out.append(LintFinding("CL007", LintLevel.WARNING, c.id,
                                   f"deadline ({rule.within_ns / 1e6:g} ms) is not shorter than the run timeout "
                                   f"({timeout_s:g} s); missing responses can only be unresolved"))
    return out


def _lint_catalog(c: BoundaryContract, catalog: Sequence[EventSpec]) -> list[LintFinding]:
    out: list[LintFinding] = []
    named: dict[str, Selector] = dict(c.rule.selectors())
    if c.precondition is not None:
        named["precondition"] = c.precondition
    for i, sel in enumerate(c.evidence):
        named[f"evidence[{i}]"] = sel

    rule_reqs: dict[str, list[tuple[str, str]]] = {}
    for name, field, key in c.rule.requirements():
        rule_reqs.setdefault(name, []).append((field, key))

    for name, sel in named.items():
        candidates = _candidates(sel, catalog)
        if not candidates:
            out.append(LintFinding("CL010", LintLevel.ERROR, c.id,
                                   f"{name} ({sel.describe()}) matches no event type the adapter emits"))
            continue

        # The selector's own filters: events without the key simply do not
        # match, so only "no candidate ever has it" is a problem.
        own = [("attributes", key) for key, _ in sel.attributes]
        if sel.where is not None:
            own += sorted(sel.where.references())
        for field, key in own:
            if not any(_has(s, field, key) for s in candidates):
                out.append(LintFinding("CL011", LintLevel.ERROR, c.id,
                                       f"{name} filters on {_label(field)} '{key}', which none of "
                                       f"{sorted(s.event_type for s in candidates)} carry"))
        # Events lacking an attribute the selector filters on cannot match it.
        candidates = [s for s in candidates if all(_has(s, "attributes", k) for k, _ in sel.attributes)]

        for field, key in rule_reqs.get(name, []):
            present = [s for s in candidates if _has(s, field, key)]
            if not present:
                out.append(LintFinding("CL011", LintLevel.ERROR, c.id,
                                       f"rule needs {_label(field)} '{key}' on {name}, but none of "
                                       f"{sorted(s.event_type for s in candidates)} carry it"))
            elif len(present) < len(candidates):
                missing = sorted(s.event_type for s in candidates if s not in present)
                out.append(LintFinding("CL012", LintLevel.WARNING, c.id,
                                       f"{_label(field)} '{key}' is missing on {missing} matched by {name}; "
                                       f"those events will be unresolved"))
    return out


def lint_contracts(
    contracts: Sequence[BoundaryContract],
    *,
    catalog: Sequence[EventSpec] | None = None,
    subsystems: Iterable[str] | None = None,
    timeout_s: float | None = None,
) -> list[LintFinding]:
    declared = set(subsystems) if subsystems is not None else None
    findings: list[LintFinding] = []
    for c in contracts:
        if declared is not None:
            for end in (c.source, c.destination):
                if end not in declared:
                    findings.append(LintFinding("CL001", LintLevel.ERROR, c.id,
                                                f"'{end}' is not a subsystem the adapter declares"))
        if c.severity is Criticality.CRITICAL and c.on_fail is FailurePolicy.WARN:
            findings.append(LintFinding("CL002", LintLevel.WARNING, c.id,
                                        "severity is critical but on_fail is warn, so a failure cannot fail the run"))
        findings += _lint_rule(c, timeout_s)
        if catalog is not None:
            findings += _lint_catalog(c, catalog)
    return sorted(findings, key=lambda f: (_LEVEL_ORDER[f.level], f.contract_id, f.code, f.message))
