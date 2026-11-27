"""Flaky contract detection over indexed runs.

Runs of one experiment, seed, and config hash are repeats of the same
configuration, so a contract whose verdict differs between them is not
deterministic: it is flaky. Series are keyed by experiment and seed as well,
so a seed sweep is compared per seed and different seeds never count as
disagreeing repeats. The run verdict is checked the same way (subject ``run``).
Only index data is read; fault labels are never consulted.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from typing import Any, Sequence

from crosslayer.core.errors import ConfigError

RUN_SUBJECT = "run"


@dataclass
class FlakyFinding:
    experiment: str
    seed: int | None
    config_hash: str
    subject: str             # a contract id, or "run" for the run verdict
    run_count: int           # repeats in which the subject was recorded
    counts: dict[str, int]   # verdict -> number of runs, most frequent first
    flips: int               # changes of verdict between consecutive runs
    latest_run: str
    latest_verdict: str
    first_flip_run: str      # the first run whose verdict differs from the one before it
    first_flip_at: str | None
    runs: list[dict[str, Any]]  # oldest first: run_id, started_at, verdict

    def to_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)


def _scan(experiment: str, seed: int | None, config_hash: str, subject: str,
          samples: list[dict[str, Any]], min_runs: int) -> FlakyFinding | None:
    """``samples`` are oldest first, each with run_id, started_at, verdict."""
    if len(samples) < min_runs or len({s["verdict"] for s in samples}) < 2:
        return None
    first_flip = next(b for a, b in zip(samples, samples[1:]) if a["verdict"] != b["verdict"])
    flips = sum(a["verdict"] != b["verdict"] for a, b in zip(samples, samples[1:]))
    counts = Counter(s["verdict"] for s in samples)
    return FlakyFinding(
        experiment, seed, config_hash, subject, len(samples),
        dict(sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))), flips,
        samples[-1]["run_id"], samples[-1]["verdict"], first_flip["run_id"], first_flip["started_at"], samples)


def find_flaky(runs: Sequence[dict[str, Any]], *, min_runs: int = 2) -> list[FlakyFinding]:
    """Contracts (and run verdicts) that disagree between runs of the same configuration.

    ``runs`` are dicts as ``RunIndex.runs()`` returns them, in any order.
    Findings are sorted by experiment, seed, config hash, then subject.
    """
    if min_runs < 2:
        raise ConfigError(f"minimum runs must be at least 2, got {min_runs}")
    groups: dict[tuple[str, int | None, str], list[dict[str, Any]]] = {}
    for run in runs:
        groups.setdefault((run["experiment"], run["seed"], run["config_hash"]), []).append(run)
    findings: list[FlakyFinding] = []
    for (experiment, seed, config), group in groups.items():
        series = sorted(group, key=lambda r: (r["started_at"] or "", r["run_id"]))
        samples: dict[str, list[dict[str, Any]]] = {
            RUN_SUBJECT: [{"run_id": r["run_id"], "started_at": r["started_at"], "verdict": r["verdict"]} for r in series]}
        for run in series:
            for result in run["contracts"]:
                samples.setdefault(result["contract_id"], []).append(
                    {"run_id": run["run_id"], "started_at": run["started_at"], "verdict": result["verdict"]})
        for subject, subject_samples in samples.items():
            found = _scan(experiment, seed, config, subject, subject_samples, min_runs)
            if found:
                findings.append(found)
    findings.sort(key=lambda f: (f.experiment, f.seed is None, f.seed or 0, f.config_hash, f.subject != RUN_SUBJECT, f.subject))
    return findings


def render(findings: Sequence[FlakyFinding], *, series: int) -> str:
    lines: list[str] = []
    for f in findings:
        seed = f" seed {f.seed}" if f.seed is not None else ""
        counts = ", ".join(f"{n} {verdict}" for verdict, n in f.counts.items())
        what = "run verdict" if f.subject == RUN_SUBJECT else f.subject
        lines.append(f"FLAKY {f.experiment}{seed} config {f.config_hash[:12]}: {what}: {counts} over {f.run_count} runs, "
                     f"{f.flips} flip(s), latest {f.latest_verdict} ({f.latest_run}); "
                     f"first differed {f.first_flip_at or '-'} in {f.first_flip_run}")
    lines.append(f"{series} repeated configuration(s) checked, {len(findings)} flaky")
    return "\n".join(lines)


def repeated_series(runs: Sequence[dict[str, Any]], *, min_runs: int = 2) -> int:
    """How many (experiment, seed, config hash) groups have at least ``min_runs`` runs."""
    counts = Counter((r["experiment"], r["seed"], r["config_hash"]) for r in runs)
    return sum(n >= min_runs for n in counts.values())
