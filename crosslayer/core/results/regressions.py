"""Regression analysis over indexed runs.

Runs of one experiment and seed form a series, ordered by start time. The
latest run is compared with the runs before it. A verdict is traced back to the
first run where it appeared (the oldest run of the unbroken streak that ends at
the latest run); the baseline is the run just before that streak, so a failure
that has lasted several runs is still a regression until a run passes again. Only index data is read; fault
labels are never consulted.

Findings are of two classes. ``regression`` findings make the command fail: the
run verdict or a contract's verdict got worse (to ``fail`` or ``error``). The
rest are ``change`` findings: recoveries, other verdict changes, stage timings
slower than the baseline by more than the tolerance, and first-violation times
that moved by more than the tolerance. Timings are wall-clock seconds, so a
slowdown needs both the relative tolerance and an absolute floor to be reported.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from typing import Any, Callable, Sequence

from crosslayer.core.errors import ConfigError

BAD = frozenset({"fail", "error"})
DEFAULT_TIMING_TOLERANCE = 0.5
DEFAULT_MIN_SECONDS = 0.05
DEFAULT_VIOLATION_TOLERANCE = 0.1


@dataclass(frozen=True)
class Tolerances:
    timing: float = DEFAULT_TIMING_TOLERANCE
    min_seconds: float = DEFAULT_MIN_SECONDS
    violation: float = DEFAULT_VIOLATION_TOLERANCE

    def __post_init__(self) -> None:
        for name, value in (("timing tolerance", self.timing), ("minimum seconds", self.min_seconds),
                            ("violation tolerance", self.violation)):
            if value < 0:
                raise ConfigError(f"{name} must not be negative, got {value}")


@dataclass
class Finding:
    kind: str           # verdict | contract | timing | first_violation
    severity: str       # regression | change
    subject: str        # the contract id, stage name, or "run"
    summary: str
    before: Any
    after: Any
    first_seen_run: str
    first_seen_at: str | None
    runs_since: int     # runs in the streak, latest included

    def to_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)


@dataclass
class SeriesReport:
    experiment: str
    seed: int | None
    run_count: int
    latest_run: str
    baseline_run: str | None
    config_changed: bool = False
    findings: list[Finding] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        doc = dict(self.__dict__)
        doc["findings"] = [f.to_dict() for f in self.findings]
        return doc


def _streak(series: Sequence[dict[str, Any]], holds: Callable[[dict[str, Any]], bool]) -> list[dict[str, Any]]:
    """The unbroken runs, ending at the latest, for which ``holds`` is true (oldest first)."""
    streak: list[dict[str, Any]] = []
    for run in reversed(series):
        if not holds(run):
            break
        streak.append(run)
    return streak[::-1]


def _finding(kind: str, severity: str, subject: str, summary: str, before: Any, after: Any,
             streak: list[dict[str, Any]]) -> Finding:
    first = streak[0]
    return Finding(kind, severity, subject, summary, before, after, first["run_id"], first.get("started_at"), len(streak))


def _results(run: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {c["contract_id"]: c for c in run["contracts"]}


def _severity(before: str, after: str) -> str:
    return "regression" if after in BAD and before not in BAD else "change"


def _verdict_findings(series: Sequence[dict[str, Any]], latest: dict[str, Any]) -> list[Finding]:
    streak = _streak(series, lambda r: r["verdict"] == latest["verdict"])
    if len(streak) == len(series):
        return []
    before = series[len(series) - len(streak) - 1]["verdict"]
    return [_finding("verdict", _severity(before, latest["verdict"]), "run",
                     f"run verdict {before} -> {latest['verdict']}", before, latest["verdict"], streak)]


def _contract_findings(series: Sequence[dict[str, Any]], latest: dict[str, Any], tol: Tolerances) -> list[Finding]:
    """Each contract's latest verdict, traced back to the run where that verdict began.

    The baseline is the run just before that streak, so a failure that has
    lasted several runs is still reported, against the last run that was
    different. A contract that has had its verdict in every run is unchanged.
    """
    findings: list[Finding] = []
    for contract_id, now in sorted(_results(latest).items()):
        verdict = now["verdict"]
        streak = _streak(series, lambda r: _results(r).get(contract_id, {}).get("verdict") == verdict)
        if len(streak) < len(series):
            earlier = _results(series[len(series) - len(streak) - 1]).get(contract_id)
            if earlier is None:
                if verdict in BAD:
                    findings.append(_finding("contract", "regression", contract_id,
                                             f"new contract {contract_id} is {verdict}", None, verdict, streak))
                continue
            severity = _severity(earlier["verdict"], verdict)
            label = "newly failing" if severity == "regression" else "changed"
            findings.append(_finding("contract", severity, contract_id,
                                     f"{contract_id} {label}: {earlier['verdict']} -> {verdict}",
                                     earlier["verdict"], verdict, streak))
        elif len(series) > 1 and verdict == "fail":
            was = _results(series[-2]).get(contract_id)
            old, new = (was or {}).get("first_violation_ns"), now["first_violation_ns"]
            if old and new is not None and abs(new - old) / old > tol.violation:
                near = _streak(series, lambda r: _fails_near(_results(r).get(contract_id), new, tol.violation))
                findings.append(_finding(
                    "first_violation", "change", contract_id,
                    f"{contract_id} first violation moved {old / 1e6:.3f} ms -> {new / 1e6:.3f} ms "
                    f"({'earlier' if new < old else 'later'}, {abs(new - old) / old:.0%})", old, new, near))
    return findings


def _fails_near(result: dict[str, Any] | None, value: int, tolerance: float) -> bool:
    if not result or result["verdict"] != "fail" or result["first_violation_ns"] is None:
        return False
    return abs(result["first_violation_ns"] - value) <= tolerance * max(value, 1)


def _timing_findings(series: Sequence[dict[str, Any]], latest: dict[str, Any], tol: Tolerances) -> list[Finding]:
    earlier = [r for r in series[:-1] if r.get("timings")]
    findings: list[Finding] = []
    for stage, seconds in sorted((latest.get("timings") or {}).items()):
        samples = [r["timings"][stage] for r in earlier if stage in r["timings"]]
        if not samples:
            continue
        baseline = statistics.median(samples)
        limit = max(baseline * (1 + tol.timing), baseline + tol.min_seconds)
        if seconds <= limit:
            continue
        streak = _streak(series, lambda r: (r.get("timings") or {}).get(stage, 0) > limit)
        growth = f"+{(seconds - baseline) / baseline:.0%}" if baseline else "from 0 s"
        findings.append(_finding(
            "timing", "change", stage,
            f"stage {stage} took {seconds:.3f} s, baseline {baseline:.3f} s "
            f"(median of {len(samples)} earlier run(s), {growth})", round(baseline, 6), seconds, streak))
    return findings


def analyze(runs: Sequence[dict[str, Any]], tolerances: Tolerances | None = None) -> list[SeriesReport]:
    """One report per (experiment, seed) series, sorted by experiment then seed. ``runs`` is any order."""
    tol = tolerances or Tolerances()
    groups: dict[tuple[str, int | None], list[dict[str, Any]]] = {}
    for run in runs:
        groups.setdefault((run["experiment"], run["seed"]), []).append(run)
    reports: list[SeriesReport] = []
    for (experiment, seed), group in sorted(groups.items(), key=lambda kv: (kv[0][0], kv[0][1] is None, kv[0][1] or 0)):
        series = sorted(group, key=lambda r: (r["started_at"] or "", r["run_id"]))
        latest = series[-1]
        report = SeriesReport(experiment, seed, len(series), latest["run_id"], series[-2]["run_id"] if len(series) > 1 else None)
        if len(series) > 1:
            report.config_changed = series[-2]["config_hash"] != latest["config_hash"]
            report.findings = (_verdict_findings(series, latest) + _contract_findings(series, latest, tol)
                               + _timing_findings(series, latest, tol))
        reports.append(report)
    return reports


def has_regression(reports: Sequence[SeriesReport]) -> bool:
    return any(f.severity == "regression" for r in reports for f in r.findings)


def render(reports: Sequence[SeriesReport]) -> str:
    lines: list[str] = []
    for r in reports:
        seed = f" seed {r.seed}" if r.seed is not None else ""
        head = f"{r.experiment}{seed}: {r.run_count} run(s)"
        if r.baseline_run is None:
            lines.append(f"{head}, no earlier run to compare with")
            continue
        note = "  [config changed since the previous run]" if r.config_changed else ""
        lines.append(f"{head}, latest {r.latest_run} vs {r.baseline_run}{note}" + ("" if r.findings else ": no change"))
        for f in r.findings:
            since = f"first seen {f.first_seen_at or '-'} in {f.first_seen_run}"
            if f.runs_since > 1:
                since += f" ({f.runs_since} runs)"
            lines.append(f"  {'REGRESSION' if f.severity == 'regression' else 'change    '} {f.summary}; {since}")
    regressions = sum(f.severity == "regression" for r in reports for f in r.findings)
    changes = sum(f.severity == "change" for r in reports for f in r.findings)
    lines.append(f"{len(reports)} series, {regressions} regression(s), {changes} other change(s)")
    return "\n".join(lines)
