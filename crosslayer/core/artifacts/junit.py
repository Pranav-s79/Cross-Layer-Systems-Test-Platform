"""JUnit XML output so any CI system can display contract results.

JUnit has no "inconclusive" outcome. INCONCLUSIVE and SKIPPED contracts are
written as ``<skipped>`` with the verdict in the message. The CLI exit code
(2 for INCONCLUSIVE) still keeps a pipeline from treating them as passes.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from typing import Any, Sequence

from crosslayer.core.contracts.model import ContractResult, FailurePolicy
from crosslayer.core.verdict import Verdict


def _finding_text(result: ContractResult) -> str:
    lines = [result.summary]
    for finding in result.findings[:20]:
        lines.append(f"- [{finding.kind}] {finding.message}")
        for ref in finding.evidence:
            lines.append(f"    {ref.event_id} ({ref.event_type}) -> {ref.raw_artifact_ref}")
    return "\n".join(lines)


def render_junit(manifest: dict[str, Any], results: Sequence[ContractResult]) -> str:
    execution = manifest.get("execution") or {}
    infra_error = manifest.get("errors") or []

    suite = ET.Element(
        "testsuite",
        name=manifest["experiment"]["name"],
        timestamp=manifest["started_at"],
        time=f"{execution.get('duration_s') or 0:.3f}",
    )
    properties = ET.SubElement(suite, "properties")
    for key in ("run_id", "verdict", "config_hash"):
        ET.SubElement(properties, "property", name=key, value=str(manifest.get(key)))

    counts = {"tests": 0, "failures": 0, "errors": 0, "skipped": 0}

    if infra_error:
        counts["tests"] += 1
        counts["errors"] += 1
        case = ET.SubElement(suite, "testcase", classname="crosslayer.run", name="execution")
        error = ET.SubElement(case, "error", message=infra_error[0]["message"][:500])
        error.text = "\n".join(f"[{e['stage']}] {e['message']}" for e in infra_error)

    for result in results:
        c = result.contract
        counts["tests"] += 1
        case = ET.SubElement(suite, "testcase", classname=f"crosslayer.{c.owner}", name=c.id)
        case_props = ET.SubElement(case, "properties")
        for key, value in (
            ("severity", str(c.severity)),
            ("boundary", f"{c.source} -> {c.destination}"),
            ("version", str(c.version)),
        ):
            ET.SubElement(case_props, "property", name=key, value=value)
        if c.on_fail is FailurePolicy.WARN and result.verdict is Verdict.FAIL:
            # A warn-only contract must not turn the CI test report red when
            # the run verdict ignores it. Keep it visible as a skip.
            counts["skipped"] += 1
            ET.SubElement(case, "skipped", message=f"WARNING (on_fail=warn): {result.summary}"[:500])
            continue
        if result.verdict is Verdict.FAIL:
            tag = "failure"
            counts["failures"] += 1
        elif result.verdict is Verdict.ERROR:
            tag = "error"
            counts["errors"] += 1
        elif result.verdict in (Verdict.INCONCLUSIVE, Verdict.SKIPPED):
            tag = "skipped"
            counts["skipped"] += 1
        else:
            continue
        node = ET.SubElement(case, tag, message=f"{result.verdict.upper()}: {result.summary}"[:500])
        if tag != "skipped":
            node.text = _finding_text(result)

    for key, value in counts.items():
        suite.set(key, str(value))
    ET.indent(suite)
    return '<?xml version="1.0" encoding="utf-8"?>\n' + ET.tostring(suite, encoding="unicode") + "\n"
