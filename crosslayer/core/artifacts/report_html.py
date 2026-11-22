"""Self-contained, interactive HTML report for a run bundle."""

from __future__ import annotations

from html import escape
from typing import Any, Sequence

from crosslayer.core.artifacts.report import format_timestamp
from crosslayer.core.contracts.model import ContractResult, FindingKind
from crosslayer.core.verdict import Verdict

_PIPELINE = (
    ("plan", "Validate and resolve the experiment"),
    ("build", "Build the system under test"),
    ("execute", "Run the experiment"),
    ("collect", "Collect raw artifacts"),
    ("normalize", "Normalize events"),
    ("evaluate", "Evaluate boundary contracts"),
    ("write", "Write and index the run bundle"),
)

_STYLE = """
:root { color-scheme: dark; --bg:#07111f; --panel:#0d1b2d; --line:#20334c; --text:#e8f0fa;
  --muted:#9fb0c5; --accent:#5eead4; --accent2:#60a5fa; --pass:#4ade80; --fail:#fb7185;
  --warn:#fbbf24; --error:#c084fc; font-family:Inter,ui-sans-serif,system-ui,sans-serif; }
* { box-sizing:border-box; } body { margin:0; background:radial-gradient(circle at top right,#102d46 0,#07111f 40%);
  color:var(--text); min-height:100vh; } a { color:var(--accent); } code { font-family:ui-monospace,SFMono-Regular,Consolas,monospace; }
.shell { max-width:1120px; margin:auto; padding:32px 20px 48px; } .eyebrow { color:var(--accent); letter-spacing:.14em;
  text-transform:uppercase; font-size:.75rem; font-weight:800; } h1 { margin:.35rem 0; font-size:clamp(2rem,5vw,3.7rem); line-height:1; }
.lede,.muted { color:var(--muted); } .panel { background:color-mix(in srgb,var(--panel) 94%,transparent); border:1px solid var(--line);
  border-radius:20px; padding:clamp(20px,4vw,38px); box-shadow:0 24px 80px #0005; }
.step-nav { display:grid; grid-template-columns:repeat(var(--steps,5),1fr); gap:8px; margin:28px 0 14px; } .step-button,.filter-button,
.control { border:1px solid var(--line); background:#0a1727; color:var(--muted); border-radius:12px; padding:10px 12px; cursor:pointer; }
.step-button:hover,.step-button:focus-visible,.filter-button:hover,.filter-button:focus-visible,.control:hover,.control:focus-visible { border-color:var(--accent); color:var(--text); outline:none; }
.step-button[aria-current="step"],.filter-button[aria-pressed="true"] { background:#12344a; border-color:var(--accent); color:var(--text); }
.step-number { display:block; color:var(--accent); font-weight:900; font-size:.75rem; } .progress { height:5px; background:#14263a;
  border-radius:99px; overflow:hidden; margin-bottom:28px; } .progress > span { display:block; width:20%; height:100%; background:linear-gradient(90deg,var(--accent),var(--accent2)); transition:width .2s ease; }
.js .step-panel { display:none; } .js .step-panel.is-active { display:block; animation:enter .22s ease-out; }
@keyframes enter { from { opacity:0; transform:translateY(8px); } }
.cards { display:grid; grid-template-columns:repeat(auto-fit,minmax(180px,1fr)); gap:12px; margin:20px 0; } .card { background:#091625;
  border:1px solid var(--line); border-radius:14px; padding:16px; } .card strong { display:block; font-size:1.35rem; margin-top:5px; overflow-wrap:anywhere; }
.badge { display:inline-block; padding:4px 9px; border:1px solid currentColor; border-radius:999px; font-size:.75rem; font-weight:900; text-transform:uppercase; }
.badge.pass { color:var(--pass); } .badge.fail { color:var(--fail); } .badge.error { color:var(--error); } .badge.inconclusive,.badge.skipped { color:var(--warn); }
.timeline { list-style:none; padding:0; counter-reset:stage; } .timeline li { counter-increment:stage; display:grid; grid-template-columns:38px 1fr auto;
  gap:12px; align-items:start; padding:13px 0; border-bottom:1px solid var(--line); } .timeline li::before { content:counter(stage); display:grid;
  place-items:center; width:32px; height:32px; border-radius:50%; background:#12344a; color:var(--accent); font-weight:900; }
.timeline strong { display:block; } .table-wrap { overflow:auto; border:1px solid var(--line); border-radius:14px; } table { border-collapse:collapse; width:100%; min-width:760px; }
th,td { text-align:left; padding:12px; border-bottom:1px solid var(--line); vertical-align:top; } th { color:var(--muted); font-size:.75rem; text-transform:uppercase; letter-spacing:.08em; }
.tools { display:flex; flex-wrap:wrap; gap:8px; margin:16px 0; } input[type="search"] { flex:1 1 240px; min-width:0; border:1px solid var(--line);
  background:#081524; color:var(--text); border-radius:12px; padding:11px 13px; } details { border:1px solid var(--line); border-radius:14px; padding:14px 16px; margin:12px 0; background:#091625; }
summary { cursor:pointer; font-weight:800; } .evidence { padding-left:20px; } .evidence code { overflow-wrap:anywhere; } .controls { display:flex; justify-content:space-between; gap:12px; margin-top:26px; }
.hyp-bar { display:block; height:6px; border-radius:99px; background:#14263a; margin-top:6px; overflow:hidden; } .hyp-bar > span { display:block; height:100%; background:linear-gradient(90deg,var(--accent),var(--accent2)); }
.control:disabled { opacity:.4; cursor:not-allowed; } .callout { border-left:4px solid var(--accent); background:#0a2131; padding:14px 16px; border-radius:0 12px 12px 0; }
.error-callout { border-left-color:var(--fail); } .flash { outline:2px solid var(--accent); outline-offset:-2px; } .sr-only { position:absolute; width:1px; height:1px; padding:0; margin:-1px; overflow:hidden; clip:rect(0,0,0,0); white-space:nowrap; border:0; }
@media (max-width:720px) { .step-nav { grid-template-columns:1fr; } .step-button { text-align:left; } .step-number { display:inline; margin-right:8px; } .shell { padding-top:20px; } }
@media print { body { background:#fff; color:#111; } .shell { max-width:none; } .step-nav,.progress,.controls,.tools { display:none; } .panel { box-shadow:none; border:0; padding:0; } .js .step-panel { display:block !important; page-break-inside:avoid; } }
"""

_SCRIPT = """
document.documentElement.classList.add('js');
(() => {
  const panels = [...document.querySelectorAll('[data-step-panel]')];
  const stepButtons = [...document.querySelectorAll('[data-step-target]')];
  const previous = document.querySelector('[data-previous]');
  const next = document.querySelector('[data-next]');
  const progress = document.querySelector('[data-progress]');
  const status = document.querySelector('[data-step-status]');
  let current = 0;
  function show(index, focusPanel = false) {
    current = Math.max(0, Math.min(index, panels.length - 1));
    panels.forEach((panel, i) => panel.classList.toggle('is-active', i === current));
    stepButtons.forEach((button, i) => {
      if (i === current) button.setAttribute('aria-current', 'step');
      else button.removeAttribute('aria-current');
    });
    previous.disabled = current === 0; next.disabled = current === panels.length - 1;
    progress.style.width = `${((current + 1) / panels.length) * 100}%`;
    progress.parentElement.setAttribute('aria-valuenow', String(current + 1));
    status.textContent = `Step ${current + 1} of ${panels.length}: ${stepButtons[current].dataset.title}`;
    if (focusPanel) panels[current].focus();
  }
  stepButtons.forEach((button, i) => button.addEventListener('click', () => show(i, true)));
  previous.addEventListener('click', () => show(current - 1, true));
  next.addEventListener('click', () => show(current + 1, true));
  document.addEventListener('keydown', event => {
    if (event.target instanceof Element && event.target.matches('input,button,summary')) return;
    if (event.key === 'ArrowRight') show(current + 1, true);
    if (event.key === 'ArrowLeft') show(current - 1, true);
  });
  const rows = [...document.querySelectorAll('[data-contract-row]')];
  const contractStep = panels.findIndex(panel => panel.querySelector('[data-contract-row]'));
  const search = document.querySelector('[data-contract-search]');
  const filters = [...document.querySelectorAll('[data-contract-filter]')];
  let verdict = 'all';
  function filterContracts() {
    const query = search.value.trim().toLowerCase();
    rows.forEach(row => { row.hidden = !((verdict === 'all' || row.dataset.verdict === verdict) && row.dataset.search.includes(query)); });
  }
  search.addEventListener('input', filterContracts);
  filters.forEach(button => button.addEventListener('click', () => {
    verdict = button.dataset.contractFilter;
    filters.forEach(item => item.setAttribute('aria-pressed', String(item === button)));
    filterContracts();
  }));
  document.querySelectorAll('[data-contract-link]').forEach(link => link.addEventListener('click', event => {
    const row = document.getElementById(link.dataset.contractLink);
    if (!row || contractStep < 0) return;
    event.preventDefault();
    verdict = 'all'; search.value = '';
    filters.forEach(item => item.setAttribute('aria-pressed', String(item.dataset.contractFilter === 'all')));
    filterContracts(); show(contractStep, false);
    row.scrollIntoView({block: 'center'}); row.classList.add('flash'); row.focus();
  }));
  show(0);
})();
"""


def _h(value: Any) -> str:
    return escape(str(value), quote=True)


def _badge(verdict: Any) -> str:
    value = str(verdict)
    return f'<span class="badge {_h(value)}">{_h(value)}</span>'


def _card(label: str, value: Any) -> str:
    return f'<div class="card"><span class="muted">{_h(label)}</span><strong>{_h(value)}</strong></div>'


def _contract_anchor(contract_id: str) -> str:
    return "contract-" + "".join(c if c.isalnum() or c in "-_" else f"_{ord(c):x}_" for c in contract_id)


def _contract_link(contract_id: str) -> str:
    anchor = _contract_anchor(contract_id)
    return f'<a href="#{_h(anchor)}" data-contract-link="{_h(anchor)}"><code>{_h(contract_id)}</code></a>'


def _localization_body(loc: dict[str, Any]) -> str:
    status = str(loc.get("status", "unknown"))
    reason = f": {_h(loc['reason'])}" if loc.get("reason") else ""
    body = (
        f'<p class="callout"><strong>Status: {_h(status)}</strong>{reason}. A ranking to act on, not a proof. '
        f'Method <code>{_h(loc.get("method", "unknown"))}</code>.</p>'
    )
    hypotheses = loc.get("hypotheses") or []
    if not hypotheses:
        return body + "<p>No contract failed, so there is nothing to rank.</p>"
    for rank, h in enumerate(hypotheses[:5], start=1):
        prob = float(h.get("probability", 0.0))
        evidence = "".join(
            f'<li>{_contract_link(str(e["contract"]))} ({_h(e.get("role", ""))}), boundary {_h(e.get("boundary", ""))}'
            + (f', first violation {_h(format_timestamp(e["first_violation_ns"]))}' if e.get("first_violation_ns") is not None else "")
            + "</li>"
            for e in h.get("evidence") or []
        )
        counter = "".join(
            f'<li>{_contract_link(str(e["contract"]))} passed ({_h(e.get("role", ""))}), boundary {_h(e.get("boundary", ""))}</li>'
            for e in h.get("counter_evidence") or []
        )
        iface = f' via <code>{_h(h["interface"])}</code>' if h.get("interface") else ""
        body += (
            f'<details{" open" if rank == 1 else ""}><summary>#{rank} <code>{_h(h.get("subsystem", "?"))}</code>{iface} '
            f'— probability {prob:.2f}</summary><span class="hyp-bar"><span style="width:{max(0.0, min(prob, 1.0)) * 100:.0f}%"></span></span>'
            f'<h3>Evidence (failing)</h3><ul class="evidence">{evidence or "<li>none</li>"}</ul>'
            f'<h3>Counter-evidence (passing)</h3><ul class="evidence">{counter or "<li>none</li>"}</ul></details>'
        )
    if loc.get("unresolved_contracts"):
        body += '<p class="muted">Not weighed (inconclusive): ' + ", ".join(
            _contract_link(str(c)) for c in loc["unresolved_contracts"]) + ".</p>"
    return body


def _panel(number: int, total: int, title: str, lead: str, body: str) -> str:
    return (
        f'<section class="step-panel" data-step-panel="{number}" tabindex="-1" aria-labelledby="step-{number}-title">'
        f'<p class="eyebrow">Step {number} of {total}</p><h2 id="step-{number}-title">{_h(title)}</h2>'
        f'<p class="lede">{_h(lead)}</p>{body}</section>'
    )


def render_html(
    manifest: dict[str, Any],
    results: Sequence[ContractResult],
    timings: dict[str, float] | None = None,
    localization: dict[str, Any] | None = None,
) -> str:
    """Render an offline report whose dynamic content is HTML-escaped.

    ``localization`` is the bundle's ``localization.json``; without it (older
    bundles) the report has five steps, with it a sixth ranks the hypotheses.
    """
    experiment = manifest["experiment"]
    adapter = manifest["adapter"]
    execution = manifest.get("execution") or {}
    event_data = manifest.get("events") or {}
    contract_data = manifest.get("contracts") or {}
    errors = manifest.get("errors") or []
    faults = manifest.get("faults") or []

    overview = '<div class="cards">' + "".join(
        (
            _card("Verdict", str(manifest["verdict"]).upper()),
            _card("Experiment", experiment["name"]),
            _card("Normalized events", event_data.get("count", 0)),
            _card("Contracts", contract_data.get("total", len(results))),
        )
    ) + "</div>"
    overview += (
        '<div class="callout"><strong>How to use this report</strong><br>'
        'Move through the numbered steps. Use the arrow keys away from controls, or the Previous and Next buttons.</div>'
    )

    config_cards = [
        _card("Run ID", manifest["run_id"]),
        _card("Adapter", f'{adapter["name"]} {adapter["adapter_version"]}'),
        _card("System", adapter["system"]),
        _card("Seed", experiment["seed"]),
        _card("Mode", manifest["mode"]),
        _card("Config hash", manifest["config_hash"][:12]),
    ]
    config = '<div class="cards">' + "".join(config_cards) + "</div>"
    if faults:
        config += '<h3>Injected faults</h3><ul>' + "".join(
            f'<li><code>{_h(fault["name"])}</code> — target {_h(fault.get("target_subsystem") or "unspecified")}</li>'
            for fault in faults
        ) + "</ul>"
    else:
        config += '<p class="callout">No faults were injected in this run.</p>'

    stage_items = []
    for stage, description in _PIPELINE:
        duration = (timings or {}).get(stage, 0.0)
        stage_items.append(
            f'<li><div><strong>{_h(stage.title())}</strong><span class="muted">{_h(description)}</span></div>'
            f'<code>{duration:.3f} s</code></li>'
        )
    pipeline = '<ol class="timeline">' + "".join(stage_items) + "</ol>"
    if execution:
        pipeline += (
            '<p class="callout"><strong>Execution result:</strong> '
            f'{_h(execution.get("status", "unknown"))}, exit code {_h(execution.get("exit_code"))}, '
            f'{float(execution.get("duration_s", 0)):.3f} s.</p>'
        )

    filters = '<div class="tools"><input type="search" data-contract-search aria-label="Search contracts" placeholder="Search contract, owner, boundary…">'
    for value in ("all", "pass", "fail", "inconclusive", "error", "skipped"):
        filters += (
            f'<button class="filter-button" type="button" data-contract-filter="{value}" '
            f'aria-pressed="{str(value == "all").lower()}">{_h(value.title())}</button>'
        )
    filters += "</div>"
    rows = []
    for result in results:
        contract = result.contract
        search_value = " ".join((contract.id, contract.title, contract.owner, contract.source, contract.destination)).lower()
        rows.append(
            f'<tr id="{_h(_contract_anchor(contract.id))}" tabindex="-1" data-contract-row data-verdict="{_h(result.verdict)}" data-search="{_h(search_value)}">'
            f'<td><code>{_h(contract.id)}</code><br><span class="muted">{_h(contract.title)}</span></td>'
            f'<td>{_h(contract.source)} → {_h(contract.destination)}</td><td>{_h(contract.owner)}</td>'
            f'<td>{_badge(result.verdict)}</td><td>{_h(result.summary)}</td></tr>'
        )
    contracts = filters + (
        '<div class="table-wrap"><table><thead><tr><th>Contract</th><th>Boundary</th><th>Owner</th><th>Verdict</th><th>Summary</th>'
        '</tr></thead><tbody>' + "".join(rows) + "</tbody></table></div>"
    )

    attention: list[str] = []
    for error in errors:
        attention.append(
            f'<div class="callout error-callout"><strong>Error during {_h(error.get("stage", "unknown"))}</strong><br>'
            f'{_h(error.get("message", "No message"))}</div>'
        )
    failing = [result for result in results if result.verdict in (Verdict.FAIL, Verdict.ERROR, Verdict.INCONCLUSIVE)]
    for index, result in enumerate(failing):
        findings = []
        for finding in result.findings:
            if finding.kind is not FindingKind.VIOLATION:
                continue
            refs = "".join(
                f'<li><code>{_h(ref.event_id)}</code> at {_h(format_timestamp(ref.timestamp_ns))}: '
                f'<code>{_h(ref.raw_artifact_ref or "no raw reference")}</code></li>'
                for ref in finding.evidence
            )
            findings.append(f'<li>{_h(finding.message)}' + (f'<ul class="evidence">{refs}</ul>' if refs else "") + "</li>")
        content = '<ul>' + "".join(findings) + "</ul>" if findings else f'<p>{_h(result.summary)}</p>'
        attention.append(
            f'<details{" open" if index == 0 else ""}><summary>{_badge(result.verdict)} <code>{_h(result.contract.id)}</code> '
            f'— {_h(format_timestamp(result.first_violation_ns))}</summary>{content}</details>'
        )
    if not attention:
        attention.append('<p class="callout"><strong>No action required.</strong> Every evaluated contract passed.</p>')
    attention.append(
        '<h3>Reproduce this exact configuration</h3><p>From the run-bundle directory:</p>'
        '<p class="card"><code>crosslayer replay .</code></p>'
        '<p class="muted">The configuration hash, normalized-event fingerprint, raw evidence, and dependency versions are stored beside this report.</p>'
    )

    sections = [
        ("Orient", "Start with the outcome and the scope of this run.", overview),
        ("Confirm the inputs", "Check the exact system, seed, configuration, and injected faults.", config),
        ("Follow the pipeline", "See each lifecycle stage in execution order and how long it took.", pipeline),
        ("Inspect the contracts", "Search and filter the checks that produced the run verdict.", contracts),
    ]
    nav_titles = ["Orient", "Inputs", "Pipeline", "Contracts"]
    if localization is not None:
        sections.append(("Locate the fault", "See which subsystem most likely started the failure, and why.",
                         _localization_body(localization)))
        nav_titles.append("Localize")
    sections.append(("Review evidence and reproduce", "Open failures in chronological context, then replay the bundle.",
                     "".join(attention)))
    nav_titles.append("Evidence")
    total = len(sections)
    panels = "".join(_panel(i, total, *section) for i, section in enumerate(sections, start=1))
    nav = f'<nav class="step-nav" style="--steps:{total}" aria-label="Report steps">' + "".join(
        f'<button class="step-button" type="button" data-step-target="{index}" data-title="{_h(title)}">'
        f'<span class="step-number">0{index}</span>{_h(title)}</button>'
        for index, title in enumerate(nav_titles, start=1)
    ) + "</nav>"

    return (
        '<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
        '<meta http-equiv="Content-Security-Policy" content="default-src \'none\'; style-src \'unsafe-inline\'; script-src \'unsafe-inline\'">'
        f'<title>CrossLayer run {_h(manifest["run_id"])}</title><style>{_STYLE}</style></head><body>'
        '<main class="shell"><p class="eyebrow">CrossLayer interactive run report</p>'
        f'<h1>{_h(experiment["name"])}</h1><p class="lede">Run <code>{_h(manifest["run_id"])}</code> · {_badge(manifest["verdict"])}</p>'
        f'{nav}<div class="progress" role="progressbar" aria-label="Report progress" aria-valuemin="1" aria-valuemax="{total}"><span data-progress></span></div>'
        '<p class="sr-only" aria-live="polite" data-step-status></p><noscript><p class="callout">JavaScript is disabled; all report steps remain visible below.</p></noscript>'
        f'<div class="panel">{panels}<div class="controls"><button class="control" type="button" data-previous>← Previous</button>'
        f'<button class="control" type="button" data-next>Next →</button></div></div></main><script>{_SCRIPT}</script></body></html>\n'
    )
