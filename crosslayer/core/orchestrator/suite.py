"""Experiment suites: several experiments, expected verdicts, dependency order.

::

    schema_version: 1
    name: reference
    experiments:
      - id: baseline
        path: baseline.yaml              # relative to this file
      - id: silent_drop
        path: fault_silent_drop.yaml
        expect: fail                     # a fault experiment *should* fail
        depends_on: [baseline]

An experiment runs only if every experiment it depends on **met its
expectation**; otherwise it is *blocked*. A fault experiment's FAIL means
nothing if the system also fails without the fault, so faults depend on the
baseline. Order is a topological sort (Kahn's algorithm) with file order as
the tie-breaker, so runs are deterministic. Cycles are configuration errors
and name the cycle.
"""

from __future__ import annotations

import json
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

from crosslayer.core.contracts.loader import read_document
from crosslayer.core.errors import ConfigError
from crosslayer.core.orchestrator.experiment import ExperimentConfig, load_experiment
from crosslayer.core.orchestrator.runner import ExperimentRunner
from crosslayer.core.verdict import Verdict

SCHEMA_VERSION = 1
_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")
_DOC_KEYS = {"schema_version", "name", "description", "experiments"}
_ENTRY_KEYS = {"id", "path", "expect", "depends_on"}


@dataclass(frozen=True)
class SuiteEntry:
    id: str
    experiment: ExperimentConfig
    expect: Verdict = Verdict.PASS
    depends_on: tuple[str, ...] = ()


@dataclass(frozen=True)
class Suite:
    name: str
    entries: tuple[SuiteEntry, ...]
    description: str = ""
    source_path: Path | None = None


@dataclass
class SuiteEntryResult:
    entry: SuiteEntry
    status: str
    """``met``, ``unmet``, or ``blocked``."""
    verdict: Verdict | None = None
    run_id: str | None = None
    bundle_dir: Path | None = None
    blocked_by: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.entry.id,
            "experiment": self.entry.experiment.name,
            "expect": str(self.entry.expect),
            "verdict": str(self.verdict) if self.verdict else None,
            "status": self.status,
            "run_id": self.run_id,
            "bundle": self.bundle_dir.name if self.bundle_dir else None,
            "depends_on": list(self.entry.depends_on),
            "blocked_by": self.blocked_by,
        }


@dataclass
class SuiteResult:
    suite: Suite
    suite_dir: Path
    results: list[SuiteEntryResult]

    @property
    def passed(self) -> bool:
        return all(r.status == "met" for r in self.results)


# -- loading -----------------------------------------------------------------------


def _find_cycle(nodes: set[str], deps: Mapping[str, tuple[str, ...]]) -> list[str]:
    """Return one dependency cycle among ``nodes`` as a path, e.g. [a, b, a]."""
    state: dict[str, int] = {}  # 1 = on the current path, 2 = finished
    path: list[str] = []

    def visit(node: str) -> list[str] | None:
        state[node] = 1
        path.append(node)
        for dep in deps[node]:
            if dep not in nodes:
                continue
            if state.get(dep) == 1:
                return path[path.index(dep):] + [dep]
            if dep not in state:
                found = visit(dep)
                if found:
                    return found
        path.pop()
        state[node] = 2
        return None

    for node in sorted(nodes):
        if node not in state:
            found = visit(node)
            if found:
                return found
    return sorted(nodes)  # unreachable when a cycle exists; kept as a safe fallback


def execution_order(entries: tuple[SuiteEntry, ...]) -> list[SuiteEntry]:
    """Kahn's algorithm. Ties are broken by position in the file."""
    position = {e.id: i for i, e in enumerate(entries)}
    by_id = {e.id: e for e in entries}
    remaining = {e.id: set(e.depends_on) for e in entries}
    ready = sorted((i for i, deps in remaining.items() if not deps), key=position.__getitem__)
    order: list[SuiteEntry] = []
    while ready:
        current = ready.pop(0)
        order.append(by_id[current])
        del remaining[current]
        newly_ready = []
        for other, deps in remaining.items():
            if current in deps:
                deps.discard(current)
                if not deps:
                    newly_ready.append(other)
        ready = sorted(ready + newly_ready, key=position.__getitem__)
    if remaining:
        cycle = _find_cycle(set(remaining), {e.id: e.depends_on for e in entries})
        raise ConfigError(f"dependency cycle: {' -> '.join(cycle)}")
    return order


def load_suite(path: Path) -> Suite:
    path = path.resolve()
    source = str(path)
    doc = read_document(path)
    if not isinstance(doc, Mapping):
        raise ConfigError("suite must be a mapping", location=source)
    unknown = doc.keys() - _DOC_KEYS
    if unknown:
        raise ConfigError(f"unknown keys {sorted(unknown)}", location=source)
    if doc.get("schema_version") != SCHEMA_VERSION:
        raise ConfigError(f"schema_version must be {SCHEMA_VERSION}", location=source)
    name = doc.get("name")
    if not isinstance(name, str) or not name.strip():
        raise ConfigError("'name' is required", location=source)
    raw_entries = doc.get("experiments")
    if not isinstance(raw_entries, list) or not raw_entries:
        raise ConfigError("'experiments' must be a non-empty list", location=source)

    entries: list[SuiteEntry] = []
    for i, raw in enumerate(raw_entries):
        where = f"{source}: experiments[{i}]"
        if not isinstance(raw, Mapping):
            raise ConfigError("entry must be a mapping", location=where)
        unknown = raw.keys() - _ENTRY_KEYS
        if unknown:
            raise ConfigError(f"unknown keys {sorted(unknown)}", location=where)
        entry_id = raw.get("id")
        if not isinstance(entry_id, str) or not _ID_RE.match(entry_id):
            raise ConfigError("'id' must be lowercase letters, digits, '_' or '-'", location=where)
        if not isinstance(raw.get("path"), str):
            raise ConfigError("'path' is required", location=where)
        try:
            expect = Verdict(raw.get("expect", "pass"))
        except ValueError:
            raise ConfigError(f"expect must be one of {[v.value for v in Verdict]}", location=where) from None
        depends = raw.get("depends_on") or []
        if isinstance(depends, str):
            depends = [depends]
        if not isinstance(depends, list) or not all(isinstance(d, str) for d in depends):
            raise ConfigError("'depends_on' must be a list of ids", location=where)
        entries.append(SuiteEntry(entry_id, load_experiment(path.parent / raw["path"]), expect, tuple(depends)))

    ids = [e.id for e in entries]
    duplicates = sorted({i for i in ids if ids.count(i) > 1})
    if duplicates:
        raise ConfigError(f"duplicate experiment ids {duplicates}", location=source)
    for e in entries:
        if e.id in e.depends_on:
            raise ConfigError(f"'{e.id}' depends on itself", location=source)
        missing = sorted(set(e.depends_on) - set(ids))
        if missing:
            raise ConfigError(f"'{e.id}' depends on unknown experiments {missing}", location=source)

    suite = Suite(name.strip(), tuple(entries), str(doc.get("description") or "").strip(), path)
    execution_order(suite.entries)  # reject cycles at load time
    return suite


# -- running -----------------------------------------------------------------------


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-") or "suite"


class SuiteRunner:
    def __init__(
        self,
        output_root: Path,
        *,
        runner_factory: Callable[[Path], ExperimentRunner] = ExperimentRunner,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        self.output_root = output_root
        self._runner_factory = runner_factory
        self._clock = clock

    def _suite_dir(self, suite: Suite) -> Path:
        base = f"suite-{_slug(suite.name)}-{self._clock():%Y%m%dT%H%M%SZ}"
        candidate, n = self.output_root / base, 1
        while candidate.exists():
            candidate = self.output_root / f"{base}-{n}"
            n += 1
        candidate.mkdir(parents=True)
        return candidate

    def run(self, suite: Suite, *, on_result: Callable[[SuiteEntryResult], None] | None = None) -> SuiteResult:
        suite_dir = self._suite_dir(suite)
        runner = self._runner_factory(suite_dir / "runs")
        status: dict[str, str] = {}
        results: list[SuiteEntryResult] = []
        for entry in execution_order(suite.entries):
            blockers = [d for d in entry.depends_on if status.get(d) != "met"]
            if blockers:
                result = SuiteEntryResult(entry, "blocked", blocked_by=blockers)
            else:
                run = runner.run(entry.experiment)
                met = run.verdict is entry.expect
                result = SuiteEntryResult(entry, "met" if met else "unmet", run.verdict, run.run_id, run.bundle_dir)
            status[entry.id] = result.status
            results.append(result)
            if on_result:
                on_result(result)

        outcome = SuiteResult(suite, suite_dir, results)
        _write_outputs(outcome)
        return outcome


def _write_outputs(outcome: SuiteResult) -> None:
    d = outcome.suite_dir
    summary = {
        "suite": outcome.suite.name,
        "source": str(outcome.suite.source_path) if outcome.suite.source_path else None,
        "passed": outcome.passed,
        "counts": {s: sum(r.status == s for r in outcome.results) for s in ("met", "unmet", "blocked")},
        "experiments": [r.to_dict() for r in outcome.results],
    }
    (d / "summary.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8", newline="\n")

    lines = [f"# Suite `{outcome.suite.name}`: {'PASSED' if outcome.passed else 'FAILED'}\n",
             "| Experiment | Expected | Verdict | Status | Bundle |", "|---|---|---|---|---|"]
    for r in outcome.results:
        bundle = f"`runs/{r.bundle_dir.name}`" if r.bundle_dir else f"blocked by {', '.join(r.blocked_by)}"
        verdict = r.verdict.upper() if r.verdict else "—"
        lines.append(f"| `{r.entry.id}` | {r.entry.expect.upper()} | {verdict} | {r.status} | {bundle} |")
    (d / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")

    suite = ET.Element("testsuite", name=outcome.suite.name, tests=str(len(outcome.results)))
    failures = skipped = 0
    for r in outcome.results:
        case = ET.SubElement(suite, "testcase", classname="crosslayer.suite", name=r.entry.id)
        if r.status == "unmet":
            failures += 1
            ET.SubElement(case, "failure", message=f"expected {r.entry.expect.upper()}, got {r.verdict.upper()}")
        elif r.status == "blocked":
            skipped += 1
            ET.SubElement(case, "skipped", message=f"blocked by {', '.join(r.blocked_by)}")
    suite.set("failures", str(failures))
    suite.set("skipped", str(skipped))
    suite.set("errors", "0")
    ET.indent(suite)
    (d / "junit.xml").write_text('<?xml version="1.0" encoding="utf-8"?>\n'
                                 + ET.tostring(suite, encoding="unicode") + "\n",
                                 encoding="utf-8", newline="\n")
