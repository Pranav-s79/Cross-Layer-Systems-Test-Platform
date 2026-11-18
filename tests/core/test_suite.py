"""Experiment suites: dependency order, expectations, blocking, outputs."""

from __future__ import annotations

import json
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest
import yaml

from crosslayer.core.errors import ConfigError
from crosslayer.core.orchestrator import ExperimentRunner
from crosslayer.core.orchestrator.suite import SuiteRunner, execution_order, load_suite
from crosslayer.core.verdict import Verdict
from tests.core.test_runner import CONTRACTS, FakeAdapter


def _experiment(tmp_path: Path, name: str, faults=()) -> str:
    doc = {"schema_version": 1, "name": name, "adapter": "fake", "seed": 1,
           "contracts": CONTRACTS, "faults": list(faults)}
    (tmp_path / f"{name}.yaml").write_text(yaml.safe_dump(doc), encoding="utf-8")
    return f"{name}.yaml"


def _suite(tmp_path: Path, entries: list[dict]) -> Path:
    path = tmp_path / "suite.yaml"
    path.write_text(yaml.safe_dump({"schema_version": 1, "name": "Demo Suite", "experiments": entries}),
                    encoding="utf-8")
    return path


def _runner(tmp_path: Path) -> SuiteRunner:
    return SuiteRunner(tmp_path / "out",
                       runner_factory=lambda out: ExperimentRunner(out, adapter_loader=lambda ref: FakeAdapter()))


def test_order_follows_dependencies_then_file_order(tmp_path):
    exp = _experiment(tmp_path, "e")
    suite = load_suite(_suite(tmp_path, [
        {"id": "c", "path": exp, "depends_on": ["b"]},
        {"id": "a", "path": exp},
        {"id": "b", "path": exp, "depends_on": ["a"]},
        {"id": "d", "path": exp},
    ]))
    # Among experiments whose dependencies are met, the one listed first runs
    # first: c (listed first) is ready after b, so it runs before d.
    assert [e.id for e in execution_order(suite.entries)] == ["a", "b", "c", "d"]


def test_cycle_is_reported_with_its_path(tmp_path):
    exp = _experiment(tmp_path, "e")
    with pytest.raises(ConfigError, match=r"dependency cycle: a -> b -> c -> a"):
        load_suite(_suite(tmp_path, [
            {"id": "a", "path": exp, "depends_on": ["b"]},
            {"id": "b", "path": exp, "depends_on": ["c"]},
            {"id": "c", "path": exp, "depends_on": ["a"]},
            {"id": "d", "path": exp},
        ]))


@pytest.mark.parametrize(
    "entries, match",
    [
        ([{"id": "a", "path": "e.yaml", "depends_on": ["a"]}], "depends on itself"),
        ([{"id": "a", "path": "e.yaml", "depends_on": ["ghost"]}], "unknown experiments"),
        ([{"id": "a", "path": "e.yaml"}, {"id": "a", "path": "e.yaml"}], "duplicate"),
        ([{"id": "A B", "path": "e.yaml"}], "'id' must be"),
        ([{"id": "a", "path": "e.yaml", "expect": "maybe"}], "expect must be"),
        ([{"id": "a", "path": "missing.yaml"}], "cannot read file"),
        ([{"id": "a", "path": "e.yaml", "retries": 3}], "unknown keys"),
    ],
)
def test_configuration_errors(tmp_path, entries, match):
    _experiment(tmp_path, "e")
    with pytest.raises(ConfigError, match=match):
        load_suite(_suite(tmp_path, entries))


def test_expected_failures_are_met(tmp_path):
    suite = load_suite(_suite(tmp_path, [
        {"id": "baseline", "path": _experiment(tmp_path, "baseline")},
        {"id": "fault", "path": _experiment(tmp_path, "fault", ["lose_ack"]), "expect": "fail",
         "depends_on": ["baseline"]},
    ]))
    outcome = _runner(tmp_path).run(suite)
    assert outcome.passed
    assert [(r.entry.id, r.status, r.verdict) for r in outcome.results] == [
        ("baseline", "met", Verdict.PASS), ("fault", "met", Verdict.FAIL)]


def test_failed_baseline_blocks_dependents(tmp_path):
    # the "baseline" is broken (it injects the fault) but still expects PASS
    suite = load_suite(_suite(tmp_path, [
        {"id": "baseline", "path": _experiment(tmp_path, "broken", ["lose_ack"])},
        {"id": "fault", "path": _experiment(tmp_path, "fault", ["lose_ack"]), "expect": "fail",
         "depends_on": ["baseline"]},
        {"id": "independent", "path": _experiment(tmp_path, "ok")},
    ]))
    outcome = _runner(tmp_path).run(suite)
    assert not outcome.passed
    status = {r.entry.id: (r.status, r.blocked_by) for r in outcome.results}
    assert status == {"baseline": ("unmet", []), "fault": ("blocked", ["baseline"]), "independent": ("met", [])}

    # a blocked experiment never ran: only two bundles exist
    assert len(list((outcome.suite_dir / "runs").iterdir())) == 2
    summary = json.loads((outcome.suite_dir / "summary.json").read_text(encoding="utf-8"))
    assert summary["counts"] == {"met": 1, "unmet": 1, "blocked": 1, "skipped": 0}
    junit = ET.parse(outcome.suite_dir / "junit.xml").getroot()
    assert (junit.get("failures"), junit.get("skipped")) == ("1", "1")
    assert "blocked by baseline" in (outcome.suite_dir / "report.md").read_text(encoding="utf-8")


def test_fail_fast_skips_the_rest(tmp_path):
    suite = load_suite(_suite(tmp_path, [
        {"id": "a", "path": _experiment(tmp_path, "a")},
        {"id": "b", "path": _experiment(tmp_path, "bad", ["lose_ack"])},
        {"id": "c", "path": _experiment(tmp_path, "c")},
        {"id": "d", "path": _experiment(tmp_path, "d"), "depends_on": ["b"]},
    ]))
    outcome = _runner(tmp_path).run(suite, fail_fast=True)
    assert not outcome.passed
    assert [(r.entry.id, r.status) for r in outcome.results] == [
        ("a", "met"), ("b", "unmet"), ("c", "skipped"), ("d", "skipped")]
    assert len(list((outcome.suite_dir / "runs").iterdir())) == 2  # skipped ones never ran
    summary = json.loads((outcome.suite_dir / "summary.json").read_text(encoding="utf-8"))
    assert summary["counts"] == {"met": 1, "unmet": 1, "blocked": 0, "skipped": 2}
    junit = ET.parse(outcome.suite_dir / "junit.xml").getroot()
    assert (junit.get("failures"), junit.get("skipped")) == ("1", "2")
    assert "skipped (fail-fast)" in (outcome.suite_dir / "report.md").read_text(encoding="utf-8")


def test_without_fail_fast_everything_runs(tmp_path):
    suite = load_suite(_suite(tmp_path, [
        {"id": "b", "path": _experiment(tmp_path, "bad", ["lose_ack"])},
        {"id": "c", "path": _experiment(tmp_path, "c")},
    ]))
    outcome = _runner(tmp_path).run(suite)
    assert [r.status for r in outcome.results] == ["unmet", "met"]


def test_transitive_blocking(tmp_path):
    suite = load_suite(_suite(tmp_path, [
        {"id": "a", "path": _experiment(tmp_path, "bad", ["lose_ack"])},
        {"id": "b", "path": _experiment(tmp_path, "b"), "depends_on": ["a"]},
        {"id": "c", "path": _experiment(tmp_path, "c"), "depends_on": ["b"]},
    ]))
    outcome = _runner(tmp_path).run(suite)
    assert [(r.entry.id, r.status) for r in outcome.results] == [("a", "unmet"), ("b", "blocked"), ("c", "blocked")]


def test_misconfigured_entry_fails_before_anything_runs(tmp_path):
    suite = load_suite(_suite(tmp_path, [
        {"id": "first", "path": _experiment(tmp_path, "first")},
        {"id": "broken", "path": _experiment(tmp_path, "broken", ["no_such_fault"])},
    ]))
    with pytest.raises(ConfigError, match="experiment 'broken'.*unknown faults"):
        _runner(tmp_path).run(suite)
    assert not (tmp_path / "out").exists()  # not even the first experiment ran


# -- seed sweeps ------------------------------------------------------------------


class CoverageAdapter(FakeAdapter):
    """Reports coverage bins that depend on the seed: odd seeds hit `a`, even seeds hit `b`."""

    def normalize(self, ctx, artifacts):
        result = super().normalize(ctx, artifacts)
        result.stats["coverage"] = {"ops": {"a": ctx.seed % 2, "b": 1 - ctx.seed % 2, "never": 0}}
        return result


def test_seeds_expand_into_one_entry_per_seed(tmp_path):
    suite = load_suite(_suite(tmp_path, [
        {"id": "base", "path": _experiment(tmp_path, "base"), "seeds": [3, 5]},
        {"id": "range", "path": _experiment(tmp_path, "range"), "seeds": {"count": 3, "start": 10}},
    ]))
    assert [(e.id, e.experiment.seed, e.sweep) for e in suite.entries] == [
        ("base@3", 3, "base"), ("base@5", 5, "base"),
        ("range@10", 10, "range"), ("range@11", 11, "range"), ("range@12", 12, "range"),
    ]


def test_dependencies_pair_by_seed_or_fan_out(tmp_path):
    suite = load_suite(_suite(tmp_path, [
        {"id": "base", "path": _experiment(tmp_path, "base"), "seeds": [1, 2]},
        {"id": "paired", "path": _experiment(tmp_path, "paired"), "seeds": [1, 2], "depends_on": ["base"]},
        {"id": "other", "path": _experiment(tmp_path, "other"), "seeds": [7], "depends_on": ["base"]},
        {"id": "single", "path": _experiment(tmp_path, "single"), "depends_on": ["base"]},
    ]))
    deps = {e.id: e.depends_on for e in suite.entries}
    assert deps["paired@1"] == ("base@1",) and deps["paired@2"] == ("base@2",)
    assert deps["other@7"] == ("base@1", "base@2")
    assert deps["single"] == ("base@1", "base@2")


@pytest.mark.parametrize(
    "seeds, match",
    [
        ([], "non-empty list"),
        ([1, 1], "distinct"),
        ([-1], "non-negative"),
        ({"count": 0}, "'count' must be a positive integer"),
        ({"count": 2, "step": 1}, "unknown keys"),
        ({"count": 1001}, "at most 1000 seeds"),
        ("1,2", "non-empty list"),
    ],
)
def test_invalid_seeds_name_the_entry(tmp_path, seeds, match):
    with pytest.raises(ConfigError, match=match) as info:
        load_suite(_suite(tmp_path, [{"id": "a", "path": _experiment(tmp_path, "a"), "seeds": seeds}]))
    assert "experiments[0].seeds" in str(info.value)


def test_sweep_runs_every_seed_and_sums_coverage(tmp_path):
    path = _suite(tmp_path, [
        {"id": "base", "path": _experiment(tmp_path, "base"), "seeds": {"count": 3}},
        {"id": "fault", "path": _experiment(tmp_path, "fault", ["lose_ack"]), "expect": "fail",
         "seeds": {"count": 3}, "depends_on": ["base"]},
    ])
    runner = SuiteRunner(tmp_path / "out", runner_factory=lambda out: ExperimentRunner(
        out, adapter_loader=lambda ref: CoverageAdapter()))
    outcome = runner.run(load_suite(path))
    assert outcome.passed
    assert [r.entry.id for r in outcome.results] == ["base@1", "base@2", "base@3", "fault@1", "fault@2", "fault@3"]
    summary = json.loads((outcome.suite_dir / "summary.json").read_text(encoding="utf-8"))
    base = next(s for s in summary["sweeps"] if s["sweep"] == "base")
    assert base["seeds"] == [1, 2, 3] and base["counts"]["met"] == 3
    assert base["coverage"] == {"ops": {"a": 2, "b": 1, "never": 0}}
    assert base["never_hit"] == {"ops": ["never"]}
    assert summary["experiments"][0]["sweep"] == "base" and summary["experiments"][0]["seed"] == 1
    report = (outcome.suite_dir / "report.md").read_text(encoding="utf-8")
    assert "## Sweeps" in report and "| `base` | 3 | 3 | 0 | 0 | ops: never |" in report


def test_sweep_without_coverage_says_so(tmp_path):
    outcome = _runner(tmp_path).run(load_suite(_suite(tmp_path, [
        {"id": "base", "path": _experiment(tmp_path, "base"), "seeds": [1, 2]}])))
    report = (outcome.suite_dir / "report.md").read_text(encoding="utf-8")
    assert "| `base` | 2 | 2 | 0 | 0 | no coverage reported |" in report


# -- parallel execution ------------------------------------------------------------


def _statuses(outcome):
    return [(r.entry.id, r.status, r.verdict) for r in outcome.results]


def _parallel(tmp_path: Path, jobs: int, adapter=FakeAdapter) -> SuiteRunner:
    return SuiteRunner(tmp_path / f"out{jobs}", jobs=jobs,
                       runner_factory=lambda out: ExperimentRunner(out, adapter_loader=lambda ref: adapter()))


def test_parallel_suite_matches_sequential_results_and_order(tmp_path):
    entries = [
        {"id": "base", "path": _experiment(tmp_path, "base"), "seeds": [1, 2, 3]},
        {"id": "fault", "path": _experiment(tmp_path, "fault", ["lose_ack"]), "expect": "fail",
         "seeds": [1, 2, 3], "depends_on": ["base"]},
        {"id": "wrong", "path": _experiment(tmp_path, "wrong", ["lose_ack"])},  # unmet: expects pass
        {"id": "after_wrong", "path": _experiment(tmp_path, "after"), "depends_on": ["wrong"]},
    ]
    suite = load_suite(_suite(tmp_path, entries))
    sequential = _parallel(tmp_path, 1).run(suite)
    parallel = _parallel(tmp_path, 4).run(suite)
    assert _statuses(parallel) == _statuses(sequential)
    assert ("after_wrong", "blocked", None) in _statuses(parallel)
    assert (parallel.suite_dir / "report.md").read_text(encoding="utf-8").count("|") == \
        (sequential.suite_dir / "report.md").read_text(encoding="utf-8").count("|")


def test_parallel_fail_fast_starts_nothing_new_after_an_unmet_result(tmp_path):
    entries = [{"id": "wrong", "path": _experiment(tmp_path, "wrong", ["lose_ack"])}]
    entries += [{"id": f"later{i}", "path": _experiment(tmp_path, f"later{i}"), "depends_on": ["wrong"]}
                for i in range(3)]
    outcome = _parallel(tmp_path, 3).run(load_suite(_suite(tmp_path, entries)), fail_fast=True)
    assert [r.status for r in outcome.results] == ["unmet", "skipped", "skipped", "skipped"]


def test_parallel_runs_of_one_configuration_get_their_own_bundles(tmp_path):
    path = _experiment(tmp_path, "same")
    entries = [{"id": f"copy{i}", "path": path} for i in range(4)]
    outcome = _parallel(tmp_path, 4).run(load_suite(_suite(tmp_path, entries)))
    assert outcome.passed
    assert len({r.bundle_dir for r in outcome.results}) == 4


def test_jobs_must_be_positive(tmp_path):
    with pytest.raises(ConfigError, match="jobs must be a positive integer"):
        SuiteRunner(tmp_path, jobs=0)


def test_reference_suite_runs_in_worker_processes(tmp_path):
    suite = load_suite(Path(__file__).resolve().parents[2] / "experiments" / "reference" / "suite.yaml")
    outcome = SuiteRunner(tmp_path / "out", jobs=2).run(suite)
    assert outcome.passed
    assert all(r.bundle_dir and (r.bundle_dir / "manifest.json").is_file() for r in outcome.results)
