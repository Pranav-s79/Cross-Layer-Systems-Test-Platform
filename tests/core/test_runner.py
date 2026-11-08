"""End-to-end behavior of the experiment runner with controllable fake adapters."""

from __future__ import annotations

import shutil
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path

import pytest
import yaml

from crosslayer.core.adapters import (
    AdapterDescription,
    Artifact,
    Capability,
    ExecutionResult,
    ExecutionStatus,
    FaultSpec,
    NormalizationResult,
    ParseDiagnostic,
    SystemAdapter,
)
from crosslayer.core.artifacts import load_json, verify_bundle
from crosslayer.core.errors import ConfigError
from crosslayer.core.orchestrator import ExperimentRunner, load_experiment, parse_experiment
from crosslayer.core.telemetry import NormalizedEvent, read_events_jsonl
from crosslayer.core.verdict import Verdict

CONTRACTS = [
    {
        "id": "demo.req_acked",
        "title": "Every request is acknowledged",
        "owner": "team-net",
        "source": "client",
        "destination": "server",
        "rule": {
            "kind": "response",
            "trigger": {"event_type": "req"},
            "response": {"event_type": "ack"},
            "correlate_by": "txn",
        },
    }
]


class FakeAdapter(SystemAdapter):
    """Writes a log of `<t_ns> <event_type> <txn>` lines, then parses it."""

    def __init__(self, lines=("0 req 1", "5 ack 1"), status=ExecutionStatus.COMPLETED,
                 crash_in=None, run_id_override=None):
        self.lines = lines
        self.status = status
        self.crash_in = crash_in
        self.run_id_override = run_id_override

    def describe(self):
        return AdapterDescription("fake", "0.0.1", "fake-system", "v1", ("client", "server"))

    def capabilities(self):
        return frozenset({Capability.EXECUTE, Capability.COLLECT, Capability.NORMALIZE, Capability.FAULT_INJECTION})

    def faults(self):
        return [FaultSpec("lose_ack", "drop acknowledgements", target_subsystem="server")]

    def execute(self, ctx):
        lines = [l for l in self.lines if not (any(f.name == "lose_ack" for f in ctx.faults) and " ack " in l)]
        (ctx.raw_dir / "app.log").write_text("\n".join(lines) + "\n", encoding="utf-8")
        return ExecutionResult(self.status, 0 if self.status is ExecutionStatus.COMPLETED else None, 0.01)

    def collect(self, ctx, execution):
        if ctx.inputs:
            shutil.copyfile(ctx.inputs[0], ctx.raw_dir / "app.log")
        return [Artifact("app.log", ctx.raw_dir / "app.log")]

    def normalize(self, ctx, artifacts):
        if self.crash_in == "normalize":
            raise RuntimeError("parser exploded")
        events, diags = [], []
        for n, line in enumerate(artifacts[0].path.read_text(encoding="utf-8").splitlines(), start=1):
            parts = line.split()
            if len(parts) != 3:
                diags.append(ParseDiagnostic("app.log", n, f"bad line {line!r}"))
                continue
            t, etype, txn = parts
            events.append(NormalizedEvent(
                run_id=self.run_id_override or ctx.run_id, event_id=f"app.log:{n}", source_system="fake",
                subsystem="server" if etype == "ack" else "client", timestamp_ns=int(t), event_type=etype,
                correlation_ids={"txn": txn}, raw_artifact_ref=ctx.raw_ref("app.log", n),
            ))
        return NormalizationResult(events=events, diagnostics=diags)


class LogOnly(FakeAdapter):
    def capabilities(self):
        return frozenset({Capability.COLLECT, Capability.NORMALIZE})

    execute = SystemAdapter.execute
    faults = SystemAdapter.faults


def experiment(tmp_path, **overrides):
    doc = {"schema_version": 1, "name": "demo", "adapter": "fake", "seed": 7, "contracts": CONTRACTS}
    doc.update(overrides)
    return parse_experiment(doc, base_dir=tmp_path, source="test")


def runner(tmp_path, adapter, clock=None):
    kwargs = {"adapter_loader": lambda ref: adapter}
    if clock:
        kwargs["clock"] = clock
    return ExperimentRunner(tmp_path / "runs", **kwargs)


def test_passing_run_produces_complete_verified_bundle(tmp_path):
    result = runner(tmp_path, FakeAdapter()).run(experiment(tmp_path))
    assert result.verdict is Verdict.PASS
    root = result.bundle_dir
    for name in ("manifest.json", "configuration.yaml", "versions.json", "normalized_events.jsonl",
                 "normalization.json", "contracts.json", "violations.json", "report.md", "junit.xml",
                 "replay.sh", "raw_artifacts/app.log"):
        assert (root / name).is_file(), name
    assert verify_bundle(root) == []
    manifest = load_json(root, "manifest.json")
    assert manifest["events"]["count"] == 2
    assert {f["path"] for f in manifest["files"]} >= {"raw_artifacts/app.log", "report.md"}
    assert len(read_events_jsonl(root / "normalized_events.jsonl")) == 2
    suite = ET.parse(root / "junit.xml").getroot()
    assert suite.get("tests") == "1" and suite.get("failures") == "0"
    [case] = suite.findall("testcase")
    props = {p.get("name"): p.get("value") for p in case.findall("properties/property")}
    assert props == {"severity": "major", "boundary": "client -> server", "version": "1"}


def test_timings_cover_every_stage_and_stay_out_of_the_fingerprint(tmp_path):
    result = runner(tmp_path, FakeAdapter()).run(experiment(tmp_path))
    timings = load_json(result.bundle_dir, "timings.json")
    assert set(timings["stages"]) == {"plan", "build", "execute", "collect", "normalize", "evaluate", "write"}
    assert all(v >= 0 for v in timings["stages"].values())
    manifest = load_json(result.bundle_dir, "manifest.json")
    assert "timings.json" in {f["path"] for f in manifest["files"]}
    assert "## Timings" in (result.bundle_dir / "report.md").read_text(encoding="utf-8")
    other = runner(tmp_path, FakeAdapter()).run(experiment(tmp_path))
    assert other.manifest["events"]["fingerprint"] == manifest["events"]["fingerprint"]


def test_tampering_is_detected(tmp_path):
    result = runner(tmp_path, FakeAdapter()).run(experiment(tmp_path))
    (result.bundle_dir / "raw_artifacts" / "app.log").write_text("edited\n", encoding="utf-8")
    assert verify_bundle(result.bundle_dir) == ["modified: raw_artifacts/app.log"]


def test_injected_fault_fails_with_traceable_evidence(tmp_path):
    result = runner(tmp_path, FakeAdapter()).run(experiment(tmp_path, faults=["lose_ack"]))
    assert result.verdict is Verdict.FAIL
    manifest = result.manifest
    assert manifest["faults"] == [{"name": "lose_ack", "params": {}, "target_subsystem": "server",
                                   "target_interface": None}]
    [violation] = load_json(result.bundle_dir, "violations.json")["violations"]
    assert violation["contract_id"] == "demo.req_acked"
    assert violation["findings"][0]["evidence"][0]["raw_artifact_ref"] == "raw_artifacts/app.log#L1"
    suite = ET.parse(result.bundle_dir / "junit.xml").getroot()
    assert suite.get("failures") == "1"


def test_parse_diagnostics_are_recorded(tmp_path):
    adapter = FakeAdapter(lines=("0 req 1", "garbage", "5 ack 1"))
    result = runner(tmp_path, adapter).run(experiment(tmp_path))
    assert result.verdict is Verdict.PASS
    assert result.manifest["events"]["parse_diagnostics"] == 1
    assert load_json(result.bundle_dir, "normalization.json")["diagnostics"][0]["line"] == 2


def test_timeout_is_error_but_contracts_still_evaluated(tmp_path):
    result = runner(tmp_path, FakeAdapter(status=ExecutionStatus.TIMEOUT)).run(experiment(tmp_path))
    assert result.verdict is Verdict.ERROR
    assert result.manifest["errors"][0]["stage"] == "execute"
    assert result.results and result.results[0].verdict is Verdict.PASS


def test_adapter_crash_is_recorded_and_evaluation_skipped(tmp_path):
    result = runner(tmp_path, FakeAdapter(crash_in="normalize")).run(experiment(tmp_path))
    assert result.verdict is Verdict.ERROR
    [error] = result.manifest["errors"]
    assert error["stage"] == "normalize" and "parser exploded" in error["message"]
    assert result.results == []
    assert (result.bundle_dir / "manifest.json").is_file()


def test_foreign_run_id_is_rejected(tmp_path):
    result = runner(tmp_path, FakeAdapter(run_id_override="someone-else")).run(experiment(tmp_path))
    assert result.verdict is Verdict.ERROR
    assert "foreign run ids" in result.manifest["errors"][0]["message"]


@pytest.mark.parametrize(
    "adapter, overrides, match",
    [
        (FakeAdapter(), {"faults": ["unknown_fault"]}, "unknown faults"),
        (FakeAdapter(), {"faults": [{"name": "lose_ack", "params": {"rate": 2}}]}, "has no parameters"),
        (LogOnly(), {}, "cannot execute"),
        (LogOnly(), {"faults": ["lose_ack"]}, "fault injection"),
        (FakeAdapter(), {"contracts": [], "include_adapter_contracts": False}, "no contracts"),
        (FakeAdapter(), {"contracts": [{**CONTRACTS[0], "source": "mystery"}]}, "does not declare"),
    ],
)
def test_configuration_errors_create_no_bundle(tmp_path, adapter, overrides, match):
    with pytest.raises(ConfigError, match=match):
        runner(tmp_path, adapter).run(experiment(tmp_path, **overrides))
    assert not (tmp_path / "runs").exists() or not any((tmp_path / "runs").iterdir())


def test_analyze_mode_uses_inputs(tmp_path):
    log = tmp_path / "captured.log"
    log.write_text("0 req 9\n3 ack 9\n", encoding="utf-8")
    result = runner(tmp_path, LogOnly()).run(experiment(tmp_path, inputs=["captured.log"]))
    assert result.verdict is Verdict.PASS
    assert result.manifest["mode"] == "analyze"
    assert result.manifest["execution"] is None
    assert result.manifest["inputs"][0]["path"] == str(log.resolve())


def test_same_second_runs_get_distinct_ids(tmp_path):
    fixed = datetime(2026, 1, 1, tzinfo=timezone.utc)
    r = runner(tmp_path, FakeAdapter(), clock=lambda: fixed)
    first, second = r.run(experiment(tmp_path)), r.run(experiment(tmp_path))
    assert first.run_id != second.run_id
    assert second.run_id == f"{first.run_id}-1"


def test_config_hash_is_location_independent(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir(), b.mkdir()
    assert experiment(a).config_hash() == experiment(b).config_hash()
    assert experiment(a).config_hash() != experiment(a, seed=8).config_hash()


def test_bundled_configuration_replays_identically(tmp_path):
    adapter = FakeAdapter()
    first = runner(tmp_path, adapter).run(experiment(tmp_path))
    replay_config = load_experiment(first.bundle_dir / "configuration.yaml")
    second = runner(tmp_path, adapter).run(replay_config)
    assert second.manifest["config_hash"] == first.manifest["config_hash"]
    assert second.manifest["events"]["fingerprint"] == first.manifest["events"]["fingerprint"]
    doc = yaml.safe_load((first.bundle_dir / "configuration.yaml").read_text(encoding="utf-8"))
    assert doc["contracts"][0]["id"] == "demo.req_acked"
    assert Path(doc["base_dir"]) == tmp_path


# -- regressions found in the Phase 1 review ---------------------------------------


def test_added_files_are_detected_by_verify(tmp_path):
    result = runner(tmp_path, FakeAdapter()).run(experiment(tmp_path))
    (result.bundle_dir / "raw_artifacts" / "planted.log").write_text("x\n", encoding="utf-8")
    assert verify_bundle(result.bundle_dir) == ["unexpected: raw_artifacts/planted.log"]


def test_non_bundle_directories_give_config_errors(tmp_path):
    from crosslayer.core.orchestrator import compare_bundles

    with pytest.raises(ConfigError, match="not a run bundle"):
        verify_bundle(tmp_path)
    with pytest.raises(ConfigError, match="not a run bundle"):
        compare_bundles(tmp_path, tmp_path)


def test_empty_description_and_negative_seed(tmp_path):
    assert experiment(tmp_path, description=None).description == ""
    with pytest.raises(ConfigError, match="seed"):
        experiment(tmp_path).with_seed(-1)


def test_comparison_classification():
    from crosslayer.core.orchestrator.compare import classify

    assert classify("pass", "fail") == "regression"
    assert classify("pass", "inconclusive") == "lost_coverage"
    assert classify("fail", "pass") == "fixed"
    assert classify("skipped", "pass") == "gained_coverage"
    assert classify(None, "pass") == "added"


def test_git_revision_only_reported_for_repository_root():
    from crosslayer.core.artifacts import git_info

    root = Path(__file__).resolve().parents[2]
    if git_info(root) is None:
        pytest.skip("not running from a git checkout")
    assert git_info(root / "crosslayer") is None  # a subdirectory is not CrossLayer's repo root
