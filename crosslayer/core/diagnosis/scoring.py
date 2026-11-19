"""Score localization against ground truth: top-1 and top-3 accuracy.

Truth comes from a bundle's manifest: ``ground_truth`` if the experiment
declares one, else the first injected fault's ``target_subsystem`` and
``target_interface``. Bundles with neither are not scored. The localization
is read from the bundle's ``localization.json``, exactly as the run wrote it.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from crosslayer.core.artifacts.bundle import MANIFEST


@dataclass(frozen=True)
class ScoredRun:
    experiment: str
    bundle: Path
    subsystem: str
    interface: str | None
    rank: int | None
    """1-based rank of the true subsystem among the hypotheses; None if absent."""
    interface_hit: bool | None
    """Top-1 hypothesis has the true subsystem and interface; None if the truth has no interface."""
    status: str
    top: list[dict[str, Any]]

    def to_dict(self) -> dict[str, Any]:
        return {"experiment": self.experiment, "bundle": str(self.bundle), "truth_subsystem": self.subsystem,
                "truth_interface": self.interface, "rank": self.rank, "interface_hit": self.interface_hit,
                "status": self.status, "top": self.top}


def _truth(manifest: dict[str, Any]) -> tuple[str, str | None] | None:
    truth = manifest.get("ground_truth")
    if not truth:
        labeled = [f for f in manifest.get("faults") or [] if f.get("target_subsystem")]
        truth = labeled[0] if labeled else None
    return (truth["target_subsystem"], truth.get("target_interface")) if truth else None


def score_bundle(bundle: Path) -> ScoredRun | None:
    manifest = json.loads((bundle / MANIFEST).read_text(encoding="utf-8"))
    truth = _truth(manifest)
    loc_path = bundle / "localization.json"
    if truth is None or not loc_path.is_file():
        return None
    loc = json.loads(loc_path.read_text(encoding="utf-8"))
    hyps = loc.get("hypotheses", [])
    subsystems = [h["subsystem"] for h in hyps]
    subsystem, interface = truth
    rank = subsystems.index(subsystem) + 1 if subsystem in subsystems else None
    interface_hit = None if interface is None else bool(
        hyps and hyps[0]["subsystem"] == subsystem and hyps[0].get("interface") == interface)
    top = [{k: h.get(k) for k in ("subsystem", "interface", "probability")} for h in hyps[:3]]
    return ScoredRun(manifest["experiment"]["name"], bundle, subsystem, interface, rank, interface_hit,
                     loc.get("status", "?"), top)


def score_paths(paths: Iterable[Path]) -> list[ScoredRun]:
    """Score every labeled bundle under the given bundles or directories (searched recursively)."""
    seen: set[Path] = set()
    runs: list[ScoredRun] = []
    for path in paths:
        manifests = [path / MANIFEST] if (path / MANIFEST).is_file() else sorted(path.rglob(MANIFEST))
        for manifest in manifests:
            bundle = manifest.parent.resolve()
            if bundle in seen:
                continue
            seen.add(bundle)
            scored = score_bundle(bundle)
            if scored is not None:
                runs.append(scored)
    return runs


def summarize(runs: list[ScoredRun]) -> dict[str, Any]:
    n = len(runs)
    with_interface = [r for r in runs if r.interface_hit is not None]
    confident = [r for r in runs if r.status == "confident"]

    def rate(hits: int, total: int) -> float | None:
        return round(hits / total, 4) if total else None

    top1 = sum(r.rank == 1 for r in runs)
    top3 = sum(r.rank is not None and r.rank <= 3 for r in runs)
    iface = sum(bool(r.interface_hit) for r in with_interface)
    confident_right = sum(r.rank == 1 for r in confident)
    return {
        "scored": n,
        "top1": {"hits": top1, "rate": rate(top1, n)},
        "top3": {"hits": top3, "rate": rate(top3, n)},
        "interface_top1": {"hits": iface, "of": len(with_interface), "rate": rate(iface, len(with_interface))},
        "confident": {"calls": len(confident), "correct": confident_right, "rate": rate(confident_right, len(confident))},
    }
