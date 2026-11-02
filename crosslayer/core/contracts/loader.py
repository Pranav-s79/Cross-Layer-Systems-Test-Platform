"""Load boundary contracts from YAML/JSON documents.

Document format::

    schema_version: 1
    contracts:
      - id: pipeline.rx_accounted
        title: Every received packet is processed or explicitly dropped
        owner: firmware-rx
        source: sensor_rx
        destination: proc
        severity: critical          # critical | major | minor
        on_fail: fail_run           # fail_run | warn
        precondition: {event_type: system.boot}
        evidence: [{event_type: system.halt}]
        rule:
          kind: response
          ...

Validation is strict: unknown keys are errors, so a typo such as
``withn_ms`` fails loudly instead of silently disabling a timing check.
"""

from __future__ import annotations

import json
import re
from collections import Counter
from pathlib import Path
from typing import Any, Iterable, Mapping

import yaml

from crosslayer.core.contracts.model import BoundaryContract, Criticality, FailurePolicy
from crosslayer.core.contracts.rules import parse_rule
from crosslayer.core.contracts.selectors import parse_selector
from crosslayer.core.errors import ConfigError

SCHEMA_VERSION = 1
_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*(\.[a-z0-9_-]+)*$")
_REQUIRED = {"id", "title", "owner", "source", "destination", "rule"}
_OPTIONAL = {"description", "severity", "on_fail", "precondition", "evidence", "tags"}


def _text(raw: Mapping[str, Any], key: str, location: str) -> str:
    value = raw[key]
    if not isinstance(value, str) or not value.strip():
        raise ConfigError("must be a non-empty string", location=f"{location}.{key}")
    return value.strip()


def parse_contract(raw: Any, location: str, origin: str = "") -> BoundaryContract:
    if not isinstance(raw, Mapping):
        raise ConfigError("contract must be a mapping", location=location)
    missing = _REQUIRED - raw.keys()
    if missing:
        raise ConfigError(f"missing required keys {sorted(missing)}", location=location)
    unknown = raw.keys() - _REQUIRED - _OPTIONAL
    if unknown:
        raise ConfigError(f"unknown keys {sorted(unknown)}", location=location)

    contract_id = _text(raw, "id", location)
    if not _ID_RE.match(contract_id):
        raise ConfigError(
            f"id {contract_id!r} must be lowercase dotted, e.g. 'ran.registration_timeout'",
            location=f"{location}.id",
        )
    location = f"{location}({contract_id})"

    try:
        severity = Criticality(raw.get("severity", Criticality.MAJOR))
    except ValueError:
        raise ConfigError(f"must be one of {[c.value for c in Criticality]}", location=f"{location}.severity") from None
    try:
        on_fail = FailurePolicy(raw.get("on_fail", FailurePolicy.FAIL_RUN))
    except ValueError:
        raise ConfigError(f"must be one of {[p.value for p in FailurePolicy]}", location=f"{location}.on_fail") from None

    precondition = None
    if "precondition" in raw:
        precondition = parse_selector(raw["precondition"], f"{location}.precondition")

    evidence_raw = raw.get("evidence", [])
    if isinstance(evidence_raw, Mapping):
        evidence_raw = [evidence_raw]
    if not isinstance(evidence_raw, list):
        raise ConfigError("must be a selector or list of selectors", location=f"{location}.evidence")
    evidence = tuple(parse_selector(s, f"{location}.evidence[{i}]") for i, s in enumerate(evidence_raw))

    tags = raw.get("tags", [])
    if not isinstance(tags, list) or not all(isinstance(t, str) for t in tags):
        raise ConfigError("must be a list of strings", location=f"{location}.tags")

    description = raw.get("description", "")
    if not isinstance(description, str):
        raise ConfigError("must be a string", location=f"{location}.description")

    return BoundaryContract(
        id=contract_id,
        title=_text(raw, "title", location),
        owner=_text(raw, "owner", location),
        source=_text(raw, "source", location),
        destination=_text(raw, "destination", location),
        rule=parse_rule(raw["rule"], f"{location}.rule"),
        description=description.strip(),
        severity=severity,
        on_fail=on_fail,
        precondition=precondition,
        evidence=evidence,
        tags=tuple(tags),
        origin=origin,
        definition=dict(raw),
    )


def parse_contract_list(items: Any, source: str) -> list[BoundaryContract]:
    if not isinstance(items, list):
        raise ConfigError("'contracts' must be a list", location=source)
    return [parse_contract(item, f"{source}: contracts[{i}]", origin=source) for i, item in enumerate(items)]


def parse_contracts_document(doc: Any, source: str) -> list[BoundaryContract]:
    if not isinstance(doc, Mapping):
        raise ConfigError("contract document must be a mapping", location=source)
    unknown = doc.keys() - {"schema_version", "contracts"}
    if unknown:
        raise ConfigError(f"unknown top-level keys {sorted(unknown)}", location=source)
    if doc.get("schema_version") != SCHEMA_VERSION:
        raise ConfigError(
            f"schema_version must be {SCHEMA_VERSION}, got {doc.get('schema_version')!r}",
            location=source,
        )
    return parse_contract_list(doc.get("contracts", []), source)


def read_document(path: Path) -> Any:
    """Read YAML or JSON. ``yaml.safe_load`` never constructs arbitrary
    Python objects, unlike ``yaml.load``."""
    text = path.read_text(encoding="utf-8")
    if path.suffix.lower() == ".json":
        return json.loads(text)
    return yaml.safe_load(text)


def load_contracts_file(path: Path) -> list[BoundaryContract]:
    try:
        doc = read_document(path)
    except (OSError, yaml.YAMLError, json.JSONDecodeError) as exc:
        raise ConfigError(f"cannot read contracts: {exc}", location=str(path)) from exc
    return parse_contracts_document(doc, str(path))


def ensure_unique_ids(contracts: Iterable[BoundaryContract]) -> None:
    contracts = list(contracts)
    counts = Counter(c.id for c in contracts)
    duplicates = sorted(cid for cid, n in counts.items() if n > 1)
    if duplicates:
        origins = {cid: sorted({c.origin for c in contracts if c.id == cid}) for cid in duplicates}
        raise ConfigError(f"duplicate contract ids: {origins}")
