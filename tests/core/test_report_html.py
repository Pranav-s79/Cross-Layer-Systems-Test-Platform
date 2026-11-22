"""The localization step of report.html: ranked hypotheses, evidence links, older bundles."""

from __future__ import annotations

import json

from crosslayer.cli import main
from crosslayer.core.artifacts.report_html import render_html
from crosslayer.core.diagnosis import localize
from crosslayer.core.verdict import Verdict
from tests.core.test_localize import DESCRIPTION, result
from tests.core.test_runner import FakeAdapter, experiment, runner


def _manifest(verdict: str = "fail") -> dict:
    return {
        "run_id": "r1", "verdict": verdict, "mode": "run", "config_hash": "0" * 16,
        "experiment": {"name": "demo", "seed": 1},
        "adapter": {"name": "fake", "adapter_version": "1", "system": "demo"},
    }


def _results():
    return [
        result("bc.count", "b", "c", Verdict.FAIL, at=100),
        result("ab.ok", "a", "b", Verdict.PASS),
        result("ab.unsure", "a", "b", Verdict.INCONCLUSIVE),
    ]


def test_localization_step_ranks_hypotheses_and_links_contracts():
    results = _results()
    loc = localize(DESCRIPTION, results).to_dict()
    page = render_html(_manifest(), results, None, loc)
    assert page.count("data-step-panel=") == 6 and "--steps:6" in page
    assert "Step 5 of 6" in page and "Locate the fault" in page
    assert "#1 <code>b</code>" in page
    # evidence and counter-evidence link to rows of the contract table
    assert 'data-contract-link="contract-bc_2e_count"' in page
    assert 'id="contract-bc_2e_count"' in page and 'id="contract-ab_2e_ok"' in page
    assert "passed (" in page and "Not weighed (inconclusive)" in page


def test_localization_values_are_escaped():
    loc = {"method": "m", "status": "confident", "reason": "<b>x</b>", "hypotheses": [
        {"subsystem": "<i>", "interface": None, "probability": 1.0,
         "evidence": [{"contract": "<c>", "role": "source", "boundary": "<i> -> y"}], "counter_evidence": []}]}
    page = render_html(_manifest(), [], None, loc)
    assert "<b>x</b>" not in page and "<i>" not in page.split("<main", 1)[1].replace("<i></i>", "")
    assert "&lt;b&gt;x&lt;/b&gt;" in page and "&lt;i&gt;" in page


def test_run_without_failures_says_nothing_to_rank():
    loc = localize(DESCRIPTION, [result("ab.ok", "a", "b", Verdict.PASS)]).to_dict()
    page = render_html(_manifest("pass"), [], None, loc)
    assert "nothing to rank" in page and "no_failures" in page


def test_report_without_localization_keeps_five_steps():
    page = render_html(_manifest(), _results(), None)
    assert page.count("data-step-panel=") == 5 and "--steps:5" in page
    assert "Locate the fault" not in page and "Step 5 of 5" in page


def test_runner_writes_localization_step(tmp_path):
    res = runner(tmp_path, FakeAdapter()).run(experiment(tmp_path))
    page = (res.bundle_dir / "report.html").read_text(encoding="utf-8")
    assert page.count("data-step-panel=") == 6 and "Locate the fault" in page


def test_show_prints_top_three_and_tolerates_older_bundles(tmp_path, capsys):
    res = runner(tmp_path, FakeAdapter()).run(experiment(tmp_path))
    loc = {"method": "m", "status": "ambiguous", "hypotheses": [
        {"subsystem": s, "interface": None, "probability": p, "evidence": [], "counter_evidence": []}
        for s, p in (("a", 0.4), ("b", 0.3), ("c", 0.2), ("d", 0.1))]}
    (res.bundle_dir / "localization.json").write_text(json.dumps(loc), encoding="utf-8")
    capsys.readouterr()
    assert main(["show", str(res.bundle_dir)]) == 0
    out = capsys.readouterr().out
    assert "likely origin (ambiguous):" in out and "3. c" in out and "4. d" not in out
    assert main(["show", str(res.bundle_dir), "--json"]) == 0
    assert len(json.loads(capsys.readouterr().out)["localization"]["top"]) == 3
    (res.bundle_dir / "localization.json").unlink()
    assert main(["show", str(res.bundle_dir)]) == 0
    assert "likely origin" not in capsys.readouterr().out
    assert main(["show", str(res.bundle_dir), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["localization"] is None
