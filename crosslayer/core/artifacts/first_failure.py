"""The first failing event of a bundle, with observed and reference values side by side.

Generic: it reads only ``contracts.json`` and ``normalized_events.jsonl``. An
event attribute pairs with a reference when the event also carries
``<prefix>ref_<name>`` for ``<prefix><name>`` or ``<name>_ref`` for ``<name>``.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from crosslayer.core.artifacts.bundle import load_json

_RAW_REF = re.compile(r"^(?P<path>.+)#L(?P<line>\d+)$")


def pair_attributes(attributes: dict[str, Any]) -> list[dict[str, Any]]:
    """Observed/reference pairs found in ``attributes``, mismatches first."""
    pairs = []
    for key in sorted(attributes):
        prefix, dot, name = key.rpartition(".")
        if name.startswith("ref_") and len(name) > 4:
            observed_key = f"{prefix}{dot}{name[4:]}"
        elif name.endswith("_ref") and len(name) > 4:
            observed_key = f"{prefix}{dot}{name[:-4]}"
        else:
            continue
        if observed_key not in attributes:
            continue
        observed, reference = attributes[observed_key], attributes[key]
        pairs.append({"field": observed_key, "observed": observed, "reference": reference,
                      "match": observed == reference})
    return sorted(pairs, key=lambda p: p["match"])


def _raw_line(bundle: Path, ref: str | None) -> str | None:
    match = _RAW_REF.match(ref or "")
    if not match:
        return None
    path = bundle / match["path"]
    try:
        with path.open(encoding="utf-8", errors="replace") as handle:
            for number, line in enumerate(handle, start=1):
                if number == int(match["line"]):
                    return line.rstrip("\n")
    except OSError:
        return None
    return None


def _load_event(bundle: Path, event_id: str) -> dict[str, Any] | None:
    try:
        with (bundle / "normalized_events.jsonl").open(encoding="utf-8") as handle:
            for line in handle:
                if f'"{event_id}"' in line:
                    event = json.loads(line)
                    if event.get("event_id") == event_id:
                        return event
    except (OSError, json.JSONDecodeError):
        return None
    return None


def first_failure(bundle: Path) -> dict[str, Any] | None:
    """The earliest violation that points at an event, or ``None``."""
    doc = load_json(bundle, "contracts.json")
    candidates = [
        (f.get("timestamp_ns") or 0, item["result"]["contract_id"], f)
        for item in doc["contracts"]
        for f in item["result"].get("findings", [])
        if f.get("kind") == "violation" and f.get("evidence")
    ]
    if not candidates:
        return None
    _, contract_id, finding = min(candidates, key=lambda c: (c[0], c[1]))
    evidence = finding["evidence"][-1]
    event = _load_event(bundle, evidence["event_id"])
    attributes = (event or {}).get("attributes", {})
    return {
        "contract_id": contract_id,
        "message": finding["message"],
        "timestamp_ns": finding.get("timestamp_ns"),
        "event_id": evidence["event_id"],
        "event_type": evidence["event_type"],
        "subsystem": evidence["subsystem"],
        "raw_artifact_ref": evidence.get("raw_artifact_ref"),
        "raw_line": _raw_line(bundle, evidence.get("raw_artifact_ref")),
        "pairs": pair_attributes(attributes),
    }


def render_first_failure(failure: dict[str, Any]) -> str:
    lines = [f"first failure  {failure['contract_id']}  t={failure['timestamp_ns']} ns",
             f"  {failure['message']}",
             f"  event  {failure['event_id']} ({failure['event_type']}, {failure['subsystem']})"]
    if failure["raw_artifact_ref"]:
        lines.append(f"  raw    {failure['raw_artifact_ref']}")
    if failure["raw_line"] is not None:
        lines.append(f"         {failure['raw_line']}")
    pairs = failure["pairs"]
    if pairs:
        width = max(len(p["field"]) for p in pairs)
        lines.append(f"  {'field':<{width}}  {'observed':<14} {'reference':<14}")
        for p in pairs:
            lines.append(f"  {p['field']:<{width}}  {_fmt(p['observed']):<14} {_fmt(p['reference']):<14}"
                         f"{'' if p['match'] else ' <-- differs'}")
    return "\n".join(lines)


def _fmt(value: Any) -> str:
    return str(value)
