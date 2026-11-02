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
ALL_CONTRACTS = {
    "pipeline.rx_accounted",
    "pipeline.packet_lifecycle",
    "pipeline.payload_integrity",
    "pipeline.latency_p99",
    "pipeline.sequence_contiguous",
    "pipeline.counter_conservation",
    "pipeline.drop_budget",
    "host.clean_exit",
}


@pytest.fixture(scope="module")
def runs(tmp_path_factory):
    """Run every reference experiment once and share the results."""
    out = tmp_path_factory.mktemp("runs")
    runner = ExperimentRunner(out)
    return {path.stem: runner.run(load_experiment(path)) for path in sorted(EXPERIMENTS.glob("*.yaml"))}


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
