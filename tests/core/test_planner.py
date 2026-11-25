"""Guided follow-up experiments: outcome models, expected information gain, and the diagnosis loop."""

from __future__ import annotations

import json
import math

import pytest

from crosslayer.cli import main
from crosslayer.core.diagnosis import DiagnosticAction, diagnose, expected_gain
from crosslayer.core.diagnosis.planner import NOISE, choose, entropy, in_diagnosis, update
from crosslayer.core.diagnosis.scoring import score_paths
from crosslayer.core.errors import ConfigError
from crosslayer.core.orchestrator import ExperimentRunner
from tests.core.test_runner import FakeAdapter, experiment

# -- the outcome models -------------------------------------------------------------


@pytest.mark.parametrize("kwargs, match", [
    ({"name": "Bad"}, "lowercase identifier"),
    ({"clears": {}, "observes": None}, "exactly one outcome model"),
    ({"clears": {"a": 1.0}, "observes": 0.8}, "exactly one outcome model"),
    ({"clears": {"a": 1.5}}, "in \\[0, 1\\]"),
    ({"clears": {}, "observes": 0.0}, "observes must be in"),
])
def test_actions_are_validated(kwargs, match):
    args = {"name": "swap", "description": "d", "clears": {"a": 1.0}, **kwargs}
    with pytest.raises(ValueError, match=match):
        DiagnosticAction(**args)


def test_clearing_likelihoods_are_clipped_and_prefer_the_interface_key():
    action = DiagnosticAction("space", "d", clears={"h/bypass": 1.0, "h/control": 0.0, "h": 0.75})
    table = action.likelihoods(["h", "alu"], {"h": "bypass", "alu": None})
    assert table["pass"] == {"h": 1 - NOISE, "alu": NOISE}
    assert action.likelihoods(["h"], {"h": "control"})["pass"]["h"] == NOISE
    assert action.likelihoods(["h"])["pass"]["h"] == 0.75  # no interface: the plain key


def test_observing_likelihoods_are_a_proper_distribution_per_suspect():
    table = DiagnosticAction("look", "d", observes=0.8).likelihoods(["a", "b", "c"])
    for suspect in "abc":
        assert math.isclose(sum(table[o][suspect] for o in table), 1.0)
    assert table["a"]["a"] > table["b"]["a"]


def test_bayes_update_and_entropy():
    posterior = update({"a": 0.5, "b": 0.5}, {"a": 0.9, "b": 0.1})
    assert math.isclose(posterior["a"], 0.9)
    assert math.isclose(entropy({"a": 0.5, "b": 0.5}), 1.0)


def test_expected_gain_rewards_actions_that_split_the_suspects():
    prior = {"a": 0.5, "b": 0.5}
    splits = DiagnosticAction("swap_a", "d", clears={"a": 1.0})
    irrelevant = DiagnosticAction("swap_c", "d", clears={"c": 1.0})
    assert expected_gain(prior, splits) > 0.8
    assert expected_gain(prior, irrelevant) == pytest.approx(0.0, abs=1e-9)
    best, gains = choose(prior, [irrelevant, splits])
    assert best is splits and gains["swap_c"] < gains["swap_a"]
    assert choose(prior, [irrelevant])[0] is None  # nothing worth running


def test_an_action_does_not_apply_when_only_ignored_contracts_failed():
    action = DiagnosticAction("space", "d", clears={"h": 1.0}, ignores=("timing",))
    assert not action.applies_to({"timing"})
    assert action.applies_to({"timing", "operands"})


# -- the loop, end to end --------------------------------------------------------------


class SwapAdapter(FakeAdapter):
    """``lose_ack`` breaks the server, but the failing contract's source is the
    client, so localization blames the client. ``parameters.reference`` runs a
    component's reference implementation, where its faults have no effect."""

    def validate_parameters(self, parameters):
        if set(parameters) - {"reference"}:
            raise ConfigError("unknown parameters")

    def execute(self, ctx):
        if "server" in ctx.parameters.get("reference", []):
            ctx = type(ctx)(**{**ctx.__dict__, "faults": ()})
        return super().execute(ctx)

    def diagnostic_actions(self, parameters, base_dir):
        return [DiagnosticAction(f"reference_{c}", f"swap {c}", parameters={"reference": [c]}, clears={c: 1.0})
                for c in ("client", "server")]


def _failed_bundle(tmp_path, adapter=None):
    runner = ExperimentRunner(tmp_path / "runs", adapter_loader=lambda ref: adapter or SwapAdapter())
    run = runner.run(experiment(tmp_path, faults=["lose_ack"]))
    assert run.manifest["localization"]["top"][0]["subsystem"] == "client"  # the wrong suspect
    return run.bundle_dir, runner


def _runner_for(adapter):
    return lambda runs: ExperimentRunner(runs, adapter_loader=lambda ref: adapter)


def test_diagnosis_runs_the_separating_swap_and_names_the_server(tmp_path):
    bundle, _ = _failed_bundle(tmp_path)
    diag = diagnose(bundle, runner_for=_runner_for(SwapAdapter()))
    assert diag.status == "confident" and diag.top == "server"
    # Swapping in a reference client (the prime suspect) still fails, which
    # alone puts the server above the threshold: one experiment, not two.
    assert [(s.action, s.outcome) for s in diag.steps] == [("reference_client", "fail")]
    assert diag.posterior["server"] > 0.9 > diag.prior["server"]
    out = bundle.with_name(bundle.name + ".diagnosis")
    trail = json.loads((out / "diagnosis.json").read_text(encoding="utf-8"))
    assert trail["posterior"][0]["subsystem"] == "server" and trail["experiments_run"] == len(diag.steps)
    assert trail["bundle"]["run_id"] == bundle.name and len(trail["bundle"]["manifest_sha256"]) == 64
    assert "| `server` |" in (out / "report.md").read_text(encoding="utf-8")
    assert in_diagnosis(out / "runs") and not in_diagnosis(bundle)


def test_budget_and_run_all_modes(tmp_path):
    bundle, _ = _failed_bundle(tmp_path)
    assert diagnose(bundle, runner_for=_runner_for(SwapAdapter()), budget=0).status == "budget"
    everything = diagnose(bundle, runner_for=_runner_for(SwapAdapter()), run_all=True,
                          out_dir=tmp_path / "all")
    assert everything.status == "exhausted" and len(everything.steps) == 2 and everything.top == "server"
    with pytest.raises(ConfigError, match="budget"):
        diagnose(bundle, runner_for=_runner_for(SwapAdapter()), budget=-1)


def test_an_adapter_without_actions_leaves_the_localization_alone(tmp_path):
    bundle, _ = _failed_bundle(tmp_path, FakeAdapter())
    diag = diagnose(bundle, runner_for=_runner_for(FakeAdapter()))
    assert diag.status == "no_actions" and diag.steps == [] and diag.posterior == diag.prior


def test_follow_up_runs_are_not_scored_as_labeled_runs(tmp_path, capsys):
    bundle, _ = _failed_bundle(tmp_path)
    diagnose(bundle, runner_for=_runner_for(SwapAdapter()))
    assert [r.bundle for r in score_paths([tmp_path / "runs"])] == [bundle.resolve()]


def test_diagnose_command_prints_the_trail_and_gates_a_directory(tmp_path, capsys, monkeypatch):
    bundle, _ = _failed_bundle(tmp_path)
    import crosslayer.core.orchestrator.runner as runner_mod

    original = runner_mod.ExperimentRunner.__init__

    def init(self, output_root, **kwargs):
        kwargs.setdefault("adapter_loader", lambda ref: SwapAdapter())
        original(self, output_root, **kwargs)

    monkeypatch.setattr(runner_mod.ExperimentRunner, "__init__", init)
    assert main(["diagnose", str(bundle)]) == 0
    out = capsys.readouterr().out
    assert "status    confident" in out and "-> server" in out
    assert main(["diagnose", str(tmp_path / "runs"), "--min-top1", "1.0"]) == 0
    assert "top-1     0/1 before -> 1/1 after" in capsys.readouterr().out
