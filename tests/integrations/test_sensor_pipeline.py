"""Reference pipeline: every fault must trip exactly the contracts designed to
catch it, and the baseline must stay clean. These tests are the controlled
fault-injection evidence that the core detects what it claims to detect."""

from __future__ import annotations

from pathlib import Path

import pytest

from crosslayer.core.adapters import check_adapter_conformance
from crosslayer.core.errors import ConfigError
from crosslayer.core.orchestrator import ExperimentRunner, compare_bundles, load_experiment, parse_experiment
from crosslayer.core.verdict import Verdict
from crosslayer.integrations.sensor_pipeline import SensorPipelineAdapter
from crosslayer.integrations.sensor_pipeline import sim

EXPERIMENTS = Path(__file__).resolve().parents[2] / "experiments" / "reference"
EXPERIMENT_FILES = [EXPERIMENTS / "baseline.yaml", *sorted(EXPERIMENTS.glob("fault_*.yaml"))]
ALL_CONTRACTS = {
    "pipeline.rx_accounted",
    "pipeline.packet_lifecycle",
    "pipeline.payload_integrity",
    "pipeline.latency_p99",
    "pipeline.sequence_contiguous",
    "pipeline.processing_order",
    "pipeline.counter_conservation",
    "pipeline.drop_budget",
    "host.clean_exit",
}


@pytest.fixture(scope="module")
def runs(tmp_path_factory):
    """Run every reference experiment once and share the results."""
    out = tmp_path_factory.mktemp("runs")
    runner = ExperimentRunner(out)
    return {path.stem: runner.run(load_experiment(path)) for path in EXPERIMENT_FILES}


def _failed(result) -> set[str]:
    return {r.contract.id for r in result.results if r.verdict is Verdict.FAIL}


def test_adapter_conforms():
    assert check_adapter_conformance(SensorPipelineAdapter()) == []


def test_baseline_passes_every_contract(runs):
    result = runs["baseline"]
    assert result.verdict is Verdict.PASS
    assert {r.contract.id for r in result.results} == ALL_CONTRACTS
    assert all(r.verdict is Verdict.PASS for r in result.results)
    assert result.manifest["events"]["parse_diagnostics"] == 0
    # the baseline contains explicit drops, so the drop path is exercised, not just absent
    assert result.manifest["events"]["by_type"].get("packet.dropped", 0) > 0


@pytest.mark.parametrize(
    "experiment, expected_failures, target",
    [
        ("fault_silent_drop", {"pipeline.rx_accounted", "pipeline.counter_conservation"}, "sensor_rx"),
        ("fault_slow_consumer", {"pipeline.rx_accounted", "pipeline.latency_p99", "pipeline.drop_budget"}, "proc"),
        ("fault_corrupt_checksum", {"pipeline.payload_integrity"}, "ring_buffer"),
        ("fault_duplicate_completion", {"pipeline.packet_lifecycle"}, "proc"),
        ("fault_reorder", {"pipeline.processing_order"}, "ring_buffer"),
    ],
)
def test_each_fault_trips_its_contracts(runs, experiment, expected_failures, target):
    result = runs[experiment]
    assert result.verdict is Verdict.FAIL
    assert _failed(result) == expected_failures
    assert result.manifest["faults"][0]["target_subsystem"] == target


def test_silent_drop_evidence_points_at_raw_log(runs):
    result = runs["fault_silent_drop"]
    [rx] = [r for r in result.results if r.contract.id == "pipeline.rx_accounted"]
    for finding in rx.findings:
        [ref] = finding.evidence
        assert ref.event_type == "packet.received"
        path, _, line = ref.raw_artifact_ref.partition("#L")
        log_line = (result.bundle_dir / path).read_text(encoding="utf-8").splitlines()[int(line) - 1]
        assert "sensor_rx: RX" in log_line


def test_same_seed_is_repeatable_and_fault_runs_share_traffic(runs, tmp_path):
    again = ExperimentRunner(tmp_path).run(load_experiment(EXPERIMENTS / "baseline.yaml"))
    comparison = compare_bundles(runs["baseline"].bundle_dir, again.bundle_dir)
    assert comparison.fingerprint_match and comparison.same_config and not comparison.regressions

    # Separate random streams: a fault that only changes processing must not
    # change which packets arrive or when.
    def arrivals(result):
        return [(e["timestamp_ns"]) for e in _events(result) if e["event_type"] == "packet.received"]

    assert arrivals(runs["baseline"]) == arrivals(runs["fault_slow_consumer"])


def _events(result):
    import json

    path = result.bundle_dir / "normalized_events.jsonl"
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def test_different_seed_changes_event_stream(runs, tmp_path):
    other = ExperimentRunner(tmp_path).run(load_experiment(EXPERIMENTS / "baseline.yaml").with_seed(99))
    assert other.manifest["events"]["fingerprint"] != runs["baseline"].manifest["events"]["fingerprint"]


def test_comparison_flags_regressions(runs):
    comparison = compare_bundles(runs["baseline"].bundle_dir, runs["fault_silent_drop"].bundle_dir)
    assert {d.contract_id for d in comparison.regressions} == {
        "pipeline.rx_accounted",
        "pipeline.counter_conservation",
    }


def test_truncated_log_is_inconclusive_not_failed(runs, tmp_path):
    """Analyze a log cut off mid-run: missing HALT evidence must turn the
    accounting contract INCONCLUSIVE instead of reporting false drops."""
    full = (runs["baseline"].bundle_dir / "raw_artifacts" / "sim.log").read_text(encoding="utf-8")
    truncated = tmp_path / "truncated.log"
    truncated.write_text("\n".join(full.splitlines()[:300]) + "\n", encoding="utf-8")

    config = parse_experiment(
        {"schema_version": 1, "name": "truncated", "adapter": "sensor_pipeline", "seed": 0,
         "inputs": ["truncated.log"]},
        base_dir=tmp_path,
        source="test",
    )
    result = ExperimentRunner(tmp_path / "runs").run(config)
    verdicts = {r.contract.id: r.verdict for r in result.results}
    assert result.manifest["mode"] == "analyze"
    assert verdicts["pipeline.rx_accounted"] is Verdict.INCONCLUSIVE
    assert verdicts["host.clean_exit"] is Verdict.SKIPPED
    assert verdicts["pipeline.counter_conservation"] is Verdict.INCONCLUSIVE  # no HALT record
    assert result.verdict is Verdict.INCONCLUSIVE


def test_unparseable_lines_become_diagnostics(tmp_path):
    log = tmp_path / "noisy.log"
    log.write_text(
        "[    0.000000] sys: BOOT fw=x seed=1 cap=8\n"
        "random noise\n"
        "[    0.001000] sensor_rx: RX seq=1 len=8 crc=0x10 mystery=7\n"
        "[    0.001200] proc: DONE seq=1 crc_rx=0x10 crc_calc=0xZZ lat_us=200\n"
        "[    0.002000] sys: HALT rx=1 done=0 drop=0\n",
        encoding="utf-8",
    )
    config = parse_experiment(
        {"schema_version": 1, "name": "noisy", "adapter": "sensor_pipeline", "seed": 0, "inputs": ["noisy.log"]},
        base_dir=tmp_path,
        source="test",
    )
    result = ExperimentRunner(tmp_path / "runs").run(config)
    assert result.manifest["events"]["parse_diagnostics"] == 3  # noise, unknown field, bad hex


@pytest.mark.parametrize(
    "parameters, faults",
    [
        ({"packets": 0}, []),
        ({"service_us_min": 500, "service_us_max": 100}, []),
        ({"colour": 3}, []),
        ({}, [{"name": "slow_consumer", "params": {"speed": 2}}]),
        ({}, [{"name": "slow_consumer", "params": {"factor": 0}}]),
    ],
)
def test_bad_parameters_rejected(tmp_path, parameters, faults):
    config = parse_experiment(
        {"schema_version": 1, "name": "bad", "adapter": "sensor_pipeline", "seed": 1,
         "parameters": parameters, "faults": faults},
        base_dir=tmp_path,
        source="test",
    )
    with pytest.raises(ConfigError):
        ExperimentRunner(tmp_path / "runs").run(config)
    assert not (tmp_path / "runs").exists()  # rejected before any bundle was created


def test_crc8_known_vector():
    # CRC-8/SMBUS check value for ASCII "123456789" is 0xF4
    assert sim.crc8(b"123456789") == 0xF4


def test_warn_only_failures_do_not_fail_junit(runs):
    import xml.etree.ElementTree as ET

    result = runs["fault_slow_consumer"]  # drop_budget (on_fail: warn) fails here
    suite = ET.parse(result.bundle_dir / "junit.xml").getroot()
    failed = {c.get("name") for c in suite.iter("testcase") if c.find("failure") is not None}
    assert failed == {"pipeline.rx_accounted", "pipeline.latency_p99"}
    [warn] = [c for c in suite.iter("testcase") if c.get("name") == "pipeline.drop_budget"]
    assert warn.find("skipped").get("message").startswith("WARNING")


# -- contract tests (Phase 2) ------------------------------------------------------

PACKAGE = Path(__file__).resolve().parents[2] / "crosslayer" / "integrations" / "sensor_pipeline"


def test_contract_tests_pass_with_full_coverage():
    from crosslayer.core.contracts.testing import run_contract_tests

    report = run_contract_tests(PACKAGE / "contract_tests.yaml")
    assert report.passed, [r.problems for r in report.results if not r.passed]
    assert report.uncovered() == []


@pytest.mark.parametrize(
    "original, weakened, caught_by",
    [
        ("within_ms: 5", "within_ms: 6", "a response after the 5 ms deadline is a violation"),
        ("value: 3000", "value: 4000", "with 20 samples p99 is the maximum, so one slow packet fails it"),
        ('prev.attributes["pipeline.seq"] + 1', 'prev.attributes["pipeline.seq"] + 2',
         "contiguous sequence numbers"),
    ],
)
def test_contract_tests_catch_a_weakened_contract(tmp_path, original, weakened, caught_by):
    """Mutate one contract and check the contract tests notice."""
    from crosslayer.core.contracts.testing import run_contract_tests

    contracts = (PACKAGE / "contracts.yaml").read_text(encoding="utf-8")
    assert original in contracts
    (tmp_path / "mutated.yaml").write_text(contracts.replace(original, weakened), encoding="utf-8")
    tests = (PACKAGE / "contract_tests.yaml").read_text(encoding="utf-8")
    tests = tests.replace("adapter: sensor_pipeline", "contracts: [mutated.yaml]")
    tests = tests.replace("trace: fixtures/", f"trace: {(PACKAGE / 'fixtures').as_posix()}/")
    (tmp_path / "tests.yaml").write_text(tests, encoding="utf-8")

    report = run_contract_tests(tmp_path / "tests.yaml")
    failed = {r.case.name for r in report.results if not r.passed}
    assert caught_by in failed


# -- event catalog and lint (Phase 2) ----------------------------------------------


def test_event_catalog_matches_every_emitted_event(runs):
    """The linter is only as good as the catalog, so check it against reality."""
    from crosslayer.core.adapters import check_events_against_catalog
    from crosslayer.core.telemetry import read_events_jsonl

    catalog = SensorPipelineAdapter().event_catalog()
    for name, result in runs.items():
        events = read_events_jsonl(result.bundle_dir / "normalized_events.jsonl")
        assert check_events_against_catalog(events, catalog) == [], name


def test_catalog_check_reports_undeclared_output():
    from crosslayer.core.adapters import check_events_against_catalog
    from tests.conftest import ev

    catalog = SensorPipelineAdapter().event_catalog()
    events = [ev("packet.received", 0, subsystem="sensor_rx", ids={"ue": 1}, attrs={"pipeline.secret": 1}),
              ev("packet.teleported", 1, subsystem="sensor_rx")]
    problems = check_events_against_catalog(events, catalog)
    assert len(problems) == 3


def test_reference_experiments_have_no_lint_errors_or_warnings():
    from crosslayer.core.contracts.lint import LintLevel
    from crosslayer.core.orchestrator.lint import lint_experiment

    for path in EXPERIMENT_FILES:
        findings = lint_experiment(load_experiment(path))
        assert [f for f in findings if f.level is not LintLevel.INFO] == [], path.name


def test_lint_catches_a_misspelled_attribute_in_a_reference_contract(tmp_path):
    from crosslayer.core.orchestrator.lint import lint_contract_file

    text = (PACKAGE / "contracts.yaml").read_text(encoding="utf-8").replace(
        'e.attributes["pipeline.crc_calc"]', 'e.attributes["pipeline.crc_calculated"]')
    path = tmp_path / "contracts.yaml"
    path.write_text(text, encoding="utf-8")
    findings = lint_contract_file(path, "sensor_pipeline")
    assert [(f.code, f.contract_id) for f in findings if f.code == "CL011"] == [("CL011", "pipeline.payload_integrity")]


def test_compare_flags_contract_changes_between_runs(runs, tmp_path):
    """A verdict change caused by editing a contract must not look like a system regression."""
    import yaml as _yaml

    from crosslayer.core.orchestrator import parse_experiment

    contracts = _yaml.safe_load((PACKAGE / "contracts.yaml").read_text(encoding="utf-8"))["contracts"]
    for contract in contracts:
        if contract["id"] == "pipeline.latency_p99":
            contract["rule"]["value"] = 1000  # tightened: the baseline now fails it
            contract["version"] = 2
    config = parse_experiment(
        {"schema_version": 1, "name": "tightened", "adapter": "sensor_pipeline", "seed": 1234,
         "parameters": {"packets": 400, "buffer_capacity": 8}, "contracts": contracts,
         "include_adapter_contracts": False},
        base_dir=tmp_path, source="test")
    tightened = ExperimentRunner(tmp_path / "runs").run(config)

    comparison = compare_bundles(runs["baseline"].bundle_dir, tightened.bundle_dir)
    assert comparison.fingerprint_match  # the system behaved identically
    [delta] = [d for d in comparison.deltas if d.contract_id == "pipeline.latency_p99"]
    assert (delta.change, delta.contract_changed) == ("regression", True)
    assert (delta.baseline_version, delta.candidate_version) == (1, 2)
    assert not any(d.contract_changed for d in comparison.deltas if d is not delta)


def _done_sequence(*extra: str) -> list[int]:
    import io
    import re

    out = io.StringIO()
    args = sim.parse_args(["--seed", "7", "--packets", "60", *extra])
    sim.simulate(args, out)
    return [int(m) for m in re.findall(r"proc: DONE seq=(\d+)", out.getvalue())]


def test_reorder_fault_changes_completion_order_only_when_enabled():
    plain = _done_sequence()
    assert plain == sorted(plain)
    reordered = _done_sequence("--fault", "reorder", "--reorder-every", "2")
    assert reordered != sorted(reordered)
    assert sorted(reordered) == sorted(plain)
