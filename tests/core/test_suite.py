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
