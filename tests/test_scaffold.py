"""`crosslayer new adapter`: the generated project must work before any edits.

This is the mechanical check of the Phase 2 exit criterion: a scaffolded
adapter passes conformance, contract tests with full coverage, lint, and a
sample run, and its own generated test suite is green.
"""

from __future__ import annotations

import subprocess
import sys

import pytest

from crosslayer.cli import main
from crosslayer.core.adapters import check_adapter_conformance, check_events_against_catalog, load_adapter
from crosslayer.core.contracts.lint import LintLevel
from crosslayer.core.contracts.testing import run_contract_tests
from crosslayer.core.errors import ConfigError
from crosslayer.core.orchestrator import ExperimentRunner, load_experiment
from crosslayer.core.orchestrator.lint import lint_experiment
from crosslayer.core.telemetry import read_events_jsonl
from crosslayer.core.verdict import Verdict
from crosslayer.scaffold import class_name, create_adapter_project


def test_generated_project_works_out_of_the_box(tmp_path, monkeypatch):
    root = create_adapter_project("scaffold_check", tmp_path)
    monkeypatch.syspath_prepend(str(root))

    adapter = load_adapter("scaffold_check.adapter:ScaffoldCheckAdapter")
    assert check_adapter_conformance(adapter) == []

    report = run_contract_tests(root / "scaffold_check" / "contract_tests.yaml")
    assert report.passed and report.uncovered() == []

    experiment = load_experiment(root / "experiments" / "analyze_sample.yaml")
    assert [f for f in lint_experiment(experiment) if f.level is not LintLevel.INFO] == []

    result = ExperimentRunner(tmp_path / "runs").run(experiment)
    assert result.verdict is Verdict.PASS
    assert result.manifest["mode"] == "analyze" and result.manifest["events"]["count"] == 6
    events = read_events_jsonl(result.bundle_dir / "normalized_events.jsonl")
    assert check_events_against_catalog(events, adapter.event_catalog()) == []


def test_generated_test_suite_passes(tmp_path):
    root = create_adapter_project("scaffold_suite", tmp_path)
    proc = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider"],
                          cwd=root, capture_output=True, text=True, timeout=300)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "4 passed" in proc.stdout


def test_bad_lines_become_diagnostics_not_crashes(tmp_path, monkeypatch):
    root = create_adapter_project("scaffold_noise", tmp_path)
    monkeypatch.syspath_prepend(str(root))
    sample = root / "examples" / "sample.log"
    sample.write_text(sample.read_text(encoding="utf-8") + "garbage line\n0.002 worker bad..type x=1\n",
                      encoding="utf-8")
    result = ExperimentRunner(tmp_path / "runs").run(load_experiment(root / "experiments" / "analyze_sample.yaml"))
    assert result.verdict is Verdict.PASS
    assert result.manifest["events"]["parse_diagnostics"] == 2


@pytest.mark.parametrize("name", ["My-Adapter", "1abc", "class", "crosslayer", "a", "trailing_", "dou__ble"])
def test_invalid_names(tmp_path, name):
    with pytest.raises(ConfigError):
        create_adapter_project(name, tmp_path)


def test_refuses_to_overwrite(tmp_path):
    create_adapter_project("taken", tmp_path)
    with pytest.raises(ConfigError, match="already exists"):
        create_adapter_project("taken", tmp_path)


def test_class_name():
    assert class_name("rv32i_core") == "Rv32iCoreAdapter"


def test_cli(tmp_path, capsys):
    assert main(["new", "adapter", "cli_made", "--dir", str(tmp_path)]) == 0
    assert (tmp_path / "cli_made" / "cli_made" / "adapter.py").is_file()
    assert main(["new", "adapter", "cli_made", "--dir", str(tmp_path)]) == 4
