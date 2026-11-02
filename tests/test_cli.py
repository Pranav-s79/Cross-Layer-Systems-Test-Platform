from __future__ import annotations

from pathlib import Path

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
