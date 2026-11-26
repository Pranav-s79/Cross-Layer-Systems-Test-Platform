"""The SQLite run index: what it records, idempotence, queries, and refusals."""

from __future__ import annotations

import json
import shutil
import sqlite3
from pathlib import Path

import pytest

from crosslayer.cli import main
from crosslayer.core.errors import ConfigError
from crosslayer.core.results import SCHEMA_VERSION, RunIndex, parse_since

EXPERIMENTS = Path(__file__).resolve().parents[2] / "experiments" / "reference"


@pytest.fixture(scope="module")
def runs_dir(tmp_path_factory) -> Path:
    """A baseline run and a silent-drop run (FAIL, with a fault label), run once."""
    out = tmp_path_factory.mktemp("runs")
    for name in ("baseline.yaml", "fault_silent_drop.yaml"):
        main(["run", str(EXPERIMENTS / name), "--out", str(out), "--quiet"])
    return out


def _bundle(runs_dir: Path, experiment: str) -> Path:
    for manifest in runs_dir.glob("*/manifest.json"):
        if json.loads(manifest.read_text())["experiment"]["name"] == experiment:
            return manifest.parent
    raise AssertionError(experiment)


@pytest.fixture
def index(tmp_path, runs_dir):
    with RunIndex(tmp_path / "index.sqlite", create=True) as idx:
        idx.add([runs_dir])
        yield idx


def test_records_runs_results_and_hypotheses(index, runs_dir):
    bundle = _bundle(runs_dir, "sensor-pipeline-silent-drop")
    manifest = json.loads((bundle / "manifest.json").read_text())
    contracts = json.loads((bundle / "contracts.json").read_text())["contracts"]
    run = index.run(manifest["run_id"])
    assert run is not None
    assert (run["experiment"], run["verdict"], run["seed"]) == ("sensor-pipeline-silent-drop", "fail", 1234)
    assert run["config_hash"] == manifest["config_hash"] and run["fingerprint"] == manifest["events"]["fingerprint"]
    assert run["faults"] == manifest["faults"] and run["faults"][0]["target_subsystem"] == "sensor_rx"
    assert run["ground_truth"] is None
    assert set(run["timings"]) >= {"plan", "execute", "evaluate"}
    assert [c["contract_id"] for c in run["contracts"]] == sorted(c["result"]["contract_id"] for c in contracts)
    failed = {c["result"]["contract_id"]: c["result"]["first_violation_ns"]
              for c in contracts if c["result"]["verdict"] == "fail"}
    assert failed and {c["contract_id"]: c["first_violation_ns"] for c in run["contracts"]
                       if c["verdict"] == "fail"} == failed
    assert [(h["subsystem"], h["interface"], h["probability"]) for h in run["hypotheses"]] == [
        (h["subsystem"], h["interface"], h["probability"]) for h in manifest["localization"]["top"]]
    assert len(run["hypotheses"]) == 3


def test_adding_twice_is_a_no_op(tmp_path, runs_dir):
    with RunIndex(tmp_path / "i.sqlite", create=True) as idx:
        first = idx.add([runs_dir, _bundle(runs_dir, "sensor-pipeline-baseline")])
        assert [o.added for o in first] == [True, True]
        again = idx.add([runs_dir])
        assert [o.added for o in again] == [False, False]
        assert len(idx.runs()) == 2
        count = idx._db.execute("SELECT count(*) FROM contract_results").fetchone()[0]
        idx.add([runs_dir])
        assert idx._db.execute("SELECT count(*) FROM contract_results").fetchone()[0] == count


def test_same_run_id_twice_in_one_call_is_indexed_once(tmp_path, runs_dir):
    source = _bundle(runs_dir, "sensor-pipeline-baseline")
    copy = tmp_path / "copies" / "again"
    shutil.copytree(source, copy)
    with RunIndex(tmp_path / "i.sqlite", create=True) as idx:
        outcomes = idx.add([source, copy])
        assert [o.added for o in outcomes] == [True, False]
        assert len(idx.runs()) == 1


def test_diagnosis_follow_up_runs_are_not_indexed(tmp_path, runs_dir):
    source = _bundle(runs_dir, "sensor-pipeline-silent-drop")
    root = tmp_path / "root"
    shutil.copytree(source, root / "b")
    shutil.copytree(source, root / "b.diagnosis" / "variant")
    with RunIndex(tmp_path / "i.sqlite", create=True) as idx:
        assert len(idx.add([root])) == 1


def test_runs_filters_and_order(index):
    runs = index.runs()
    assert [r["started_at"] for r in runs] == sorted((r["started_at"] for r in runs), reverse=True)
    assert {r["experiment"] for r in index.runs(verdict="fail")} == {"sensor-pipeline-silent-drop"}
    assert [r["experiment"] for r in index.runs(experiment="sensor-pipeline-baseline")] == ["sensor-pipeline-baseline"]
    assert len(index.runs(limit=1)) == 1
    assert index.runs(since=parse_since("2999-01-01")) == []
    assert len(index.runs(since=parse_since("2000-01-01"))) == 2
    newest = runs[0]["started_at"]
    assert [r["run_id"] for r in index.runs(since=newest)] == [runs[0]["run_id"]]  # inclusive


def test_history_lists_one_contract_over_time(index):
    rows = index.history("pipeline.rx_accounted")
    assert {r["experiment"]: r["verdict"] for r in rows} == {
        "sensor-pipeline-baseline": "pass", "sensor-pipeline-silent-drop": "fail"}
    fail = next(r for r in rows if r["verdict"] == "fail")
    assert fail["first_violation_ns"] > 0 and fail["run_verdict"] == "fail"
    assert [r["started_at"] for r in rows] == sorted((r["started_at"] for r in rows), reverse=True)
    assert [r["experiment"] for r in index.history("pipeline.rx_accounted", experiment="sensor-pipeline-baseline")] == [
        "sensor-pipeline-baseline"]
    assert index.history("pipeline.rx_accounted", experiment="nope") == []
    assert len(index.history("pipeline.rx_accounted", limit=1)) == 1
    assert "pipeline.rx_accounted" in index.known_contracts()


@pytest.mark.parametrize("text, expected", [
    ("2026-11-01", "2026-11-01T00:00:00.000Z"),
    ("2026-11-01T12:30", "2026-11-01T12:30:00.000Z"),
    ("2026-11-01T12:30:15.250Z", "2026-11-01T12:30:15.250Z"),
    ("2026-11-01T12:00:00-06:00", "2026-11-01T18:00:00.000Z"),
])
def test_parse_since(text, expected):
    assert parse_since(text) == expected


def test_parse_since_rejects_garbage():
    with pytest.raises(ConfigError, match="--since 'yesterday'"):
        parse_since("yesterday")


def test_missing_path_and_empty_directory(tmp_path):
    with RunIndex(tmp_path / "i.sqlite", create=True) as idx:
        with pytest.raises(ConfigError, match="no such file"):
            idx.add([tmp_path / "missing"])
        (tmp_path / "empty").mkdir()
        with pytest.raises(ConfigError, match="no run bundles found"):
            idx.add([tmp_path / "empty"])
        (tmp_path / "file.txt").write_text("x")
        with pytest.raises(ConfigError, match="not a run bundle"):
            idx.add([tmp_path / "file.txt"])


def test_a_bad_bundle_adds_nothing(tmp_path, runs_dir):
    good = _bundle(runs_dir, "sensor-pipeline-baseline")
    bad = tmp_path / "bad"
    shutil.copytree(good, bad)
    (bad / "manifest.json").write_text("{not json")
    with RunIndex(tmp_path / "i.sqlite", create=True) as idx:
        with pytest.raises(ConfigError, match="unreadable manifest"):
            idx.add([good, bad])
        assert idx.runs() == []


@pytest.mark.parametrize("edit, message", [
    (lambda m: m.pop("run_id"), "no 'run_id'"),
    (lambda m: m.pop("verdict"), "no 'verdict'"),
    (lambda m: m.pop("config_hash"), "no 'config_hash'"),
    (lambda m: m["experiment"].pop("name"), "no experiment name"),
])
def test_manifest_missing_fields_are_named(tmp_path, runs_dir, edit, message):
    bad = tmp_path / "bad"
    shutil.copytree(_bundle(runs_dir, "sensor-pipeline-baseline"), bad)
    manifest = json.loads((bad / "manifest.json").read_text())
    edit(manifest)
    (bad / "manifest.json").write_text(json.dumps(manifest))
    with RunIndex(tmp_path / "i.sqlite", create=True) as idx, pytest.raises(ConfigError, match=message):
        idx.add([bad])


def test_bundle_without_contracts_or_with_broken_json(tmp_path, runs_dir):
    bad = tmp_path / "bad"
    shutil.copytree(_bundle(runs_dir, "sensor-pipeline-baseline"), bad)
    (bad / "timings.json").write_text("[")
    with RunIndex(tmp_path / "i.sqlite", create=True) as idx:
        with pytest.raises(ConfigError, match="unreadable timings.json"):
            idx.add([bad])
        (bad / "timings.json").unlink()  # optional: older bundles
        assert idx.add([bad])[0].added and idx.runs()[0]["timings"] is None
    (bad / "contracts.json").unlink()
    with RunIndex(tmp_path / "j.sqlite", create=True) as idx, pytest.raises(ConfigError, match="no contracts.json"):
        idx.add([bad])


def test_schema_version_is_checked(tmp_path):
    path = tmp_path / "i.sqlite"
    RunIndex(path, create=True).close()
    assert sqlite3.connect(path).execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    db = sqlite3.connect(path)
    db.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 1}")
    db.commit()
    db.close()
    with pytest.raises(ConfigError, match="schema version 2, this CrossLayer reads version 1"):
        RunIndex(path)


def test_refuses_foreign_databases(tmp_path):
    other = tmp_path / "other.sqlite"
    db = sqlite3.connect(other)
    db.execute("CREATE TABLE notes (x)")
    db.commit()
    db.close()
    with pytest.raises(ConfigError, match="schema version 0"):
        RunIndex(other, create=True)
    junk = tmp_path / "junk.sqlite"
    junk.write_bytes(b"this is not a database" * 10)
    with pytest.raises(ConfigError, match="not a usable index database"):
        RunIndex(junk)


def test_reading_never_creates_the_database(tmp_path):
    with pytest.raises(ConfigError, match="no run index"):
        RunIndex(tmp_path / "nope" / "i.sqlite")
    assert not (tmp_path / "nope").exists()


# -- CLI ---------------------------------------------------------------

def test_cli_add_runs_history(tmp_path, runs_dir, capsys):
    db = str(tmp_path / "i.sqlite")
    assert main(["index", "add", str(runs_dir), "--db", db]) == 0
    assert "2 added, 0 already indexed" in capsys.readouterr().out
    assert main(["index", "add", str(runs_dir), "--db", db, "--json"]) == 0
    doc = json.loads(capsys.readouterr().out)
    assert (doc["added"], doc["unchanged"]) == (0, 2)

    assert main(["index", "runs", "--db", db, "--verdict", "fail", "--json"]) == 0
    [run] = json.loads(capsys.readouterr().out)["runs"]
    assert run["experiment"] == "sensor-pipeline-silent-drop"

    assert main(["index", "runs", "--db", db, "--since", "2999-01-01"]) == 0
    assert "0 run(s)" in capsys.readouterr().out

    assert main(["history", "pipeline.rx_accounted", "--db", db]) == 0
    out = capsys.readouterr().out
    assert "fail" in out and "first violation" in out and "2 result(s)" in out
    assert main(["history", "pipeline.rx_accounted", "--db", db, "--experiment", "x", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["results"] == []


def test_cli_errors_exit_4(tmp_path, runs_dir, capsys):
    db = str(tmp_path / "i.sqlite")
    assert main(["history", "x", "--db", db]) == 4
    assert "no run index" in capsys.readouterr().err
    main(["index", "add", str(runs_dir), "--db", db])
    capsys.readouterr()
    assert main(["history", "no.such.contract", "--db", db]) == 4
    assert "no contract 'no.such.contract'" in capsys.readouterr().err
    assert main(["index", "runs", "--db", db, "--since", "soon"]) == 4
    assert main(["index", "add", str(tmp_path / "missing"), "--db", db]) == 4
    with pytest.raises(SystemExit):
        main(["index", "runs", "--db", db, "--limit", "0"])


def test_ground_truth_is_stored_as_recorded(tmp_path, runs_dir):
    bundle = tmp_path / "b"
    shutil.copytree(_bundle(runs_dir, "sensor-pipeline-baseline"), bundle)
    manifest = json.loads((bundle / "manifest.json").read_text())
    manifest["ground_truth"] = {"target_subsystem": "proc", "target_interface": None}
    (bundle / "manifest.json").write_text(json.dumps(manifest))
    with RunIndex(tmp_path / "i.sqlite", create=True) as idx:
        idx.add([bundle])
        assert idx.runs()[0]["ground_truth"] == {"target_subsystem": "proc", "target_interface": None}
