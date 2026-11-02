"""Compare two run bundles: regression detection and repeatability checks."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from crosslayer.core.artifacts.bundle import load_json, load_manifest
from crosslayer.core.telemetry.store import canonical_json

REGRESSION_CHANGES = frozenset({"regression", "lost_coverage"})


def classify(baseline: str | None, candidate: str | None) -> str:
    if baseline is None:
        return "added"
    if candidate is None:
        return "removed"
    if baseline == candidate:
        return "unchanged"
    if baseline == "pass" and candidate in ("fail", "error"):
        return "regression"
    if baseline == "pass":
        return "lost_coverage"  # was verified, now inconclusive or skipped
    if candidate == "pass" and baseline in ("fail", "error"):
        return "fixed"
    if candidate == "pass":
        return "gained_coverage"  # was inconclusive or skipped, now verified
    return "changed"


@dataclass
class ContractDelta:
    contract_id: str
    baseline: str | None
    candidate: str | None
    change: str
    contract_changed: bool = False
    """The contract's definition differs between the two runs, so a verdict
    change may come from editing the contract rather than from the system."""
    baseline_version: int | None = None
    candidate_version: int | None = None


@dataclass
class Comparison:
    baseline_run: str
    candidate_run: str
    baseline_verdict: str
    candidate_verdict: str
    same_config: bool
    same_seed: bool
    fingerprint_match: bool
    deltas: list[ContractDelta] = field(default_factory=list)

    @property
    def regressions(self) -> list[ContractDelta]:
        return [d for d in self.deltas if d.change in REGRESSION_CHANGES]

    def to_dict(self) -> dict[str, Any]:
        return {
            "baseline_run": self.baseline_run,
            "candidate_run": self.candidate_run,
            "baseline_verdict": self.baseline_verdict,
            "candidate_verdict": self.candidate_verdict,
            "same_config": self.same_config,
            "same_seed": self.same_seed,
            "fingerprint_match": self.fingerprint_match,
            "regressions": len(self.regressions),
            "deltas": [d.__dict__ for d in self.deltas],
        }


def _contracts(bundle_dir: Path) -> dict[str, dict[str, Any]]:
    doc = load_json(bundle_dir, "contracts.json")
    return {
        item["result"]["contract_id"]: {
            "verdict": item["result"]["verdict"],
            "version": item["result"].get("version"),
            "definition": canonical_json(item.get("definition")),
        }
        for item in doc["contracts"]
    }


def compare_bundles(baseline_dir: Path, candidate_dir: Path) -> Comparison:
    base = load_manifest(baseline_dir)
    cand = load_manifest(candidate_dir)
    before = _contracts(baseline_dir)
    after = _contracts(candidate_dir)
    deltas = []
    for cid in sorted(before.keys() | after.keys()):
        b, a = before.get(cid), after.get(cid)
        bv, av = (b or {}).get("verdict"), (a or {}).get("verdict")
        deltas.append(ContractDelta(
            cid, bv, av, classify(bv, av),
            contract_changed=bool(b and a and b["definition"] != a["definition"]),
            baseline_version=(b or {}).get("version"),
            candidate_version=(a or {}).get("version"),
        ))
    return Comparison(
        baseline_run=base["run_id"],
        candidate_run=cand["run_id"],
        baseline_verdict=base["verdict"],
        candidate_verdict=cand["verdict"],
        same_config=base["config_hash"] == cand["config_hash"],
        same_seed=base["experiment"]["seed"] == cand["experiment"]["seed"],
        fingerprint_match=base["events"]["fingerprint"] == cand["events"]["fingerprint"],
        deltas=deltas,
    )


def render_comparison(comparison: Comparison) -> str:
    c = comparison
    lines = [
        f"baseline  {c.baseline_run}  verdict={c.baseline_verdict}",
        f"candidate {c.candidate_run}  verdict={c.candidate_verdict}",
        f"same config: {c.same_config}   same seed: {c.same_seed}   identical event stream: {c.fingerprint_match}",
        "",
    ]
    width = max((len(d.contract_id) for d in c.deltas), default=8)
    for d in c.deltas:
        marker = "!!" if d.change in REGRESSION_CHANGES else "  "
        note = ""
        if d.contract_changed:
            note = f"  [contract changed: v{d.baseline_version} -> v{d.candidate_version}]"
        lines.append(
            f"{marker} {d.contract_id:<{width}}  {str(d.baseline):<12} -> {str(d.candidate):<12} {d.change}{note}"
        )
    lines.append("")
    lines.append(f"{len(c.regressions)} regression(s)")
    changed = [d.contract_id for d in c.deltas if d.contract_changed]
    if changed:
        lines.append(f"{len(changed)} contract(s) changed between the runs; their verdict changes may not be system changes")
    return "\n".join(lines)
