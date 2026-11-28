"""A self-contained HTML dashboard over indexed runs.

Reads only what :class:`RunIndex` returns (never bundles, never the clock), so
the same index always gives the same page. The page has no scripts and loads no
network resources; charts are inline SVG with a table beside each, so every
number is readable without rendering the chart.

Sections: suite health per day, failing contracts of each experiment's latest
run grouped by owner, regressions, flaky contracts, and localization accuracy
per day. Localization is scored from the indexed top hypotheses against the
ground truth (or, failing that, the first labeled fault), the same rule as
``crosslayer localize --score``; the index keeps three hypotheses, so top-3 is
the widest rank it can score.
"""

from __future__ import annotations

from collections import defaultdict
from html import escape
from typing import Any, Mapping, Sequence

from crosslayer.core.results.flaky import RUN_SUBJECT, FlakyFinding, find_flaky, repeated_series
from crosslayer.core.results.regressions import SeriesReport, analyze

VERDICTS = ("pass", "fail", "error", "inconclusive")
BAD = ("fail", "error")
NO_OWNER = "(no owner)"

_STYLE = """
:root{color-scheme:light dark;--bg:#fff;--fg:#14202e;--muted:#5b6b7c;--line:#d5dde6;--panel:#f5f8fb;
--pass:#1a8f4c;--fail:#c9304a;--error:#8a45c7;--inconclusive:#b27a00;--accent:#1f6fb2;
font-family:system-ui,sans-serif}
@media(prefers-color-scheme:dark){:root{--bg:#0b1522;--fg:#e6eef7;--muted:#9bacbf;--line:#243750;--panel:#101e30;
--pass:#4ade80;--fail:#fb7185;--error:#c084fc;--inconclusive:#fbbf24;--accent:#60a5fa}}
body{margin:0;background:var(--bg);color:var(--fg);line-height:1.45}
main{max-width:1040px;margin:auto;padding:24px 16px 48px}
h1{margin:0 0 4px}h2{margin:36px 0 8px;border-bottom:1px solid var(--line);padding-bottom:4px}
.muted{color:var(--muted)}code{font-family:ui-monospace,Consolas,monospace;overflow-wrap:anywhere}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px;margin:16px 0}
.card{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:10px 14px}
.card strong{display:block;font-size:1.5rem}
.wrap{overflow-x:auto}table{border-collapse:collapse;width:100%;font-size:.9rem}
th,td{text-align:left;padding:6px 10px;border-bottom:1px solid var(--line);vertical-align:top}
th{color:var(--muted);font-size:.75rem;text-transform:uppercase;letter-spacing:.06em}
.v-pass{color:var(--pass)}.v-fail{color:var(--fail)}.v-error{color:var(--error)}.v-inconclusive{color:var(--inconclusive)}
.chart{width:100%;height:auto;max-height:240px}.chart text{fill:var(--muted);font-size:10px}
.legend span{margin-right:14px}.legend i{display:inline-block;width:10px;height:10px;border-radius:2px;margin-right:4px}
"""


def _day(run: dict[str, Any]) -> str:
    return (run.get("started_at") or "unknown")[:10]


def _pct(hits: int, total: int) -> str:
    return f"{100 * hits / total:.0f}%" if total else "-"


def _verdict(v: str) -> str:
    return f'<span class="v-{escape(v)}">{escape(v)}</span>'


def _table(headers: Sequence[str], rows: Sequence[Sequence[str]], empty: str) -> str:
    if not rows:
        return f'<p class="muted">{escape(empty)}</p>'
    head = "".join(f"<th>{escape(h)}</th>" for h in headers)
    body = "".join("<tr>" + "".join(f"<td>{cell}</td>" for cell in row) + "</tr>" for row in rows)
    return f'<div class="wrap"><table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table></div>'


def _bars(days: Sequence[str], series: Mapping[str, Sequence[int]], label: str,
          names: Mapping[str, str] | None = None) -> str:
    """Stacked bars, one per day; each bar carries a <title> with its numbers."""
    names = names or {}
    totals = [sum(series[k][i] for k in series) for i in range(len(days))]
    peak = max(totals) or 1
    width, height, left, bottom = 640, 200, 30, 22
    slot = (width - left) / max(len(days), 1)
    bar = min(slot * 0.7, 40)
    parts = [f'<svg class="chart" viewBox="0 0 {width} {height}" role="img" aria-label="{escape(label)}">']
    parts.append(f'<text x="2" y="12">{peak}</text><text x="2" y="{height - bottom}">0</text>')
    for i, day in enumerate(days):
        x = left + i * slot + (slot - bar) / 2
        y = float(height - bottom)
        for kind, counts in series.items():
            h = (height - bottom - 16) * counts[i] / peak
            if h > 0:
                y -= h
                parts.append(f'<rect x="{x:.1f}" y="{y:.1f}" width="{bar:.1f}" height="{h:.1f}" fill="var(--{kind})"/>')
        detail = ", ".join(f"{counts[i]} {names.get(kind, kind)}" for kind, counts in series.items() if counts[i])
        parts.append(f'<rect x="{x:.1f}" y="16" width="{bar:.1f}" height="{height - bottom - 16}" fill="transparent">'
                     f"<title>{escape(day)}: {escape(detail)}</title></rect>")
        if len(days) <= 14 or i % max(len(days) // 7, 1) == 0:
            parts.append(f'<text x="{x:.1f}" y="{height - 6}">{escape(day[5:])}</text>')
    parts.append("</svg>")
    legend = "".join(f'<span><i style="background:var(--{k})"></i>{escape(names.get(k, k))}</span>' for k in series)
    return "".join(parts) + f'<p class="legend muted">{legend}</p>'


def health_by_day(runs: Sequence[dict[str, Any]]) -> tuple[list[str], dict[str, list[int]]]:
    days = sorted({_day(r) for r in runs})
    counts = {k: [0] * len(days) for k in VERDICTS}
    index = {d: i for i, d in enumerate(days)}
    other = [0] * len(days)
    for r in runs:
        if r["verdict"] in counts:
            counts[r["verdict"]][index[_day(r)]] += 1
        else:
            other[index[_day(r)]] += 1
    if any(other):  # a verdict this version does not know is shown, not dropped
        counts["inconclusive"] = [a + b for a, b in zip(counts["inconclusive"], other)]
    return days, counts


def latest_per_series(runs: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    latest: dict[tuple[str, int | None], dict[str, Any]] = {}
    for r in sorted(runs, key=lambda r: (r["started_at"] or "", r["run_id"])):
        latest[(r["experiment"], r["seed"])] = r
    return sorted(latest.values(), key=lambda r: (r["experiment"], r["seed"] is None, r["seed"] or 0))


def failing_by_owner(latest: Sequence[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    owners: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for run in latest:
        for c in run["contracts"]:
            if c["verdict"] in BAD:
                owners[c.get("owner") or NO_OWNER].append({**c, "experiment": run["experiment"], "seed": run["seed"],
                                                           "run_id": run["run_id"]})
    return dict(sorted(owners.items()))


def truth_of(run: dict[str, Any]) -> tuple[str, str | None] | None:
    """The injected location the run is scored against (ground truth, else the first labeled fault)."""
    truth = run.get("ground_truth")
    if not truth:
        labeled = [f for f in run.get("faults") or [] if f.get("target_subsystem")]
        truth = labeled[0] if labeled else None
    return (truth["target_subsystem"], truth.get("target_interface")) if truth else None


def localization_by_day(runs: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    """Per day: scored runs and how many had the true subsystem first, and within the top three."""
    days: dict[str, dict[str, Any]] = {}
    for run in runs:
        truth = truth_of(run)
        if truth is None or not run["hypotheses"]:
            continue
        ranked = [h["subsystem"] for h in sorted(run["hypotheses"], key=lambda h: h["rank"])]
        row = days.setdefault(_day(run), {"day": _day(run), "scored": 0, "top1": 0, "top3": 0})
        row["scored"] += 1
        row["top1"] += ranked[0] == truth[0]
        row["top3"] += truth[0] in ranked[:3]
    return [days[d] for d in sorted(days)]


def _health_section(runs: Sequence[dict[str, Any]]) -> str:
    days, counts = health_by_day(runs)
    rows = [[escape(d)] + [str(counts[k][i]) for k in VERDICTS] + [_pct(counts["pass"][i], sum(counts[k][i] for k in VERDICTS))]
            for i, d in enumerate(days)]
    return ("<h2>Suite health</h2><p class=\"muted\">Runs started per day (UTC) by verdict.</p>"
            + _bars(days, counts, "Runs per day by verdict")
            + _table(["Day", *VERDICTS, "Pass rate"], rows, "No runs."))


def _owners_section(latest: Sequence[dict[str, Any]]) -> str:
    owners = failing_by_owner(latest)
    rows = []
    for owner, items in owners.items():
        for c in items:
            seed = "" if c["seed"] is None else f" seed {c['seed']}"
            rows.append([escape(owner), f"<code>{escape(c['contract_id'])}</code>", _verdict(c["verdict"]),
                         escape(c["experiment"] + seed), escape(str(c.get("severity") or "-")),
                         escape(c.get("summary") or "")])
    return ("<h2>Failing contracts by owner</h2><p class=\"muted\">From the latest run of each experiment and seed.</p>"
            + _table(["Owner", "Contract", "Verdict", "Experiment", "Severity", "Summary"], rows,
                     "No failing contracts in the latest runs."))


def _regressions_section(reports: Sequence[SeriesReport]) -> str:
    rows = []
    for r in reports:
        for f in r.findings:
            if f.severity != "regression":
                continue
            seed = "" if r.seed is None else f" seed {r.seed}"
            rows.append([escape(r.experiment + seed), f"<code>{escape(f.subject)}</code>", escape(f.summary),
                         f"<code>{escape(f.first_seen_run)}</code>", escape(f.first_seen_at or "-"), str(f.runs_since)])
    return ("<h2>Regressions</h2><p class=\"muted\">The latest run got worse than the last different one; "
            "linked to the first run where it appeared.</p>"
            + _table(["Experiment", "Subject", "Change", "First seen in", "At", "Runs since"], rows, "No regressions."))


def _flaky_section(findings: Sequence[FlakyFinding], checked: int) -> str:
    rows = []
    for f in findings:
        seed = "" if f.seed is None else f" seed {f.seed}"
        counts = ", ".join(f"{n} {v}" for v, n in f.counts.items())
        rows.append([escape(f.experiment + seed), "run verdict" if f.subject == RUN_SUBJECT else f"<code>{escape(f.subject)}</code>",
                     escape(counts), str(f.flips), f"<code>{escape(f.first_flip_run)}</code>"])
    note = f"{checked} repeated configuration(s) checked, {len(findings)} flaky."
    return ("<h2>Flaky contracts</h2><p class=\"muted\">Verdicts that differ between runs with the same experiment, seed, "
            f"and config hash. {escape(note)}</p>"
            + _table(["Experiment", "Subject", "Verdicts", "Flips", "First differed in"], rows, "Nothing flaky."))


def _localization_section(runs: Sequence[dict[str, Any]]) -> str:
    rows_data = localization_by_day(runs)
    rows = [[escape(r["day"]), str(r["scored"]), f"{r['top1']} ({_pct(r['top1'], r['scored'])})",
             f"{r['top3']} ({_pct(r['top3'], r['scored'])})"] for r in rows_data]
    scored = sum(r["scored"] for r in rows_data)
    total = (f"Overall: {sum(r['top1'] for r in rows_data)}/{scored} top-1, {sum(r['top3'] for r in rows_data)}/{scored} top-3."
             if scored else "")
    chart = ""
    if rows_data:
        days = [r["day"] for r in rows_data]
        chart = _bars(days, {"pass": [r["top1"] for r in rows_data],
                             "inconclusive": [r["top3"] - r["top1"] for r in rows_data],
                             "fail": [r["scored"] - r["top3"] for r in rows_data]},
                      "Localization outcomes per day",
                      {"pass": "top-1", "inconclusive": "rank 2-3", "fail": "missed"})
    return ("<h2>Localization accuracy</h2><p class=\"muted\">Runs with a labeled fault or ground truth, scored on the "
            f"indexed top three hypotheses. {escape(total)}</p>" + chart
            + _table(["Day", "Scored", "Top-1", "Top-3"], rows, "No indexed run has both a labeled fault and a localization."))


def render_dashboard(runs: Sequence[dict[str, Any]], *, title: str = "CrossLayer dashboard") -> str:
    """The dashboard page for ``runs`` (dicts as ``RunIndex.runs()`` returns them, any order)."""
    latest = latest_per_series(runs)
    reports = analyze(runs)
    flaky = find_flaky(runs)
    failing_series = sum(r["verdict"] in BAD for r in latest)
    regressions = sum(f.severity == "regression" for r in reports for f in r.findings)
    through = max((r["started_at"] or "" for r in runs), default="") or "-"
    cards = [("Runs indexed", len(runs)), ("Experiment series", len(latest)),
             ("Series failing now", failing_series), ("Regressions", regressions), ("Flaky", len(flaky))]
    card_html = "".join(f'<div class="card">{escape(k)}<strong>{v}</strong></div>' for k, v in cards)
    body = "".join([_health_section(runs), _owners_section(latest), _regressions_section(reports),
                    _flaky_section(flaky, repeated_series(runs)), _localization_section(runs)])
    return (f'<!doctype html><html lang="en"><head><meta charset="utf-8">'
            f'<meta name="viewport" content="width=device-width,initial-scale=1"><title>{escape(title)}</title>'
            f"<style>{_STYLE}</style></head><body><main><h1>{escape(title)}</h1>"
            f'<p class="muted">Data through {escape(through)}. Built from the run index only.</p>'
            f'<div class="cards">{card_html}</div>{body}</main></body></html>\n')
