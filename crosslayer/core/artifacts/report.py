"""Human-readable Markdown report for a run bundle."""

from __future__ import annotations

from collections import defaultdict
from typing import Any, Sequence

from crosslayer.core.contracts.model import ContractResult, FailurePolicy, FindingKind
from crosslayer.core.units import NS_PER_S
from crosslayer.core.verdict import Verdict

_MAX_FINDINGS_SHOWN = 10


def format_timestamp(ns: int | None) -> str:
    if ns is None:
        return "n/a"
    return f"{ns // NS_PER_S}.{ns % NS_PER_S:09d} s"


def _cell(text: str) -> str:
    return text.replace("|", "\\|").replace("\n", " ")


def coverage_bins(stats: Any) -> dict[str, dict[str, int]] | None:
    """``stats["coverage"]`` if it has the bin shape ``{group: {bin: hits}}``, else None.

    Adapters report coverage this way in their normalization stats, listing
    bins with zero hits too, so reports can show what was never exercised.
    """
    coverage = stats.get("coverage") if isinstance(stats, dict) else None
    if not isinstance(coverage, dict) or not coverage or not all(
        isinstance(bins, dict) and all(isinstance(n, int) and not isinstance(n, bool) for n in bins.values())
        for bins in coverage.values()
    ):
        return None
    return {str(g): {str(b): int(n) for b, n in bins.items()} for g, bins in coverage.items()}


def _localization_section(loc: dict[str, Any]) -> list[str]:
    out = ["## Fault localization\n",
           f"Where the failure most likely started: a ranking, not a proof. Status **{loc['status']}**"
           + (f": {loc['reason']}" if loc.get("reason") else "") + f". Method `{loc['method']}`.\n",
           "| Rank | Subsystem | Interface | Probability | Evidence (failing) | Counter-evidence (passing) |",
           "|---|---|---|---|---|---|"]
    for rank, h in enumerate(loc["hypotheses"][:5], start=1):
        evidence = ", ".join(f"`{e['contract']}` ({e['role']})" for e in h["evidence"][:3])
        if len(h["evidence"]) > 3:
            evidence += f", +{len(h['evidence']) - 3} more"
        counter = f"{len(h['counter_evidence'])} contracts" if h["counter_evidence"] else "-"
        out.append(f"| {rank} | `{h['subsystem']}` | {h['interface'] or '-'} | {h['probability']:.2f} | "
                   f"{_cell(evidence)} | {counter} |")
    if loc.get("unresolved_contracts"):
        out.append(f"\nNot weighed (inconclusive): {', '.join(f'`{c}`' for c in loc['unresolved_contracts'])}.")
    out.append("")
    return out


def render_markdown(
    manifest: dict[str, Any],
    results: Sequence[ContractResult],
    timings: dict[str, float] | None = None,
    coverage: dict[str, dict[str, int]] | None = None,
    localization: dict[str, Any] | None = None,
) -> str:
    exp = manifest["experiment"]
    adapter = manifest["adapter"]
    execution = manifest.get("execution") or {}
    out: list[str] = []

    out.append(f"# CrossLayer run `{manifest['run_id']}`\n")
    out.append(f"**Verdict: {manifest['verdict'].upper()}**\n")
    out.append(f"- Experiment: `{exp['name']}` (config `{manifest['config_hash'][:12]}`, seed {exp['seed']})")
    out.append(f"- Adapter: `{adapter['name']}` {adapter['adapter_version']} -> system `{adapter['system']}` {adapter.get('system_version') or ''}")
    out.append(f"- Mode: {manifest['mode']}")
    if execution:
        out.append(
            f"- Execution: {execution.get('status')} (exit code {execution.get('exit_code')}) "
            f"in {execution.get('duration_s', 0):.3f} s"
        )
    events = manifest.get("events") or {}
    out.append(
        f"- Events: {events.get('count', 0)} normalized, "
        f"{events.get('parse_diagnostics', 0)} raw lines not normalized"
    )
    faults = manifest.get("faults") or []
    if faults:
        labels = ", ".join(f"`{f['name']}` (target: {f.get('target_subsystem')})" for f in faults)
        out.append(f"- Injected faults: {labels}")
    else:
        out.append("- Injected faults: none")
    for err in manifest.get("errors") or []:
        out.append(f"- **Error during {err['stage']}:** {err['message']}")
    out.append("")

    out.append("## Contracts\n")
    out.append("| Contract | Boundary | Owner | Policy | Verdict | Summary |")
    out.append("|---|---|---|---|---|---|")
    for r in results:
        c = r.contract
        out.append(
            f"| `{c.id}` | {c.source} -> {c.destination} | {c.owner} | {c.on_fail} | "
            f"**{r.verdict.upper()}** | {_cell(r.summary)} |"
        )
    out.append("")

    by_owner: dict[str, list[ContractResult]] = defaultdict(list)
    for r in results:
        if r.verdict in (Verdict.FAIL, Verdict.ERROR, Verdict.INCONCLUSIVE):
            by_owner[r.contract.owner].append(r)
    if by_owner:
        out.append("## Needs attention, by owner\n")
        for owner in sorted(by_owner):
            items = ", ".join(f"`{r.contract.id}` ({r.verdict})" for r in by_owner[owner])
            out.append(f"- **{owner}**: {items}")
        out.append("")

    failing = sorted(
        (r for r in results if r.verdict is Verdict.FAIL),
        key=lambda r: (r.first_violation_ns is None, r.first_violation_ns or 0),
    )
    if failing:
        out.append("## Violations (earliest first)\n")
        out.append(
            "Ordering is by first observed violation time. An earlier violation is a lead, not proof of root cause.\n"
        )
        for r in failing:
            c = r.contract
            policy = " (warning only)" if c.on_fail is FailurePolicy.WARN else ""
            out.append(f"### `{c.id}`{policy}\n")
            out.append(f"{c.title}. Severity {c.severity}, owner {c.owner}, boundary {c.source} -> {c.destination}.")
            out.append(f"First violation at {format_timestamp(r.first_violation_ns)}.\n")
            shown = [f for f in r.findings if f.kind is FindingKind.VIOLATION][:_MAX_FINDINGS_SHOWN]
            for finding in shown:
                out.append(f"- {finding.message}")
                for key in ("observed", "prev", "curr"):
                    values = finding.details.get(key)
                    if isinstance(values, dict) and values:
                        rendered = ", ".join(f"{k}={v}" for k, v in values.items())
                        out.append(f"  - {key}: {rendered}")
                for ref in finding.evidence:
                    out.append(
                        f"  - `{ref.event_id}` {ref.event_type} @ {format_timestamp(ref.timestamp_ns)} "
                        f"-> `{ref.raw_artifact_ref}`"
                    )
            total = r.stats.get("violations", len(shown))
            if isinstance(total, int) and total > len(shown):
                out.append(f"- ... {total - len(shown)} more violations in contracts.json")
            out.append("")

    if localization and localization.get("hypotheses"):
        out.extend(_localization_section(localization))

    inconclusive = [r for r in results if r.verdict is Verdict.INCONCLUSIVE]
    if inconclusive:
        out.append("## Inconclusive\n")
        for r in inconclusive:
            out.append(f"- `{r.contract.id}`: {r.summary}")
        out.append("")

    if coverage:
        out.append("## Coverage\n")
        out.append("| Group | Bins hit | Never hit |")
        out.append("|---|---|---|")
        for group, bins in coverage.items():
            never = [b for b, n in bins.items() if n == 0]
            out.append(f"| {group} | {len(bins) - len(never)}/{len(bins)} | {_cell(', '.join(never)) or '-'} |")
        out.append("")

    if timings:
        out.append("## Timings\n")
        out.append("| Stage | Seconds |")
        out.append("|---|---|")
        for stage, seconds in timings.items():
            out.append(f"| {stage} | {seconds:.3f} |")
        out.append("")

    out.append("## Reproduce\n")
    out.append("```sh\ncrosslayer replay .   # any OS; or: sh replay.sh\n```\n")
    out.append("External dependencies are listed at the top of `replay.sh` and in `versions.json`.\n")
    return "\n".join(out)
