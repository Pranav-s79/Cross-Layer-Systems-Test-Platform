"""Boundary contracts and the results they produce.

A boundary contract states what must hold at the interface between two
components (``source`` -> ``destination``), who owns it, how strictly to treat
a violation, and what evidence must exist before a PASS can be trusted.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, ClassVar, Mapping, Sequence

from crosslayer.core.contracts.selectors import Selector
from crosslayer.core.telemetry.events import NormalizedEvent
from crosslayer.core.verdict import Verdict

MAX_FINDINGS_PER_CONTRACT = 50
"""Findings beyond this are counted but not stored, so one systemic failure
cannot produce a multi-megabyte violations.json."""


class Criticality(StrEnum):
    CRITICAL = "critical"
    MAJOR = "major"
    MINOR = "minor"


class FailurePolicy(StrEnum):
    FAIL_RUN = "fail_run"
    """A FAIL here fails the whole run."""
    WARN = "warn"
    """A FAIL here is reported as a warning; the run verdict ignores it."""


class FindingKind(StrEnum):
    VIOLATION = "violation"
    """Concrete evidence that the contract was broken."""
    UNRESOLVED = "unresolved"
    """Could not decide: data missing or observation ended too early."""


@dataclass(frozen=True)
class EvidenceRef:
    """A pointer from a finding back to the event (and raw log line) behind it."""

    event_id: str
    event_type: str
    subsystem: str
    timestamp_ns: int
    raw_artifact_ref: str | None

    @classmethod
    def of(cls, event: NormalizedEvent) -> "EvidenceRef":
        return cls(
            event_id=event.event_id,
            event_type=event.event_type,
            subsystem=event.subsystem,
            timestamp_ns=event.timestamp_ns,
            raw_artifact_ref=event.raw_artifact_ref,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "event_type": self.event_type,
            "subsystem": self.subsystem,
            "timestamp_ns": self.timestamp_ns,
            "raw_artifact_ref": self.raw_artifact_ref,
        }


@dataclass(frozen=True)
class Finding:
    kind: FindingKind
    message: str
    evidence: tuple[EvidenceRef, ...] = ()
    timestamp_ns: int | None = None
    """When the violation became observable. Used later to find the earliest
    violation across contracts."""
    details: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": str(self.kind),
            "message": self.message,
            "timestamp_ns": self.timestamp_ns,
            "evidence": [ref.to_dict() for ref in self.evidence],
            "details": dict(self.details),
        }


class FindingCollector:
    """Accumulates findings, storing at most ``limit`` of each kind while
    still counting all of them."""

    def __init__(self, limit: int = MAX_FINDINGS_PER_CONTRACT) -> None:
        self.limit = limit
        self.findings: list[Finding] = []
        self.counts: dict[FindingKind, int] = {kind: 0 for kind in FindingKind}

    def add(self, finding: Finding) -> None:
        self.counts[finding.kind] += 1
        if self.counts[finding.kind] <= self.limit:
            self.findings.append(finding)

    def _record(
        self,
        kind: FindingKind,
        message: str,
        events: Sequence[NormalizedEvent],
        at_ns: int | None,
        details: dict[str, Any],
    ) -> None:
        if at_ns is None and events:
            at_ns = events[-1].timestamp_ns
        self.add(
            Finding(
                kind=kind,
                message=message,
                evidence=tuple(EvidenceRef.of(e) for e in events),
                timestamp_ns=at_ns,
                details=details,
            )
        )

    def violation(
        self,
        message: str,
        events: Sequence[NormalizedEvent],
        *,
        at_ns: int | None = None,
        **details: Any,
    ) -> None:
        """Record a violation. ``at_ns`` is when it became observable
        (e.g. a missed deadline), defaulting to the last evidence event."""
        self._record(FindingKind.VIOLATION, message, events, at_ns, details)

    def unresolved(
        self,
        message: str,
        events: Sequence[NormalizedEvent],
        *,
        at_ns: int | None = None,
        **details: Any,
    ) -> None:
        self._record(FindingKind.UNRESOLVED, message, events, at_ns, details)

    @property
    def violations(self) -> int:
        return self.counts[FindingKind.VIOLATION]

    @property
    def unresolved_count(self) -> int:
        return self.counts[FindingKind.UNRESOLVED]

    def verdict(self) -> Verdict:
        """Any violation -> FAIL; otherwise anything unresolved -> INCONCLUSIVE."""
        if self.violations:
            return Verdict.FAIL
        if self.unresolved_count:
            return Verdict.INCONCLUSIVE
        return Verdict.PASS


@dataclass
class RuleOutcome:
    verdict: Verdict
    summary: str
    findings: list[Finding] = field(default_factory=list)
    stats: dict[str, Any] = field(default_factory=dict)


class Rule(ABC):
    """A verification rule. Subclasses register a ``kind`` used in YAML."""

    kind: ClassVar[str]

    @classmethod
    @abstractmethod
    def parse(cls, raw: Mapping[str, Any], location: str) -> "Rule":
        """Build the rule from its YAML mapping (without the ``kind`` key)."""

    @abstractmethod
    def evaluate(self, events: Sequence[NormalizedEvent]) -> RuleOutcome:
        """Evaluate against time-ordered events."""


@dataclass(frozen=True)
class BoundaryContract:
    id: str
    title: str
    owner: str
    source: str
    destination: str
    rule: Rule
    description: str = ""
    severity: Criticality = Criticality.MAJOR
    on_fail: FailurePolicy = FailurePolicy.FAIL_RUN
    precondition: Selector | None = None
    evidence: tuple[Selector, ...] = ()
    tags: tuple[str, ...] = ()
    origin: str = ""
    """Where the contract was defined (file path or adapter name)."""
    definition: Mapping[str, Any] = field(default_factory=dict)
    """The original mapping, kept verbatim for contracts.json and replay."""


@dataclass
class ContractResult:
    contract: BoundaryContract
    verdict: Verdict
    summary: str
    findings: list[Finding] = field(default_factory=list)
    stats: dict[str, Any] = field(default_factory=dict)
    missing_evidence: list[dict[str, Any]] = field(default_factory=list)
    rule_verdict: Verdict | None = None
    """What the rule alone concluded, before evidence gating. Differs from
    ``verdict`` when required evidence was missing."""
    error: str | None = None

    @property
    def first_violation_ns(self) -> int | None:
        times = [
            f.timestamp_ns
            for f in self.findings
            if f.kind is FindingKind.VIOLATION and f.timestamp_ns is not None
        ]
        return min(times) if times else None

    def to_dict(self) -> dict[str, Any]:
        c = self.contract
        return {
            "contract_id": c.id,
            "title": c.title,
            "owner": c.owner,
            "source": c.source,
            "destination": c.destination,
            "severity": str(c.severity),
            "on_fail": str(c.on_fail),
            "rule_kind": c.rule.kind,
            "verdict": str(self.verdict),
            "rule_verdict": str(self.rule_verdict) if self.rule_verdict else None,
            "summary": self.summary,
            "first_violation_ns": self.first_violation_ns,
            "missing_evidence": self.missing_evidence,
            "stats": self.stats,
            "error": self.error,
            "findings": [f.to_dict() for f in self.findings],
        }
