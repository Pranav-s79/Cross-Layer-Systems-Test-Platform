"""Guided follow-up experiments: run the variant that best separates the suspects (roadmap Phase 8).

A failing run's localization is a probability over subsystems. For one
configuration, an adapter may declare diagnostic actions
(``SystemAdapter.diagnostic_actions``): variants of the experiment, each
with an outcome model that says what each hypothesis predicts.

* ``clears``: the variant removes what a fault in some subsystems needs to
  show (that subsystem swapped for a reference implementation, or the
  condition that exercises it taken away). The outcome is whether the
  failure reproduces; ``clears[s]`` is P(no failure | fault in s). A key
  ``subsystem/interface`` applies when the localization blamed that
  interface of the subsystem, and wins over a plain ``subsystem`` key: the
  same subsystem can fail in ways a variant does and does not remove.
  ``ignores`` names contracts the variant changes by construction (a timing
  budget when the instruction mix changes); they do not count, and an action
  does not apply when every original failure is one of them.
* ``observes``: the variant sees more (a trace with operand values). The
  outcome is the variant's own top-ranked subsystem, which names the faulty
  one with probability ``observes`` and any other suspect otherwise.

The planner keeps a posterior over the suspects (the prior is the
localization), picks the unused action with the highest expected
information gain, runs it, updates by Bayes' rule, and stops when one
suspect reaches ``threshold``, the budget is spent, or no remaining action
could change the answer. Likelihoods are clipped to ``[NOISE, 1 - NOISE]``:
one surprising outcome lowers a suspect but never rules it out.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from crosslayer.core.artifacts.bundle import MANIFEST, load_manifest
from crosslayer.core.errors import ConfigError
from crosslayer.core.verdict import Verdict

METHOD = "expected-information-gain-v1"
NOISE = 0.02
MIN_GAIN = 0.01
"""Bits: below this, running the action would not change the answer."""
DEFAULT_THRESHOLD = 0.9
DIAGNOSIS_SUFFIX = ".diagnosis"
"""A diagnosis lives next to its bundle (``<bundle>.diagnosis/``); bundles stay
immutable, and scoring skips the variant runs inside."""

_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")


@dataclass(frozen=True)
class DiagnosticAction:
    """One experiment variant and what each suspect predicts it will show."""

    name: str
    description: str
    parameters: Mapping[str, Any] = field(default_factory=dict)
    """Overrides merged into the experiment's parameters."""
    clears: Mapping[str, float] = field(default_factory=dict)
    """Subsystem -> probability that the variant passes if the fault is there."""
    observes: float | None = None
    """For an observing action: the probability that its top suspect is right."""
    ignores: tuple[str, ...] = ()
    """Contracts whose results this variant changes by construction."""

    def __post_init__(self) -> None:
        if not _NAME_RE.match(self.name):
            raise ValueError(f"action name {self.name!r} must be a lowercase identifier")
        if bool(self.clears) == (self.observes is not None):
            raise ValueError(f"action {self.name!r} needs exactly one outcome model: clears or observes")
        if any(not 0.0 <= p <= 1.0 for p in self.clears.values()):
            raise ValueError(f"action {self.name!r}: clears probabilities must be in [0, 1]")
        if self.observes is not None and not 0.0 < self.observes <= 1.0:
            raise ValueError(f"action {self.name!r}: observes must be in (0, 1]")

    @property
    def model(self) -> str:
        return "clears" if self.clears else "observes"

    def clear_probability(self, suspect: str, interface: str | None = None) -> float:
        if interface is not None and f"{suspect}/{interface}" in self.clears:
            return self.clears[f"{suspect}/{interface}"]
        return self.clears.get(suspect, 0.0)

    def applies_to(self, failing: set[str]) -> bool:
        """False when every failing contract is one this variant changes by construction."""
        return bool(failing - set(self.ignores))

    def likelihoods(self, suspects: Sequence[str],
                    interfaces: Mapping[str, str | None] | None = None) -> dict[str, dict[str, float]]:
        """Outcome -> suspect -> P(outcome | fault in suspect), clipped and normalized per suspect.
        ``interfaces`` gives the interface the localization blamed for each suspect."""
        if self.clears:
            passes = {s: _clip(self.clear_probability(s, (interfaces or {}).get(s))) for s in suspects}
            return {"pass": passes, "fail": {s: 1.0 - p for s, p in passes.items()}}
        assert self.observes is not None
        if len(suspects) < 2:
            return {s: {s: 1.0} for s in suspects}
        right, wrong = _clip(self.observes), _clip((1.0 - self.observes) / (len(suspects) - 1))
        raw = {o: {s: right if o == s else wrong for s in suspects} for o in suspects}
        totals = {s: sum(raw[o][s] for o in suspects) for s in suspects}
        return {o: {s: raw[o][s] / totals[s] for s in suspects} for o in suspects}

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"name": self.name, "description": self.description,
                               "parameters": dict(self.parameters), "model": self.model}
        if self.clears:
            out["clears"] = dict(self.clears)
        else:
            out["observes"] = self.observes
        if self.ignores:
            out["ignores"] = list(self.ignores)
        return out


def _clip(p: float) -> float:
    return min(max(p, NOISE), 1.0 - NOISE)


def entropy(dist: Mapping[str, float]) -> float:
    return -sum(p * math.log2(p) for p in dist.values() if p > 0)


def update(prior: Mapping[str, float], likelihood: Mapping[str, float]) -> dict[str, float]:
    joint = {s: prior[s] * likelihood.get(s, 1.0) for s in prior}
    total = sum(joint.values()) or 1.0
    return {s: p / total for s, p in joint.items()}


def expected_gain(prior: Mapping[str, float], action: DiagnosticAction,
                  interfaces: Mapping[str, str | None] | None = None) -> float:
    """Expected reduction in entropy (bits) from running ``action``."""
    tables = action.likelihoods(list(prior), interfaces)
    after = 0.0
    for table in tables.values():
        p_outcome = sum(prior[s] * table[s] for s in prior)
        if p_outcome > 0:
            after += p_outcome * entropy(update(prior, table))
    return max(entropy(prior) - after, 0.0)


def choose(prior: Mapping[str, float], actions: Sequence[DiagnosticAction],
           interfaces: Mapping[str, str | None] | None = None) -> tuple[DiagnosticAction | None, dict[str, float]]:
    """The action with the highest expected gain (ties: declaration order), and every action's gain."""
    gains = {a.name: round(expected_gain(prior, a, interfaces), 6) for a in actions}
    best = max(actions, key=lambda a: gains[a.name], default=None)
    if best is None or gains[best.name] < MIN_GAIN:
        return None, gains
    return best, gains


def _ranked(dist: Mapping[str, float]) -> list[dict[str, Any]]:
    return [{"subsystem": s, "probability": round(p, 4)}
            for s, p in sorted(dist.items(), key=lambda kv: (-kv[1], kv[0]))]


@dataclass
class Step:
    action: str
    expected_gain_bits: float
    gains: dict[str, float]
    run_id: str
    bundle: str
    verdict: str
    outcome: str | None
    """``pass``/``fail`` or the variant's top suspect; None if the run says nothing (an error)."""
    posterior: dict[str, float]

    def to_dict(self) -> dict[str, Any]:
        return {"action": self.action, "expected_gain_bits": self.expected_gain_bits, "gains": self.gains,
                "run_id": self.run_id, "bundle": self.bundle, "verdict": self.verdict, "outcome": self.outcome,
                "posterior": _ranked(self.posterior)}


@dataclass
class Diagnosis:
    experiment: str
    bundle: Path
    status: str
    """``confident``, ``budget``, ``no_informative_action``, ``no_actions``, or ``no_failures``."""
    prior: dict[str, float]
    posterior: dict[str, float]
    interfaces: dict[str, str | None]
    """The interface the localization blamed for each suspect."""
    actions: list[DiagnosticAction]
    steps: list[Step]
    threshold: float
    budget: int | None
    mode: str
    """``planned`` (by expected gain) or ``all`` (every action, in order)."""
    reason: str = ""

    @property
    def top(self) -> str | None:
        return _ranked(self.posterior)[0]["subsystem"] if self.posterior else None

    def to_dict(self) -> dict[str, Any]:
        manifest = self.bundle / MANIFEST
        return {
            "method": METHOD, "mode": self.mode, "experiment": self.experiment,
            "bundle": {"path": str(self.bundle), "run_id": self.bundle.name,
                       "manifest_sha256": hashlib.sha256(manifest.read_bytes()).hexdigest()},
            "threshold": self.threshold, "budget": self.budget, "status": self.status, "reason": self.reason,
            "experiments_run": len(self.steps), "prior": _ranked(self.prior), "posterior": _ranked(self.posterior),
            "actions": [a.to_dict() for a in self.actions], "steps": [s.to_dict() for s in self.steps],
        }


def diagnosis_dir(bundle: Path) -> Path:
    return bundle.with_name(bundle.name + DIAGNOSIS_SUFFIX)


def in_diagnosis(path: Path) -> bool:
    """True for anything inside a ``<bundle>.diagnosis/`` directory (variant runs)."""
    return any(part.endswith(DIAGNOSIS_SUFFIX) for part in path.resolve().parts)


def diagnose(
    bundle: Path,
    *,
    runner_for: Callable[[Path], Any],
    budget: int | None = None,
    threshold: float = DEFAULT_THRESHOLD,
    run_all: bool = False,
    out_dir: Path | None = None,
) -> Diagnosis:
    """Diagnose one failing bundle by running follow-up variants of its experiment.

    ``runner_for(runs_dir)`` returns an ``ExperimentRunner`` writing there.
    Writes ``diagnosis.json`` and ``report.md`` to ``out_dir`` (default
    ``<bundle>.diagnosis``), with the variant runs under ``runs/``.
    """
    from crosslayer.core.orchestrator.experiment import load_experiment

    manifest = load_manifest(bundle)
    config = bundle / "configuration.yaml"
    loc_path = bundle / "localization.json"
    if not config.is_file() or not loc_path.is_file():
        raise ConfigError("bundle has no configuration.yaml or localization.json (made before Phase 7?)",
                          location=str(bundle))
    if budget is not None and budget < 0:
        raise ConfigError("budget must be zero or more")
    experiment = load_experiment(config)
    loc = json.loads(loc_path.read_text(encoding="utf-8"))
    hypotheses = [h for h in loc.get("hypotheses", []) if h["probability"] > 0]
    prior = {h["subsystem"]: float(h["probability"]) for h in hypotheses}
    interfaces = {h["subsystem"]: h.get("interface") for h in hypotheses}
    failing = _failing(bundle)
    out = out_dir if out_dir is not None else diagnosis_dir(bundle)
    runner = runner_for(out / "runs")
    adapter = runner.plan(experiment).adapter
    declared = [] if experiment.inputs else list(adapter.diagnostic_actions(experiment.parameters,
                                                                            experiment.base_dir))
    actions = [a for a in declared if a.applies_to(failing)]
    mode = "all" if run_all else "planned"
    diag = Diagnosis(manifest["experiment"]["name"], bundle, "", dict(prior), dict(prior), interfaces, actions,
                     [], threshold, budget, mode)

    if not prior:
        diag.status, diag.reason = "no_failures", "the run has no failing contracts to explain"
    elif not actions:
        diag.status = "no_actions"
        diag.reason = ("an analyzed run cannot be re-run" if experiment.inputs
                       else "no diagnostic action applies to this configuration and failure")
    else:
        _loop(diag, experiment, runner, actions)
    _write(diag, out)
    return diag


def _loop(diag: Diagnosis, experiment: Any, runner: Any, actions: list[DiagnosticAction]) -> None:
    remaining = list(actions)
    while True:
        if max(diag.posterior.values()) >= diag.threshold and diag.mode == "planned":
            diag.status = "confident"
            diag.reason = f"`{diag.top}` reached {diag.posterior[diag.top or '']:.2f}"
            return
        if diag.budget is not None and len(diag.steps) >= diag.budget:
            diag.status, diag.reason = "budget", f"ran {len(diag.steps)} of a budget of {diag.budget}"
            return
        gains: dict[str, float] = {}
        action: DiagnosticAction | None
        if diag.mode == "all":
            action = remaining[0] if remaining else None
        else:
            action, gains = choose(diag.posterior, remaining, diag.interfaces)
        if action is None:
            if diag.mode == "all" or not remaining:
                diag.status, diag.reason = "exhausted", "every diagnostic action has run"
            else:
                diag.status = "no_informative_action"
                diag.reason = f"no remaining action is expected to gain {MIN_GAIN} bits"
            return
        remaining.remove(action)
        gain = gains[action.name] if gains else round(expected_gain(diag.posterior, action, diag.interfaces), 6)
        variant = replace(experiment, name=f"{experiment.name}+{action.name}",
                          parameters={**experiment.parameters, **action.parameters})
        result = runner.run(variant)
        outcome = _outcome(action, result, list(diag.posterior))
        if outcome is not None:
            table = action.likelihoods(list(diag.posterior), diag.interfaces)[outcome]
            diag.posterior = update(diag.posterior, table)
        diag.steps.append(Step(action.name, gain, gains, result.run_id, str(result.bundle_dir),
                               result.verdict.value, outcome, dict(diag.posterior)))


def _failing(bundle: Path) -> set[str]:
    doc = json.loads((bundle / "contracts.json").read_text(encoding="utf-8")) if (bundle / "contracts.json").is_file() else {}
    return {c["result"]["contract_id"] for c in doc.get("contracts", []) if c["result"].get("verdict") == Verdict.FAIL.value}


def _outcome(action: DiagnosticAction, result: Any, suspects: list[str]) -> str | None:
    """A clearing action asks whether the failure reproduces: ``fail`` if a
    contract it does not ignore fails, ``pass`` if none does (an INCONCLUSIVE
    contract had nothing to check, which is how a cleared symptom looks),
    nothing from an ERROR. An observing action's outcome is the variant's top
    suspect."""
    if action.clears:
        if result.verdict is Verdict.ERROR:
            return None
        failed = {r.contract.id for r in result.results if r.verdict is Verdict.FAIL}
        return "fail" if failed - set(action.ignores) else "pass"
    top = (result.manifest.get("localization") or {}).get("top") or []
    return top[0]["subsystem"] if top and top[0]["subsystem"] in suspects else None


def _write(diag: Diagnosis, out: Path) -> None:
    out.mkdir(parents=True, exist_ok=True)
    (out / "diagnosis.json").write_text(json.dumps(diag.to_dict(), indent=2) + "\n", encoding="utf-8")
    (out / "report.md").write_text(render(diag), encoding="utf-8")


def render(diag: Diagnosis) -> str:
    """The diagnosis trail as Markdown."""
    lines = [f"# Diagnosis of `{diag.experiment}`\n",
             f"Status **{diag.status}**" + (f": {diag.reason}" if diag.reason else "") + f". "
             f"{len(diag.steps)} follow-up experiment(s), method `{METHOD}` ({diag.mode}).\n",
             "| Suspect | Before | After |", "|---|---|---|"]
    for row in _ranked(diag.posterior)[:5]:
        lines.append(f"| `{row['subsystem']}` | {diag.prior.get(row['subsystem'], 0):.2f} | {row['probability']:.2f} |")
    if diag.steps:
        lines += ["", "| # | Action | Expected gain (bits) | Verdict | Outcome | Top suspect after |",
                  "|---|---|---|---|---|---|"]
        for i, s in enumerate(diag.steps, start=1):
            top = _ranked(s.posterior)[0]
            lines.append(f"| {i} | `{s.action}` | {s.expected_gain_bits:.3f} | {s.verdict.upper()} | "
                         f"{s.outcome or '-'} | `{top['subsystem']}` {top['probability']:.2f} |")
    if diag.actions:
        lines += ["", "Available actions:", ""]
        lines += [f"- `{a.name}` ({a.model}): {a.description}" for a in diag.actions]
    return "\n".join(lines) + "\n"
