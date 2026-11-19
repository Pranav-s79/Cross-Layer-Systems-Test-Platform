"""Fault localization: ranking, counter-evidence, calibration, ground truth, and scoring."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from crosslayer.cli import main
from crosslayer.core.adapters.base import AdapterDescription, InterfaceSpec
from crosslayer.core.contracts.loader import parse_contract
from crosslayer.core.contracts.model import ContractResult, Finding, FindingKind
from crosslayer.core.diagnosis import localize
from crosslayer.core.diagnosis.scoring import score_paths, summarize
from crosslayer.core.errors import ConfigError
from crosslayer.core.orchestrator import ExperimentRunner
from crosslayer.core.verdict import Verdict
from tests.core.test_runner import FakeAdapter, experiment

# a -> b -> c, plus b -> a (opposite direction), d feeds a and logs nothing
DESCRIPTION = AdapterDescription(
    "demo", "1", "demo", "1", ("a", "b", "c", "d"),
    interfaces=(InterfaceSpec("ab", "a", "b"), InterfaceSpec("ba", "b", "a"), InterfaceSpec("bc", "b", "c"),
                InterfaceSpec("da", "d", "a")),
)


def result(cid: str, source: str, destination: str, verdict: Verdict, at: int | None = None,
           severity: str = "critical") -> ContractResult:
    contract = parse_contract({
        "id": cid, "title": cid, "owner": "t", "source": source, "destination": destination, "severity": severity,
        "rule": {"kind": "count", "select": {"event_type": "x"}, "min": 1},
    }, "test")
    findings = [Finding(FindingKind.VIOLATION, "broken", timestamp_ns=at)] if verdict is Verdict.FAIL else []
    return ContractResult(contract, verdict, "s", findings)


def ranked(loc) -> list[str]:
    return [h.subsystem for h in loc.hypotheses]


def test_no_failure_gives_no_hypotheses():
    loc = localize(DESCRIPTION, [result("t.ok", "a", "b", Verdict.PASS)])
    assert loc.status == "no_failures" and loc.hypotheses == []


def test_source_outranks_destination_and_probabilities_sum_to_one():
    loc = localize(DESCRIPTION, [result("t.ab", "a", "b", Verdict.FAIL, 10)])
    assert ranked(loc)[:2] == ["a", "b"]
    assert abs(sum(h.probability for h in loc.hypotheses) - 1) < 1e-9
    assert loc.to_dict() == localize(DESCRIPTION, [result("t.ab", "a", "b", Verdict.FAIL, 10)]).to_dict()


def test_earliest_violation_dominates_later_consequences():
    loc = localize(DESCRIPTION, [
        result("t.late", "b", "c", Verdict.FAIL, 50),
        result("t.early", "a", "b", Verdict.FAIL, 10),
    ])
    assert ranked(loc)[0] == "a"


def test_silent_upstream_component_is_reached_through_the_graph():
    loc = localize(DESCRIPTION, [result("t.ab", "a", "b", Verdict.FAIL, 10)])
    d = next(h for h in loc.hypotheses if h.subsystem == "d")
    assert d.evidence[0]["role"] == "upstream (1 hop)" and d.interface == "da"
    assert ranked(loc).index("d") > ranked(loc).index("a")


def test_passing_contracts_are_counter_evidence():
    failing = result("t.ab", "a", "b", Verdict.FAIL, 10, severity="major")
    alone = localize(DESCRIPTION, [failing])
    with_pass = localize(DESCRIPTION, [failing, result("t.da", "d", "a", Verdict.PASS),
                                       result("t.a2", "a", "c", Verdict.PASS)])
    a_alone = next(h for h in alone.hypotheses if h.subsystem == "a")
    a_pass = next(h for h in with_pass.hypotheses if h.subsystem == "a")
    assert a_pass.score < a_alone.score
    assert {c["contract"] for c in a_pass.counter_evidence} == {"t.da", "t.a2"}


def test_interface_follows_the_contract_direction():
    loc = localize(DESCRIPTION, [result("t.ab", "a", "b", Verdict.FAIL, 10)])
    assert loc.hypotheses[0].interface == "ab"  # not "ba", which joins the same pair the other way


def test_same_evidence_for_the_top_two_is_ambiguous_distinct_evidence_can_be_confident():
    single = localize(DESCRIPTION, [result("t.ab", "a", "b", Verdict.FAIL, 10)])
    assert single.status == "ambiguous" and "same failing contracts" in single.reason and "directly" in single.reason
    many = localize(DESCRIPTION, [
        result("t.ab", "a", "b", Verdict.FAIL, 10), result("t.ac", "a", "c", Verdict.FAIL, 10),
        result("t.bc", "b", "c", Verdict.PASS), result("t.ba", "b", "a", Verdict.PASS),
    ])
    assert ranked(many)[0] == "a" and many.status == "confident"


def test_inconclusive_contracts_are_listed_as_not_weighed():
    loc = localize(DESCRIPTION, [result("t.ab", "a", "b", Verdict.FAIL, 10),
                                 result("t.maybe", "b", "c", Verdict.INCONCLUSIVE)])
    assert loc.unresolved == ["t.maybe"]


# -- ground truth ----------------------------------------------------------------


@pytest.mark.parametrize("truth, match", [
    ({"interface": "x"}, "subsystem: must be a non-empty string"),
    ({"subsystem": "server", "where": "?"}, "unknown keys"),
    ("server", "must be a mapping"),
])
def test_ground_truth_shape_is_validated(tmp_path, truth, match):
    with pytest.raises(ConfigError, match=match):
        experiment(tmp_path, ground_truth=truth)


def test_ground_truth_must_name_a_declared_subsystem(tmp_path):
    runner = ExperimentRunner(tmp_path / "runs", adapter_loader=lambda ref: FakeAdapter())
    with pytest.raises(ConfigError, match="is not a subsystem"):
        runner.plan(experiment(tmp_path, ground_truth={"subsystem": "gpu"}))


def test_ground_truth_is_recorded_but_not_part_of_the_config_hash(tmp_path):
    labeled = experiment(tmp_path, ground_truth={"subsystem": "server", "note": "slow"})
    assert labeled.config_hash() == experiment(tmp_path).config_hash()
    run = ExperimentRunner(tmp_path / "runs", adapter_loader=lambda ref: FakeAdapter()).run(labeled)
    assert run.manifest["ground_truth"] == {"target_subsystem": "server", "target_interface": None, "note": "slow"}


# -- runs and scoring ----------------------------------------------------------------


def test_every_run_writes_its_localization_and_report_section(tmp_path):
    run = ExperimentRunner(tmp_path / "runs", adapter_loader=lambda ref: FakeAdapter()).run(
        experiment(tmp_path, faults=["lose_ack"]))
    loc = json.loads((run.bundle_dir / "localization.json").read_text(encoding="utf-8"))
    assert loc["status"] in {"confident", "ambiguous"} and loc["hypotheses"]
    assert run.manifest["localization"]["top"][0]["subsystem"] == loc["hypotheses"][0]["subsystem"]
    assert "## Fault localization" in (run.bundle_dir / "report.md").read_text(encoding="utf-8")


def _bundle(root: Path, name: str, truth: dict | None, hyps: list[str], faults=None) -> Path:
    bundle = root / name
    bundle.mkdir(parents=True)
    manifest = {"experiment": {"name": name}, "faults": faults or [], "ground_truth": truth}
    (bundle / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    loc = {"status": "ambiguous", "hypotheses": [
        {"subsystem": s, "interface": f"{s}_if", "probability": 1 / len(hyps)} for s in hyps]}
    (bundle / "localization.json").write_text(json.dumps(loc), encoding="utf-8")
    return bundle


def test_scoring_ranks_truth_and_skips_unlabeled_bundles(tmp_path):
    _bundle(tmp_path, "hit", {"target_subsystem": "a", "target_interface": "a_if"}, ["a", "b"])
    _bundle(tmp_path, "third", None, ["x", "y", "z"], faults=[{"target_subsystem": "z", "target_interface": None}])
    _bundle(tmp_path, "miss", {"target_subsystem": "q", "target_interface": None}, ["a", "b", "c", "d"])
    _bundle(tmp_path, "unlabeled", None, ["a"])
    runs = {r.experiment: r for r in score_paths([tmp_path])}
    assert set(runs) == {"hit", "third", "miss"}
    assert (runs["hit"].rank, runs["hit"].interface_hit) == (1, True)
    assert runs["third"].rank == 3 and runs["miss"].rank is None
    summary = summarize(list(runs.values()))
    assert summary["top1"]["hits"] == 1 and summary["top3"]["hits"] == 2 and summary["interface_top1"]["of"] == 1


def test_score_command_gates_on_minimum_accuracy(tmp_path, capsys):
    _bundle(tmp_path, "hit", {"target_subsystem": "a", "target_interface": None}, ["a"])
    _bundle(tmp_path, "miss", {"target_subsystem": "q", "target_interface": None}, ["a"])
    assert main(["localize", str(tmp_path), "--score", "--min-top3", "0.5"]) == 0
    assert main(["localize", str(tmp_path), "--score", "--min-top1", "0.9"]) == 1
    assert "GATE      top1 0.5 is below the minimum 0.9" in capsys.readouterr().out


def test_localize_one_bundle_and_reject_old_bundles(tmp_path, capsys):
    run = ExperimentRunner(tmp_path / "runs", adapter_loader=lambda ref: FakeAdapter()).run(
        experiment(tmp_path, faults=["lose_ack"]))
    assert main(["localize", str(run.bundle_dir)]) == 0
    assert "1. " in capsys.readouterr().out
    (run.bundle_dir / "localization.json").unlink()
    assert main(["localize", str(run.bundle_dir)]) == 4
