"""The results dashboard: sections, escaping, determinism, and the CLI."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from crosslayer.cli import main
from crosslayer.core.results import render_dashboard
from crosslayer.core.results.dashboard import (
    failing_by_owner,
    health_by_day,
    latest_per_series,
    localization_by_day,
    truth_of,
)

from .test_regressions import run

EXPERIMENTS = Path(__file__).resolve().parents[2] / "experiments" / "reference"


def with_extras(r, *, owners=None, faults=None, truth=None, hyps=None, day=None):
    r = dict(r)
    if day:
        r["started_at"] = f"{day}T00:{r['run_id'][1:].zfill(2)}:00.000Z"
    for c in r["contracts"]:
        c["owner"] = (owners or {}).get(c["contract_id"])
        c["severity"] = "error"
        c["summary"] = f"summary of {c['contract_id']}"
    r["faults"], r["ground_truth"] = faults or [], truth
    r["hypotheses"] = [{"rank": i, "subsystem": s, "interface": None, "probability": 0.5}
                       for i, s in enumerate(hyps or [], start=1)]
    return r


def test_health_counts_runs_per_day_and_keeps_unknown_verdicts_visible():
    runs = [with_extras(run(1), day="2026-11-01"), with_extras(run(2, "fail"), day="2026-11-01"),
            with_extras(run(3), day="2026-11-02"), with_extras(run(4, "weird"), day="2026-11-02")]
    days, counts = health_by_day(runs)
    assert days == ["2026-11-01", "2026-11-02"]
    assert counts["pass"] == [1, 1] and counts["fail"] == [1, 0] and counts["inconclusive"] == [0, 1]


def test_failing_contracts_come_from_the_latest_run_grouped_by_owner():
    old = with_extras(run(1, "fail", {"a": ("fail", 5)}), owners={"a": "alice"})
    new = with_extras(run(2, "fail", {"a": ("fail", 5), "b": ("error", None), "c": ("pass", None)}),
                      owners={"a": "alice", "b": None})
    other = with_extras(run(3, "fail", {"d": ("fail", 1)}, seed=2), owners={"d": "bob"})
    latest = latest_per_series([old, new, other])
    assert [r["run_id"] for r in latest] == ["r2", "r3"]
    owners = failing_by_owner(latest)
    assert {o: [c["contract_id"] for c in cs] for o, cs in owners.items()} == {
        "(no owner)": ["b"], "alice": ["a"], "bob": ["d"]}


def test_truth_prefers_ground_truth_then_first_labeled_fault():
    assert truth_of(with_extras(run(1))) is None
    fault = {"target_subsystem": "tx", "target_interface": "tx->rx"}
    assert truth_of(with_extras(run(1), faults=[{"kind": "x"}, fault])) == ("tx", "tx->rx")
    assert truth_of(with_extras(run(1), faults=[fault], truth={"target_subsystem": "rx"})) == ("rx", None)


def test_localization_scores_top1_and_top3_per_day_and_skips_unscorable_runs():
    truth = {"target_subsystem": "tx"}
    runs = [with_extras(run(1), truth=truth, hyps=["tx", "rx"], day="2026-11-01"),
            with_extras(run(2), truth=truth, hyps=["rx", "tx"], day="2026-11-01"),
            with_extras(run(3), truth=truth, hyps=["rx", "q", "z"], day="2026-11-02"),
            with_extras(run(4), hyps=["tx"], day="2026-11-02"),          # no truth
            with_extras(run(5), truth=truth, hyps=[], day="2026-11-02")]  # no localization
    assert localization_by_day(runs) == [{"day": "2026-11-01", "scored": 2, "top1": 1, "top3": 2},
                                         {"day": "2026-11-02", "scored": 1, "top1": 0, "top3": 0}]


def test_page_has_every_section_and_is_self_contained():
    runs = [with_extras(run(1, contracts={"a": ("pass", None)}), owners={"a": "alice"}, day="2026-11-01"),
            with_extras(run(2, "fail", {"a": ("fail", 5)}), owners={"a": "alice"}, day="2026-11-02",
                        truth={"target_subsystem": "tx"}, hyps=["tx"])]
    page = render_dashboard(runs)
    for heading in ("Suite health", "Failing contracts by owner", "Regressions", "Flaky contracts", "Localization accuracy"):
        assert f"<h2>{heading}</h2>" in page
    assert "<code>a</code>" in page and "alice" in page and "run verdict pass -&gt; fail" in page
    assert "<script" not in page and not re.search(r"(src|href)=", page) and "http" not in page.replace("http-equiv", "")
    assert "<title>CrossLayer dashboard</title>" in page
    assert render_dashboard(runs) == page and render_dashboard(list(reversed(runs))) == page


def test_empty_sections_say_so():
    page = render_dashboard([with_extras(run(1, contracts={"a": ("pass", None)}))])
    for text in ("No failing contracts in the latest runs.", "No regressions.", "Nothing flaky.",
                 "No indexed run has both a labeled fault and a localization."):
        assert text in page


def test_flaky_rows_and_untrusted_text_are_escaped():
    a = with_extras(run(1, contracts={"<b>x</b>": ("pass", None)}), owners={"<b>x</b>": "<i>me</i>"})
    b = with_extras(run(2, "fail", {"<b>x</b>": ("fail", 5)}), owners={"<b>x</b>": "<i>me</i>"})
    page = render_dashboard([a, b], title="<script>x</script>")
    assert "<b>x</b>" not in page and "<i>me</i>" not in page and "<script>x" not in page
    assert "&lt;b&gt;x&lt;/b&gt;" in page and "2 flaky." in page


@pytest.fixture(scope="module")
def real_db(tmp_path_factory) -> Path:
    out = tmp_path_factory.mktemp("dash")
    for name in ("baseline.yaml", "fault_silent_drop.yaml"):
        main(["run", str(EXPERIMENTS / name), "--out", str(out / "runs"), "--quiet"])
    db = out / "index.sqlite"
    assert main(["index", "add", str(out / "runs"), "--db", str(db)]) == 0
    return db


def test_cli_writes_dashboard_from_real_bundles(real_db, tmp_path, capsys):
    out = tmp_path / "sub" / "dash.html"
    assert main(["dashboard", "--db", str(real_db), "--out", str(out), "--title", "Ref"]) == 0
    assert "2 run(s)" in capsys.readouterr().out
    page = out.read_text()
    assert "<title>Ref</title>" in page and "sensor-pipeline-silent-drop" in page
    assert "Overall: 1/1 top-1" in page  # the labeled fault run is localized
    assert main(["dashboard", "--db", str(real_db), "--out", str(out), "--experiment", "nope"]) == 4


def test_cli_without_an_index_or_with_an_unwritable_target_is_a_config_error(real_db, tmp_path, capsys):
    assert main(["dashboard", "--db", str(tmp_path / "missing.sqlite"), "--out", str(tmp_path / "d.html")]) == 4
    blocker = tmp_path / "file"
    blocker.write_text("x")
    assert main(["dashboard", "--db", str(real_db), "--out", str(blocker / "d.html")]) == 4
    assert "cannot write the dashboard" in capsys.readouterr().err
