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

      - id: random_programs
        path: random.yaml
        seeds: {count: 20}               # or a list: [1, 2, 3]

An experiment runs only if every experiment it depends on **met its
expectation**; otherwise it is *blocked*. A fault experiment's FAIL means
nothing if the system also fails without the fault, so faults depend on the
baseline. Order is a topological sort (Kahn's algorithm) with file order as
the tie-breaker, so runs are deterministic. Cycles are configuration errors
and name the cycle.

``seeds`` turns an entry into a *sweep*: one run per seed, with ids
``<id>@<seed>``, each of which must meet the expectation. A dependency on a
sweep means every seed of it, except between two sweeps over the same seeds,
where seed ``n`` depends only on seed ``n``. Runs whose normalization stats
carry ``coverage`` bins (``{group: {bin: hits}}``) are summed per sweep, and
the suite report lists bins no seed hit.
"""

from __future__ import annotations

import json
import re
import xml.etree.ElementTree as ET
from concurrent.futures import FIRST_COMPLETED, Executor, Future, ProcessPoolExecutor, ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

from crosslayer.core.artifacts.report import coverage_bins
from crosslayer.core.contracts.loader import read_document
from crosslayer.core.errors import ConfigError
from crosslayer.core.orchestrator.experiment import ExperimentConfig, load_experiment
from crosslayer.core.orchestrator.runner import ExperimentRunner
from crosslayer.core.verdict import Verdict

SCHEMA_VERSION = 1
_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")
_DOC_KEYS = {"schema_version", "name", "description", "experiments"}
_ENTRY_KEYS = {"id", "path", "expect", "depends_on", "seeds"}
MAX_SEEDS = 1000
FAIL_FAST = "fail-fast"
NOT_SELECTED = "not selected"


@dataclass(frozen=True)
class SuiteEntry:
    id: str
    experiment: ExperimentConfig
    expect: Verdict = Verdict.PASS
    depends_on: tuple[str, ...] = ()
    sweep: str | None = None
    """The declared id when this entry is one seed of a sweep."""


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
    """``met``, ``unmet``, ``blocked``, or ``skipped`` (``skip_reason`` says why)."""
    verdict: Verdict | None = None
    run_id: str | None = None
    bundle_dir: Path | None = None
    blocked_by: list[str] = field(default_factory=list)
    coverage: dict[str, dict[str, int]] | None = None
    """Coverage bins from the run's normalization stats, if it reported any."""
    skip_reason: str | None = None
    """``fail-fast`` (after a stop) or ``not selected`` (outside ``only``)."""

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.entry.id,
            "sweep": self.entry.sweep,
            "seed": self.entry.experiment.seed,
            "experiment": self.entry.experiment.name,
            "expect": str(self.entry.expect),
            "verdict": str(self.verdict) if self.verdict else None,
            "status": self.status,
            "run_id": self.run_id,
            "bundle": self.bundle_dir.name if self.bundle_dir else None,
            "depends_on": list(self.entry.depends_on),
            "blocked_by": self.blocked_by,
            "skip_reason": self.skip_reason,
        }


@dataclass
class SuiteResult:
    suite: Suite
    suite_dir: Path
    results: list[SuiteEntryResult]
    only: tuple[str, ...] | None = None
    """The ``only`` selection this run used, as given."""

    @property
    def passed(self) -> bool:
        """Every selected experiment met its expectation (unselected ones do not count)."""
        return all(r.status == "met" or r.skip_reason == NOT_SELECTED for r in self.results)


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

    declared: list[tuple[str, ExperimentConfig, Verdict, tuple[str, ...], tuple[int, ...] | None]] = []
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
        seeds = _parse_seeds(raw["seeds"], f"{where}.seeds") if "seeds" in raw else None
        declared.append((entry_id, load_experiment(path.parent / raw["path"]), expect, tuple(depends), seeds))

    ids = [d[0] for d in declared]
    duplicates = sorted({i for i in ids if ids.count(i) > 1})
    if duplicates:
        raise ConfigError(f"duplicate experiment ids {duplicates}", location=source)
    for entry_id, _, _, depends, _ in declared:
        if entry_id in depends:
            raise ConfigError(f"'{entry_id}' depends on itself", location=source)
        missing = sorted(set(depends) - set(ids))
        if missing:
            raise ConfigError(f"'{entry_id}' depends on unknown experiments {missing}", location=source)

    entries = _expand_sweeps(declared)
    suite = Suite(name.strip(), tuple(entries), str(doc.get("description") or "").strip(), path)
    execution_order(suite.entries)  # reject cycles at load time
    return suite


def _parse_seeds(raw: Any, where: str) -> tuple[int, ...]:
    """``[1, 2, 3]`` or ``{count: N, start: S}`` (start defaults to 1)."""
    if isinstance(raw, Mapping):
        unknown = raw.keys() - {"count", "start"}
        if unknown:
            raise ConfigError(f"unknown keys {sorted(unknown)} (allowed: count, start)", location=where)
        count, start = raw.get("count"), raw.get("start", 1)
        for key, value in (("count", count), ("start", start)):
            if isinstance(value, bool) or not isinstance(value, int) or value < (1 if key == "count" else 0):
                raise ConfigError(f"'{key}' must be a {'positive' if key == 'count' else 'non-negative'} integer",
                                  location=where)
        assert isinstance(count, int) and isinstance(start, int)
        seeds = tuple(range(start, start + count))
    elif isinstance(raw, list) and raw and all(isinstance(s, int) and not isinstance(s, bool) and s >= 0 for s in raw):
        seeds = tuple(raw)
        if len(set(seeds)) != len(seeds):
            raise ConfigError("seeds must be distinct", location=where)
    else:
        raise ConfigError("must be a non-empty list of non-negative integers or {count: N, start: S}",
                          location=where)
    if len(seeds) > MAX_SEEDS:
        raise ConfigError(f"at most {MAX_SEEDS} seeds per sweep", location=where)
    return seeds


def _expand_sweeps(
    declared: list[tuple[str, ExperimentConfig, Verdict, tuple[str, ...], tuple[int, ...] | None]],
) -> list[SuiteEntry]:
    """One entry per (entry, seed); dependencies on a sweep fan out or pair by seed."""
    seeds_of = {d[0]: d[4] for d in declared}

    def deps_for(depends: tuple[str, ...], own_seeds: tuple[int, ...] | None, seed: int | None) -> tuple[str, ...]:
        out: list[str] = []
        for dep in depends:
            dep_seeds = seeds_of[dep]
            if dep_seeds is None:
                out.append(dep)
            elif seed is not None and dep_seeds == own_seeds:
                out.append(f"{dep}@{seed}")
            else:
                out.extend(f"{dep}@{s}" for s in dep_seeds)
        return tuple(out)

    entries: list[SuiteEntry] = []
    for entry_id, experiment, expect, depends, seeds in declared:
        if seeds is None:
            entries.append(SuiteEntry(entry_id, experiment, expect, deps_for(depends, None, None)))
            continue
        for seed in seeds:
            entries.append(SuiteEntry(f"{entry_id}@{seed}", experiment.with_seed(seed), expect,
                                      deps_for(depends, seeds, seed), sweep=entry_id))
    return entries


def select_entries(suite: Suite, only: Iterable[str]) -> set[str]:
    """Ids of the entries ``only`` names plus everything they depend on, transitively.

    A name is an entry id, a sweep's declared id (all of its seeds), or one
    seed as ``<id>@<seed>``. Unknown names are a ``ConfigError`` that lists
    the valid ones.
    """
    by_id = {e.id: e for e in suite.entries}
    names = list(only)
    if not names:
        raise ConfigError("'only' must name at least one experiment")
    chosen: set[str] = set()
    for name in names:
        matched = [e.id for e in suite.entries if e.id == name or e.sweep == name]
        if not matched:
            valid = sorted({e.sweep or e.id for e in suite.entries} | set(by_id))
            raise ConfigError(f"unknown experiment '{name}' in only (valid: {', '.join(valid)})")
        chosen.update(matched)
    stack = list(chosen)
    while stack:
        for dep in by_id[stack.pop()].depends_on:
            if dep not in chosen:
                chosen.add(dep)
                stack.append(dep)
    return chosen


def sweep_summaries(results: list[SuiteEntryResult]) -> list[dict[str, Any]]:
    """Per sweep: seed counts by status, and coverage bins summed over the seeds that ran."""
    sweeps: dict[str, list[SuiteEntryResult]] = {}
    for r in results:
        if r.entry.sweep is not None:
            sweeps.setdefault(r.entry.sweep, []).append(r)
    summaries = []
    for sweep_id, runs in sweeps.items():
        coverage: dict[str, dict[str, int]] = {}
        for r in runs:
            for group, bins in (r.coverage or {}).items():
                total = coverage.setdefault(group, {})
                for bin_name, hits in bins.items():
                    total[bin_name] = total.get(bin_name, 0) + hits
        summaries.append({
            "sweep": sweep_id,
            "seeds": [r.entry.experiment.seed for r in runs],
            "counts": {s: sum(r.status == s for r in runs) for s in ("met", "unmet", "blocked", "skipped")},
            "coverage": coverage,
            "never_hit": {g: sorted(b for b, n in bins.items() if n == 0) for g, bins in coverage.items()
                          if any(n == 0 for n in bins.values())},
        })
    return summaries


def _coverage_of(bundle_dir: Path | None) -> dict[str, dict[str, int]] | None:
    """``stats.coverage`` from a bundle's normalization.json, when it has the bin shape."""
    if bundle_dir is None or not (bundle_dir / "normalization.json").is_file():
        return None
    return coverage_bins(json.loads((bundle_dir / "normalization.json").read_text(encoding="utf-8")).get("stats"))


# -- running -----------------------------------------------------------------------


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-") or "suite"


_WORKER_RUNNERS: dict[Path, ExperimentRunner] = {}


def _run_in_worker(runs_dir: Path, experiment: ExperimentConfig) -> tuple[Verdict, str, Path]:
    """Run one experiment in a pool process, reusing that process's runner."""
    runner = _WORKER_RUNNERS.get(runs_dir)
    if runner is None:
        runner = _WORKER_RUNNERS[runs_dir] = ExperimentRunner(runs_dir)
    run = runner.run(experiment)
    return run.verdict, run.run_id, run.bundle_dir


class SuiteRunner:
    def __init__(
        self,
        output_root: Path,
        *,
        runner_factory: Callable[[Path], ExperimentRunner] = ExperimentRunner,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
        jobs: int = 1,
    ) -> None:
        """``jobs`` > 1 runs experiments whose dependencies are done in parallel:
        in worker processes with the default runner, in threads with a custom
        ``runner_factory`` (which need not be picklable)."""
        if isinstance(jobs, bool) or not isinstance(jobs, int) or jobs < 1:
            raise ConfigError(f"jobs must be a positive integer, got {jobs!r}")
        self.output_root = output_root
        self._runner_factory = runner_factory
        self._clock = clock
        self.jobs = jobs

    def _suite_dir(self, suite: Suite) -> Path:
        base = f"suite-{_slug(suite.name)}-{self._clock():%Y%m%dT%H%M%SZ}"
        candidate, n = self.output_root / base, 1
        while candidate.exists():
            candidate = self.output_root / f"{base}-{n}"
            n += 1
        candidate.mkdir(parents=True)
        return candidate

    def run(
        self,
        suite: Suite,
        *,
        on_result: Callable[[SuiteEntryResult], None] | None = None,
        fail_fast: bool = False,
        only: Iterable[str] | None = None,
    ) -> SuiteResult:
        """``only`` runs the named experiments and what they depend on (see
        ``select_entries``); the rest are reported as skipped."""
        only_names = tuple(only) if only is not None else None
        selected = select_entries(suite, only_names) if only_names is not None else None
        # Validate every experiment that will run (adapter, faults, contracts) before
        # running any of them, so a mistake in the last entry cannot leave a half-run suite.
        planner = self._runner_factory(self.output_root)
        for entry in suite.entries:
            if selected is not None and entry.id not in selected:
                continue
            try:
                planner.plan(entry.experiment)
            except ConfigError as exc:
                raise ConfigError(f"experiment '{entry.id}': {exc}") from exc

        suite_dir = self._suite_dir(suite)
        order = execution_order(suite.entries)
        if self.jobs == 1:
            results = self._run_sequential(order, suite_dir / "runs", on_result, fail_fast, selected)
        else:
            results = self._run_parallel(order, suite_dir / "runs", on_result, fail_fast, selected)
        outcome = SuiteResult(suite, suite_dir, results, only_names)
        _write_outputs(outcome)
        return outcome

    @staticmethod
    def _resolve(
        entry: SuiteEntry, status: Mapping[str, str], stopped: bool, selected: set[str] | None,
    ) -> SuiteEntryResult | None:
        """The result of an entry that will not run, or None if it should run."""
        if selected is not None and entry.id not in selected:
            return SuiteEntryResult(entry, "skipped", skip_reason=NOT_SELECTED)
        if stopped:
            return SuiteEntryResult(entry, "skipped", skip_reason=FAIL_FAST)
        blockers = [d for d in entry.depends_on if status.get(d) != "met"]
        return SuiteEntryResult(entry, "blocked", blocked_by=blockers) if blockers else None

    @staticmethod
    def _finished(entry: SuiteEntry, verdict: Verdict, run_id: str, bundle_dir: Path) -> SuiteEntryResult:
        status = "met" if verdict is entry.expect else "unmet"
        return SuiteEntryResult(entry, status, verdict, run_id, bundle_dir, coverage=_coverage_of(bundle_dir))

    def _run_sequential(
        self, order: list[SuiteEntry], runs_dir: Path,
        on_result: Callable[[SuiteEntryResult], None] | None, fail_fast: bool,
        selected: set[str] | None,
    ) -> list[SuiteEntryResult]:
        runner = self._runner_factory(runs_dir)
        status: dict[str, str] = {}
        results: list[SuiteEntryResult] = []
        stopped = False
        for entry in order:
            result = self._resolve(entry, status, stopped, selected)
            if result is None:
                run = runner.run(entry.experiment)
                result = self._finished(entry, run.verdict, run.run_id, run.bundle_dir)
            status[entry.id] = result.status
            stopped = stopped or (fail_fast and result.status == "unmet")
            results.append(result)
            if on_result:
                on_result(result)
        return results

    def _run_parallel(
        self, order: list[SuiteEntry], runs_dir: Path,
        on_result: Callable[[SuiteEntryResult], None] | None, fail_fast: bool,
        selected: set[str] | None,
    ) -> list[SuiteEntryResult]:
        """Start each entry once every dependency has finished; report in ``order``."""
        status: dict[str, str] = {}
        done: dict[str, SuiteEntryResult] = {}
        pending = list(order)
        running: dict[Future[tuple[Verdict, str, Path]], SuiteEntry] = {}
        stopped = False

        def record(result: SuiteEntryResult) -> None:
            nonlocal stopped
            done[result.entry.id] = result
            status[result.entry.id] = result.status
            stopped = stopped or (fail_fast and result.status == "unmet")
            if on_result:
                on_result(result)

        executor: Executor
        if self._runner_factory is ExperimentRunner:
            executor = ProcessPoolExecutor(max_workers=self.jobs)
            submit = lambda e: executor.submit(_run_in_worker, runs_dir, e.experiment)  # noqa: E731
        else:
            local = self._runner_factory(runs_dir)

            def run_local(experiment: ExperimentConfig) -> tuple[Verdict, str, Path]:
                run = local.run(experiment)
                return run.verdict, run.run_id, run.bundle_dir

            executor = ThreadPoolExecutor(max_workers=self.jobs)
            submit = lambda e: executor.submit(run_local, e.experiment)  # noqa: E731
        with executor:
            while pending or running:
                for entry in list(pending):
                    if any(d not in status for d in entry.depends_on):
                        continue  # a dependency is still pending or running
                    if not stopped and len(running) >= self.jobs:
                        break
                    pending.remove(entry)
                    result = self._resolve(entry, status, stopped, selected)
                    if result is not None:
                        record(result)
                    else:
                        running[submit(entry)] = entry
                if not running:
                    continue
                finished, _ = wait(running, return_when=FIRST_COMPLETED)
                for future in finished:
                    entry = running.pop(future)
                    record(self._finished(entry, *future.result()))
        return [done[entry.id] for entry in order]


def _write_outputs(outcome: SuiteResult) -> None:
    d = outcome.suite_dir
    sweeps = sweep_summaries(outcome.results)
    summary = {
        "suite": outcome.suite.name,
        "source": str(outcome.suite.source_path) if outcome.suite.source_path else None,
        "passed": outcome.passed,
        "only": list(outcome.only) if outcome.only is not None else None,
        "counts": {s: sum(r.status == s for r in outcome.results) for s in ("met", "unmet", "blocked", "skipped")},
        "experiments": [r.to_dict() for r in outcome.results],
        "sweeps": sweeps,
    }
    (d / "summary.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8", newline="\n")

    lines = [f"# Suite `{outcome.suite.name}`: {'PASSED' if outcome.passed else 'FAILED'}\n",
             "| Experiment | Expected | Verdict | Status | Bundle |", "|---|---|---|---|---|"]
    for r in outcome.results:
        bundle = (f"`runs/{r.bundle_dir.name}`" if r.bundle_dir
                  else f"skipped ({r.skip_reason})" if r.status == "skipped"
                  else f"blocked by {', '.join(r.blocked_by)}")
        verdict = r.verdict.upper() if r.verdict else "—"
        lines.append(f"| `{r.entry.id}` | {r.entry.expect.upper()} | {verdict} | {r.status} | {bundle} |")
    if sweeps:
        lines += ["", "## Sweeps", "", "| Sweep | Seeds | Met | Unmet | Blocked | Coverage bins never hit |",
                  "|---|---|---|---|---|---|"]
        for s in sweeps:
            never = "; ".join(f"{g}: {', '.join(b)}" for g, b in s["never_hit"].items()) or (
                "none" if s["coverage"] else "no coverage reported")
            c = s["counts"]
            lines.append(f"| `{s['sweep']}` | {len(s['seeds'])} | {c['met']} | {c['unmet']} | {c['blocked']} | {never} |")
    (d / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")

    suite = ET.Element("testsuite", name=outcome.suite.name, tests=str(len(outcome.results)))
    failures = skipped = 0
    for r in outcome.results:
        case = ET.SubElement(suite, "testcase", classname="crosslayer.suite", name=r.entry.id)
        if r.status == "unmet":
            failures += 1
            ET.SubElement(case, "failure", message=f"expected {r.entry.expect.upper()}, got {r.verdict.upper() if r.verdict else 'none'}")
        elif r.status in ("blocked", "skipped"):
            skipped += 1
            message = (f"skipped: {r.skip_reason}" if r.status == "skipped"
                       else f"blocked by {', '.join(r.blocked_by)}")
            ET.SubElement(case, "skipped", message=message)
    suite.set("failures", str(failures))
    suite.set("skipped", str(skipped))
    suite.set("errors", "0")
    ET.indent(suite)
    (d / "junit.xml").write_text('<?xml version="1.0" encoding="utf-8"?>\n'
                                 + ET.tostring(suite, encoding="unicode") + "\n",
                                 encoding="utf-8", newline="\n")
