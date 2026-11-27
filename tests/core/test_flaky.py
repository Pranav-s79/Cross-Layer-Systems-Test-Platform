"""Flaky contracts: verdicts that differ between runs of the same experiment, seed, and config hash."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from crosslayer.cli import main
from crosslayer.core.errors import ConfigError
from crosslayer.core.results import RunIndex, find_flaky, render_flaky, repeated_series

from .test_regressions import EXPERIMENTS, run


def test_agreeing_repeats_are_not_flaky():
    runs = [run(n, contracts={"a": ("pass", None)}) for n in (1, 2, 3)]
    assert find_flaky(runs) == [] and repeated_series(runs) == 1


def test_contract_disagreeing_between_repeats_is_flaky_with_counts():
    runs = [run(1, contracts={"a": ("pass", None), "b": ("pass", None)}),
            run(2, "fail", {"a": ("fail", 5), "b": ("pass", None)}),
            run(3, contracts={"a": ("pass", None), "b": ("pass", None)}),
            run(4, "fail", {"a": ("fail", 5), "b": ("pass", None)})]
    by_subject = {f.subject: f for f in find_flaky(runs)}
    assert set(by_subject) == {"run", "a"}
    a = by_subject["a"]
    assert (a.counts, a.flips, a.run_count, a.latest_run, a.latest_verdict) == ({"fail": 2, "pass": 2}, 3, 4, "r4", "fail")
    assert (a.first_flip_run, [s["run_id"] for s in a.runs]) == ("r2", ["r1", "r2", "r3", "r4"])
    text = render_flaky(find_flaky(runs), series=1)
    assert "a: 2 fail, 2 pass over 4 runs, 3 flip(s)" in text and "2 flaky" in text


def test_run_order_does_not_matter():
    runs = [run(2, "fail", {"a": ("fail", 5)}), run(1, contracts={"a": ("pass", None)})]
    assert find_flaky(runs)[0].first_flip_run == "r2"
    assert [f.subject for f in find_flaky(runs)] == ["run", "a"]


def test_changed_config_hash_is_not_flakiness():
    runs = [run(1, contracts={"a": ("pass", None)}), run(2, "fail", {"a": ("fail", 5)}, config="h2")]
    assert find_flaky(runs) == [] and repeated_series(runs) == 0


def test_seeds_are_compared_per_seed():
    runs = [run(1, seed=1, contracts={"a": ("pass", None)}), run(2, "fail", {"a": ("fail", 5)}, seed=2),
            run(3, seed=1, contracts={"a": ("pass", None)}), run(4, "fail", {"a": ("fail", 5)}, seed=2)]
    assert find_flaky(runs) == []
    runs.append(run(5, "fail", {"a": ("fail", 5)}, seed=1))
    assert {(f.seed, f.subject) for f in find_flaky(runs)} == {(1, "a"), (1, "run")}


def test_experiments_are_separate_and_none_seed_sorts_last():
    flip = [run(1, experiment="x", seed=None), run(2, "fail", experiment="x", seed=None)]
    flip2 = [run(3, experiment="x", seed=7), run(4, "fail", experiment="x", seed=7)]
    other = [run(5, experiment="y", seed=1), run(6, "fail", experiment="z", seed=1)]
    assert [(f.experiment, f.seed) for f in find_flaky(flip + flip2 + other)] == [("x", 7), ("x", None)]


def test_min_runs_threshold_and_validation():
    runs = [run(1), run(2, "fail")]
    assert find_flaky(runs, min_runs=3) == []
    assert repeated_series(runs, min_runs=3) == 0
    with pytest.raises(ConfigError, match="minimum runs must be at least 2"):
        find_flaky(runs, min_runs=1)


# -- command line, against a real index -------------------------------------


@pytest.fixture(scope="module")
def seeded_db(tmp_path_factory) -> Path:
    out = tmp_path_factory.mktemp("runs")
    main(["run", str(EXPERIMENTS / "baseline.yaml"), "--out", str(out), "--quiet"])
    db = out / "index.sqlite"
    with RunIndex(db, create=True) as index:
        index.add([out])
    return db


def _repeat(db: Path, tmp_path: Path, *, fail_contract: str | None) -> Path:
    copy = tmp_path / "copy.sqlite"
    copy.write_bytes(db.read_bytes())
    con = sqlite3.connect(copy)
    con.execute("INSERT INTO runs SELECT 'repeat-run', bundle_path, experiment, adapter, mode, config_hash, seed, verdict, "
                "'2999-01-01T00:00:00.000Z', finished_at, event_count, fingerprint, localization_status, faults, "
                "ground_truth, timings FROM runs")
    con.execute("INSERT INTO contract_results SELECT 'repeat-run', contract_id, version, owner, severity, verdict, "
                "first_violation_ns, summary FROM contract_results")
    if fail_contract:
        con.execute("UPDATE contract_results SET verdict = 'fail', first_violation_ns = 42 "
                    "WHERE run_id = 'repeat-run' AND contract_id = ?", (fail_contract,))
    con.commit()
    con.close()
    return copy


def test_cli_lists_flaky_contract(seeded_db, tmp_path, capsys):
    with RunIndex(seeded_db) as index:
        contract = index.known_contracts()[0]
    db = _repeat(seeded_db, tmp_path, fail_contract=contract)
    assert main(["flaky", "--db", str(db)]) == 0
    out = capsys.readouterr().out
    assert f"{contract}: 1 fail, 1 pass over 2 runs" in out and "first differed 2999-01-01T00:00:00.000Z in repeat-run" in out
    assert main(["flaky", "--db", str(db), "--fail-on-flaky"]) == 1
    capsys.readouterr()
    assert main(["flaky", "--db", str(db), "--json"]) == 0
    doc = json.loads(capsys.readouterr().out)
    assert doc["flaky"] is True and doc["series_checked"] == 1
    assert [f["subject"] for f in doc["findings"]] == [contract]  # the run verdict was copied, so it agrees


def test_cli_stable_repeats_and_errors(seeded_db, tmp_path, capsys):
    db = _repeat(seeded_db, tmp_path, fail_contract=None)
    assert main(["flaky", "--db", str(db), "--fail-on-flaky"]) == 0
    assert "1 repeated configuration(s) checked, 0 flaky" in capsys.readouterr().out
    assert main(["flaky", "--db", str(tmp_path / "missing.sqlite")]) == 4
    capsys.readouterr()
    assert main(["flaky", "--db", str(seeded_db), "--experiment", "nope"]) == 4
    assert "no runs in the index for experiment 'nope'" in capsys.readouterr().err
    assert main(["flaky", "--db", str(seeded_db), "--min-runs", "1"]) == 4
    assert "minimum runs must be at least 2" in capsys.readouterr().err
