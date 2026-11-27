"""Regression analysis: flips, newly failing contracts, timing and first-violation shifts, first appearance."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from crosslayer.cli import main
from crosslayer.core.errors import ConfigError
from crosslayer.core.results import RunIndex, Tolerances, analyze, has_regression, render

EXPERIMENTS = Path(__file__).resolve().parents[2] / "experiments" / "reference"


def run(n: int, verdict="pass", contracts=None, timings=None, *, experiment="exp", seed=1, config="h1"):
    """A run dict as RunIndex.runs() returns it; run n starts at minute n."""
    return {"run_id": f"r{n}", "experiment": experiment, "seed": seed, "config_hash": config, "verdict": verdict,
            "started_at": f"2026-11-01T00:{n:02d}:00.000Z", "timings": timings,
            "contracts": [{"contract_id": cid, "verdict": v, "first_violation_ns": fv}
                          for cid, (v, fv) in (contracts or {}).items()]}


def only(runs, **kw):
    (report,) = analyze(runs, Tolerances(**kw))
    return report


def test_single_run_has_nothing_to_compare():
    report = only([run(1)])
    assert report.baseline_run is None and report.findings == [] and not has_regression([report])
    assert "no earlier run" in render([report])


def test_identical_runs_report_no_change():
    report = only([run(1, contracts={"a": ("pass", None)}), run(2, contracts={"a": ("pass", None)})])
    assert report.findings == [] and report.baseline_run == "r1"
    assert "no change" in render([report])


def test_verdict_flip_and_contract_newly_failing():
    report = only([run(1, contracts={"a": ("pass", None), "b": ("pass", None)}),
                   run(2, "fail", {"a": ("fail", 500), "b": ("pass", None)})])
    assert [(f.kind, f.severity, f.subject, f.first_seen_run) for f in report.findings] == [
        ("verdict", "regression", "run", "r2"), ("contract", "regression", "a", "r2")]
    assert "a newly failing: pass -> fail" in render([report])


def test_a_lasting_failure_links_to_the_run_where_it_began():
    runs = [run(1, contracts={"a": ("pass", None), "b": ("pass", None)}),
            run(2, contracts={"a": ("pass", None), "b": ("pass", None)}),
            run(3, "fail", {"a": ("fail", 500), "b": ("pass", None)}),
            run(4, "fail", {"a": ("fail", 500), "b": ("fail", 9)}),
            run(5, "fail", {"a": ("fail", 500), "b": ("fail", 9)})]
    by_subject = {f.subject: f for f in only(runs).findings}
    assert (by_subject["a"].first_seen_run, by_subject["a"].runs_since, by_subject["a"].before) == ("r3", 3, "pass")
    assert (by_subject["b"].first_seen_run, by_subject["b"].runs_since) == ("r4", 2)
    assert (by_subject["run"].first_seen_run, by_subject["run"].severity) == ("r3", "regression")
    assert "first seen 2026-11-01T00:03:00.000Z in r3 (3 runs)" in render([only(runs)])


def test_always_failing_contract_is_not_a_regression():
    runs = [run(1, "fail", {"a": ("fail", 500)}), run(2, "fail", {"a": ("fail", 500)})]
    assert only(runs).findings == []


def test_verdict_changes_between_bad_states_are_changes():
    report = only([run(1, contracts={"a": ("pass", None)}), run(2, "error", {"a": ("error", None)}),
                   run(3, "fail", {"a": ("fail", 10)})])
    assert {(f.kind, f.severity, f.first_seen_run) for f in report.findings} == {
        ("verdict", "change", "r3"), ("contract", "change", "r3")}
    assert not has_regression([report])


def test_recovery_is_a_change_not_a_regression():
    report = only([run(1, "fail", {"a": ("fail", 5)}), run(2, "pass", {"a": ("pass", None)})])
    assert {f.severity for f in report.findings} == {"change"}
    assert not has_regression([report])
    assert "a changed: fail -> pass" in render([report])


def test_inconclusive_is_not_a_regression_but_error_is():
    assert not has_regression(analyze([run(1), run(2, "inconclusive")]))
    assert has_regression(analyze([run(1), run(2, "error")]))
    assert has_regression(analyze([run(1, "inconclusive"), run(2, "fail")]))
    assert not has_regression(analyze([run(1, "fail"), run(2, "error")]))


def test_new_failing_contract_is_a_regression():
    report = only([run(1, contracts={"a": ("pass", None)}),
                   run(2, contracts={"a": ("pass", None), "z": ("fail", 7)})])
    (finding,) = report.findings
    assert (finding.subject, finding.severity, finding.before) == ("z", "regression", None)
    assert only([run(1), run(2, contracts={"z": ("pass", None)})]).findings == []


def test_first_violation_shift_beyond_tolerance_only():
    base = [run(1, "fail", {"a": ("fail", 1000)})]
    assert only(base + [run(2, "fail", {"a": ("fail", 1090)})]).findings == []
    report = only(base + [run(2, "fail", {"a": ("fail", 400)})])
    (finding,) = report.findings
    assert (finding.kind, finding.severity, finding.before, finding.after) == ("first_violation", "change", 1000, 400)
    assert "earlier" in finding.summary and "60%" in finding.summary
    assert only(base + [run(2, "fail", {"a": ("fail", 1090)})], violation=0.05).findings
    # Streak: r2 and r3 share the shifted time, so the shift first appeared at r2.
    runs = base + [run(2, "fail", {"a": ("fail", 400)}), run(3, "fail", {"a": ("fail", 410)}),
                   run(4, "fail", {"a": ("fail", 2000)})]
    assert only(runs).findings[0].first_seen_run == "r4"
    shifted = base + [run(2, "fail", {"a": ("fail", 2000)}), run(3, "fail", {"a": ("fail", 2010)})]
    # r2 -> r3 moved 0.5%: no finding, since each comparison is against the previous run.
    assert only(shifted).findings == []


def test_timing_needs_relative_and_absolute_slowdown():
    fast = [run(n, timings={"execute": 1.0, "plan": 0.001}) for n in (1, 2, 3)]
    assert only(fast + [run(4, timings={"execute": 1.2, "plan": 0.001})]).findings == []
    report = only(fast + [run(4, timings={"execute": 2.0, "plan": 0.04})])
    (finding,) = report.findings
    assert (finding.kind, finding.subject, finding.before, finding.after) == ("timing", "execute", 1.0, 2.0)
    assert "median of 3 earlier" in finding.summary and finding.severity == "change"
    # plan grew 40x but by 0.04 s, below the floor; lowering the floor reports it.
    assert {f.subject for f in only(fast + [run(4, timings={"execute": 1.0, "plan": 0.04})], min_seconds=0.01).findings} == {"plan"}
    assert only(fast + [run(4, timings={"execute": 1.4})], timing=0.3).findings


def test_timing_streak_and_median_resist_one_outlier():
    runs = [run(1, timings={"execute": 1.0}), run(2, timings={"execute": 5.0}), run(3, timings={"execute": 1.0}),
            run(4, timings={"execute": 1.0})]
    assert only(runs).findings == []  # median 1.0 absorbs the earlier outlier
    slow = [run(n, timings={"execute": 1.0}) for n in (1, 2, 3, 4)] + [
        run(5, timings={"execute": 3.0}), run(6, timings={"execute": 3.1})]
    (finding,) = only(slow).findings
    assert finding.first_seen_run == "r5" and finding.runs_since == 2


def test_missing_timings_are_skipped():
    assert only([run(1), run(2, timings={"execute": 9.0})]).findings == []
    assert only([run(1, timings={"execute": 1.0}), run(2)]).findings == []


def test_series_split_by_experiment_and_seed_and_flag_config_change():
    runs = [run(1, experiment="a", seed=1), run(2, "fail", experiment="a", seed=1, config="h2"),
            run(3, "fail", experiment="a", seed=2), run(4, experiment="b", seed=None)]
    a1, a2, b = analyze(runs)
    assert (a1.experiment, a1.seed, a1.config_changed, has_regression([a1])) == ("a", 1, True, True)
    assert (a2.seed, a2.baseline_run, b.seed) == (2, None, None)
    assert "[config changed" in render([a1])


def test_negative_tolerance_is_a_config_error():
    with pytest.raises(ConfigError, match="timing tolerance"):
        Tolerances(timing=-1)


# -- command line, against a real index -------------------------------------


@pytest.fixture(scope="module")
def seeded_db(tmp_path_factory) -> Path:
    out = tmp_path_factory.mktemp("runs")
    main(["run", str(EXPERIMENTS / "baseline.yaml"), "--out", str(out), "--quiet"])
    db = out / "index.sqlite"
    with RunIndex(db, create=True) as index:
        index.add([out])
    return db


def _clone_latest(db: Path, tmp_path: Path, *, verdict: str, fail_contract: str | None, started_at: str) -> Path:
    """Copy the database and add a later run of the same experiment, optionally with one contract failing."""
    copy = tmp_path / "copy.sqlite"
    copy.write_bytes(db.read_bytes())
    con = sqlite3.connect(copy)
    con.execute("INSERT INTO runs SELECT 'cloned-run', bundle_path, experiment, adapter, mode, config_hash, seed, ?, ?, "
                "finished_at, event_count, fingerprint, localization_status, faults, ground_truth, timings FROM runs",
                (verdict, started_at))
    con.execute("INSERT INTO contract_results SELECT 'cloned-run', contract_id, version, owner, severity, verdict, "
                "first_violation_ns, summary FROM contract_results")
    if fail_contract:
        con.execute("UPDATE contract_results SET verdict = 'fail', first_violation_ns = 42 "
                    "WHERE run_id = 'cloned-run' AND contract_id = ?", (fail_contract,))
    con.commit()
    con.close()
    return copy


def test_cli_reports_regression_with_exit_1(seeded_db, tmp_path, capsys):
    with RunIndex(seeded_db) as index:
        contract = index.known_contracts()[0]
    db = _clone_latest(seeded_db, tmp_path, verdict="fail", fail_contract=contract, started_at="2999-01-01T00:00:00.000Z")
    assert main(["regressions", "--db", str(db)]) == 1
    out = capsys.readouterr().out
    assert f"{contract} newly failing: pass -> fail" in out and "REGRESSION" in out and "1 series" in out
    assert main(["regressions", "--db", str(db), "--json"]) == 1
    doc = json.loads(capsys.readouterr().out)
    assert doc["regression"] is True
    (series,) = doc["series"]
    assert series["latest_run"] == "cloned-run"
    assert {f["kind"] for f in series["findings"]} == {"verdict", "contract"}
    assert all(f["first_seen_run"] == "cloned-run" for f in series["findings"])


def test_cli_no_change_exits_0(seeded_db, tmp_path, capsys):
    db = _clone_latest(seeded_db, tmp_path, verdict="pass", fail_contract=None, started_at="2999-01-01T00:00:00.000Z")
    assert main(["regressions", "--db", str(db), "--experiment", "sensor-pipeline-baseline"]) == 0
    assert "no change" in capsys.readouterr().out


def test_cli_errors(seeded_db, tmp_path, capsys):
    assert main(["regressions", "--db", str(tmp_path / "missing.sqlite")]) == 4
    assert "no run index" in capsys.readouterr().err
    assert main(["regressions", "--db", str(seeded_db), "--experiment", "nope"]) == 4
    assert "no runs in the index for experiment 'nope'" in capsys.readouterr().err
    with pytest.raises(SystemExit):
        main(["regressions", "--db", str(seeded_db), "--timing-tolerance", "-1"])
