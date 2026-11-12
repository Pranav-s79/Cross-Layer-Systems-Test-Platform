from __future__ import annotations

import json
from pathlib import Path

import pytest

from crosslayer.cli import main

EXPERIMENTS = Path(__file__).resolve().parents[1] / "experiments" / "reference"


def _bundle(out: Path) -> Path:
    [bundle] = [p for p in out.iterdir() if p.is_dir()]
    return bundle


def test_run_exit_codes(tmp_path, capsys):
    assert main(["run", str(EXPERIMENTS / "baseline.yaml"), "--out", str(tmp_path / "a"), "--quiet"]) == 0
    assert main(["run", str(EXPERIMENTS / "fault_silent_drop.yaml"), "--out", str(tmp_path / "b")]) == 1
    out = capsys.readouterr().out
    assert "verdict   FAIL" in out and "pipeline.rx_accounted" in out


def test_compare_and_verify(tmp_path, capsys):
    main(["run", str(EXPERIMENTS / "baseline.yaml"), "--out", str(tmp_path / "a"), "--quiet"])
    main(["run", str(EXPERIMENTS / "fault_corrupt_checksum.yaml"), "--out", str(tmp_path / "b"), "--quiet"])
    a, b = _bundle(tmp_path / "a"), _bundle(tmp_path / "b")
    assert main(["compare", str(a), str(a)]) == 0
    assert main(["compare", str(a), str(b)]) == 1
    assert main(["verify", str(b)]) == 0
    assert "1 regression(s)" in capsys.readouterr().out


def test_replay_matches_original(tmp_path, capsys):
    main(["run", str(EXPERIMENTS / "baseline.yaml"), "--out", str(tmp_path / "a"), "--quiet"])
    original = _bundle(tmp_path / "a")
    capsys.readouterr()
    assert main(["replay", str(original), "--out", str(tmp_path / "r")]) == 0
    out = capsys.readouterr().out
    assert "config hash        match" in out and "event fingerprint  match" in out
    assert main(["verify", str(_bundle(tmp_path / "r"))]) == 0


def test_replay_reports_changed_fingerprint(tmp_path, capsys):
    import json

    main(["run", str(EXPERIMENTS / "baseline.yaml"), "--out", str(tmp_path / "a"), "--quiet"])
    original = _bundle(tmp_path / "a")
    manifest = original / "manifest.json"
    doc = json.loads(manifest.read_text(encoding="utf-8"))
    doc["events"]["fingerprint"] = "0" * 64
    manifest.write_text(json.dumps(doc), encoding="utf-8")
    capsys.readouterr()
    assert main(["replay", str(original), "--out", str(tmp_path / "r")]) == 1
    assert "event fingerprint  DIFFERENT" in capsys.readouterr().out


def test_replay_rejects_non_bundle(tmp_path, capsys):
    assert main(["replay", str(tmp_path)]) == 4


def test_config_error_exit_code(tmp_path, capsys):
    bad = tmp_path / "bad.yaml"
    bad.write_text("schema_version: 1\nname: x\nadapter: nope\nseed: 1\n", encoding="utf-8")
    assert main(["run", str(bad), "--out", str(tmp_path)]) == 4
    assert "unknown adapter" in capsys.readouterr().err


def test_check_validates_files(tmp_path, capsys):
    contracts = Path(__file__).resolve().parents[1] / "crosslayer" / "integrations" / "sensor_pipeline" / "contracts.yaml"
    assert main(["check", str(contracts), str(EXPERIMENTS / "baseline.yaml")]) == 0
    broken = tmp_path / "broken.yaml"
    broken.write_text("schema_version: 1\ncontracts:\n  - id: X\n", encoding="utf-8")
    assert main(["check", str(broken)]) == 4


def test_adapters_lists_reference_adapter(capsys):
    assert main(["adapters"]) == 0
    assert "sensor_pipeline" in capsys.readouterr().out


def test_adapters_check_passes_for_reference_adapter(capsys):
    assert main(["adapters", "--check"]) == 0
    assert "conformance: ok" in capsys.readouterr().out


def test_adapters_check_fails_on_problems(monkeypatch, capsys):
    monkeypatch.setattr("crosslayer.cli.check_adapter_conformance", lambda adapter: ["bad thing"])
    assert main(["adapters", "--check"]) == 1
    assert "problem: bad thing" in capsys.readouterr().out
    assert main(["adapters"]) == 0


def test_adapters_check_fails_on_load_error(monkeypatch):
    def boom(name):
        raise RuntimeError("cannot load")

    monkeypatch.setattr("crosslayer.cli.load_adapter", boom)
    assert main(["adapters", "--check"]) == 1
    assert main(["adapters"]) == 0


def test_bad_inputs_exit_cleanly_with_code_4(tmp_path, capsys):
    broken = tmp_path / "broken.yaml"
    broken.write_text("schema_version: 1\ncontracts: [\n  - bad: [\n", encoding="utf-8")
    assert main(["check", str(broken)]) == 4
    assert main(["compare", str(tmp_path), str(tmp_path)]) == 4
    assert main(["verify", str(tmp_path)]) == 4
    assert "not a run bundle" in capsys.readouterr().err


def test_lint_exit_codes(tmp_path, capsys):
    assert main(["lint", str(EXPERIMENTS / "baseline.yaml"), "--strict"]) == 0
    bad = tmp_path / "bad.yaml"
    bad.write_text(
        "schema_version: 1\ncontracts:\n  - {id: a.b, title: t, owner: o, source: proc, destination: proc,\n"
        "     rule: {kind: count, select: {event_type: packet.teleported}, min: 1}}\n",
        encoding="utf-8",
    )
    assert main(["lint", str(bad)]) == 0  # nothing to check without an adapter catalog
    assert main(["lint", str(bad), "--adapter", "sensor_pipeline"]) == 1
    assert "CL010" in capsys.readouterr().out


def test_test_contracts_command(capsys):
    path = Path(__file__).resolve().parents[1] / "crosslayer" / "integrations" / "sensor_pipeline" / "contract_tests.yaml"
    assert main(["test-contracts", str(path), "--require-coverage"]) == 0
    assert "coverage: 9/9" in capsys.readouterr().out


def test_schema_command(tmp_path, capsys):
    import json

    assert main(["schema", "contracts"]) == 0
    assert json.loads(capsys.readouterr().out)["title"] == "CrossLayer contracts"
    assert main(["schema", "--write", str(tmp_path)]) == 0
    assert sorted(p.name for p in tmp_path.iterdir()) == [
        "contract-tests.schema.json", "contracts.schema.json", "experiment.schema.json", "suite.schema.json"]


def test_suite_command_and_file_kinds(tmp_path, capsys):
    suite = EXPERIMENTS / "suite.yaml"
    assert main(["suite", str(suite), "--out", str(tmp_path)]) == 0
    assert "result    PASSED" in capsys.readouterr().out
    assert main(["check", str(suite), str(EXPERIMENTS / "baseline.yaml")]) == 0
    assert "suite, 6 experiments" in capsys.readouterr().out
    assert main(["lint", str(suite), "--strict"]) == 0


def test_diff_contracts_command(tmp_path, capsys):
    old = Path(__file__).resolve().parents[1] / "crosslayer" / "integrations" / "sensor_pipeline" / "contracts.yaml"
    text = old.read_text(encoding="utf-8")
    unbumped = tmp_path / "unbumped.yaml"
    unbumped.write_text(text.replace("within_ms: 5", "within_ms: 6"), encoding="utf-8")
    assert main(["diff-contracts", str(old), str(unbumped)]) == 1
    assert "without a version bump" in capsys.readouterr().out
    bumped = tmp_path / "bumped.yaml"
    bumped.write_text(text.replace("within_ms: 5", "within_ms: 6").replace(
        "  - id: pipeline.rx_accounted\n", "  - id: pipeline.rx_accounted\n    version: 2\n"), encoding="utf-8")
    assert main(["diff-contracts", str(old), str(bumped), "--markdown"]) == 0
    assert "## firmware-rx" in capsys.readouterr().out


def test_version_flag(capsys):
    import pytest
    from importlib import metadata

    with pytest.raises(SystemExit) as exc:
        main(["--version"])
    assert exc.value.code == 0
    assert capsys.readouterr().out.strip() == f"crosslayer {metadata.version('crosslayer')}"


def test_run_dry_run_writes_no_bundle(tmp_path, capsys):
    out = tmp_path / "runs"
    assert main(["run", str(EXPERIMENTS / "fault_silent_drop.yaml"), "--out", str(out), "--dry-run"]) == 0
    text = capsys.readouterr().out
    assert "mode       execute" in text and "faults     " in text and "pipeline.rx_accounted" in text
    assert not out.exists()


def test_run_dry_run_config_error(tmp_path, capsys):
    bad = tmp_path / "bad.yaml"
    bad.write_text("schema_version: 1\nname: x\nadapter: nope\nseed: 1\n", encoding="utf-8")
    assert main(["run", str(bad), "--out", str(tmp_path / "o"), "--dry-run"]) == 4
    assert not (tmp_path / "o").exists()


def _subcommand_names():
    import argparse

    from crosslayer.cli import build_parser

    [action] = [a for a in build_parser()._actions if isinstance(a, argparse._SubParsersAction)]
    return sorted(action.choices)


@pytest.mark.parametrize("name", _subcommand_names())
def test_subcommand_help_renders(name, capsys):
    with pytest.raises(SystemExit) as exc:
        main([name, "--help"])
    assert exc.value.code == 0
    assert name in capsys.readouterr().out


def test_lint_json_output(capsys):
    import json

    code = main(["lint", "experiments/reference/suite.yaml", "--strict", "--json"])
    doc = json.loads(capsys.readouterr().out)
    assert code == 0 and doc["passed"] is True
    assert doc["targets"] and all(isinstance(t["findings"], list) for t in doc["targets"])


def test_test_contracts_json_output(capsys):
    import json

    path = "crosslayer/integrations/sensor_pipeline/contract_tests.yaml"
    code = main(["test-contracts", path, "--require-coverage", "--json"])
    doc = json.loads(capsys.readouterr().out)
    assert code == 0 and doc["passed"] is True and doc["failed_cases"] == 0
    f = doc["files"][0]
    assert f["coverage"]["covered"] == f["coverage"]["total"] and not f["coverage"]["uncovered"]
    assert {"contract_id", "name", "expect", "actual", "passed", "problems"} <= f["cases"][0].keys()


def test_show_summarizes_bundle_without_running(tmp_path, capsys):
    main(["run", str(EXPERIMENTS / "fault_silent_drop.yaml"), "--out", str(tmp_path / "a"), "--quiet"])
    bundle = _bundle(tmp_path / "a")
    before = sorted(p.name for p in bundle.iterdir())
    capsys.readouterr()
    assert main(["show", str(bundle), "--limit", "2"]) == 0
    out = capsys.readouterr().out
    assert "verdict   FAIL" in out and "pipeline.rx_accounted" in out
    assert "first violations (2 of" in out and "no response within" in out
    assert sorted(p.name for p in bundle.iterdir()) == before


def test_show_rejects_non_bundle(tmp_path):
    assert main(["show", str(tmp_path)]) == 4


def test_show_json(tmp_path, capsys):
    main(["run", str(EXPERIMENTS / "fault_silent_drop.yaml"), "--out", str(tmp_path / "a"), "--quiet"])
    bundle = _bundle(tmp_path / "a")
    capsys.readouterr()
    assert main(["show", str(bundle), "--limit", "2", "--json"]) == 0
    doc = json.loads(capsys.readouterr().out)
    assert doc["verdict"] == "fail" and doc["violation_count"] >= 2
    assert len(doc["violations"]) == 2
    assert any(c["contract_id"] == "pipeline.rx_accounted" for c in doc["contracts"])


def test_verify_json(tmp_path, capsys):
    main(["run", str(EXPERIMENTS / "fault_silent_drop.yaml"), "--out", str(tmp_path / "a"), "--quiet"])
    bundle = _bundle(tmp_path / "a")
    capsys.readouterr()
    assert main(["verify", str(bundle), "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == {"intact": True, "problems": []}
    (bundle / "violations.json").write_text("{}")
    assert main(["verify", str(bundle), "--json"]) == 1
    doc = json.loads(capsys.readouterr().out)
    assert doc["intact"] is False and any("violations.json" in p for p in doc["problems"])
