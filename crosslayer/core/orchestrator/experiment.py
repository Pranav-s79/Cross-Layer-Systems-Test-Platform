"""Versioned experiment configuration.

Example::

    schema_version: 1
    name: sensor-pipeline-baseline
    adapter: sensor_pipeline           # registered name or "module:Class"
    seed: 1234                         # required: no implicit randomness
    timeout_s: 60
    parameters: {packets: 400}         # adapter-specific, validated by the adapter
    faults:                            # optional, adapter must support fault injection
      - silent_drop
      - {name: corrupt_checksum, params: {every: 50}}
    contracts:                         # file paths (relative to this file) or inline contracts
      - contracts/extra.yaml
    include_adapter_contracts: true
    inputs: []                         # existing artifacts to analyze (log-only adapters)
"""

from __future__ import annotations

import copy
import hashlib
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Mapping, Sequence

import yaml

from crosslayer.core.adapters.base import FaultRequest
from crosslayer.core.contracts.loader import (
    load_contracts_file,
    parse_contract,
    read_document,
)
from crosslayer.core.contracts.model import BoundaryContract
from crosslayer.core.errors import ConfigError
from crosslayer.core.telemetry.store import canonical_json

SCHEMA_VERSION = 1
_REQUIRED = {"schema_version", "name", "adapter", "seed"}
_OPTIONAL = {
    "description",
    "timeout_s",
    "parameters",
    "faults",
    "contracts",
    "include_adapter_contracts",
    "inputs",
    "base_dir",
    "ground_truth",
}
_GROUND_TRUTH_KEYS = {"subsystem", "interface", "note"}
DEFAULT_TIMEOUT_S = 300.0


@dataclass(frozen=True)
class ExperimentConfig:
    name: str
    adapter: str
    seed: int
    base_dir: Path
    timeout_s: float = DEFAULT_TIMEOUT_S
    description: str = ""
    parameters: Mapping[str, Any] = field(default_factory=dict)
    faults: tuple[FaultRequest, ...] = ()
    contract_files: tuple[Path, ...] = ()
    inline_contracts: tuple[Mapping[str, Any], ...] = ()
    include_adapter_contracts: bool = True
    inputs: tuple[Path, ...] = ()
    source_path: Path | None = None
    ground_truth: Mapping[str, str] | None = None
    """Where the fault really is, for experiments whose fault is in the inputs
    (no injected FaultSpec to label it). Used only to score localization;
    excluded from the config hash."""

    def with_seed(self, seed: int) -> "ExperimentConfig":
        if isinstance(seed, bool) or not isinstance(seed, int) or seed < 0:
            raise ConfigError(f"seed must be a non-negative integer, got {seed!r}")
        return replace(self, seed=seed)

    def with_parameters(self, overrides: Mapping[str, Any]) -> "ExperimentConfig":
        """Merge ``overrides`` into ``parameters``; dotted keys address nested mappings."""
        merged: dict[str, Any] = copy.deepcopy(dict(self.parameters))
        for dotted, value in overrides.items():
            parts = dotted.split(".")
            node = merged
            for i, part in enumerate(parts[:-1]):
                child = node.setdefault(part, {})
                if not isinstance(child, dict):
                    raise ConfigError(f"cannot set {dotted!r}: parameters.{'.'.join(parts[:i + 1])} "
                                      f"is {type(child).__name__}, not a mapping", location="--set")
                node = child
            node[parts[-1]] = value
        return replace(self, parameters=merged)

    def load_contracts(self) -> list[BoundaryContract]:
        contracts: list[BoundaryContract] = []
        for path in self.contract_files:
            contracts.extend(load_contracts_file(path))
        source = str(self.source_path) if self.source_path else self.name
        for i, raw in enumerate(self.inline_contracts):
            contracts.append(parse_contract(raw, f"{source}: contracts[inline {i}]", origin=source))
        return contracts

    def resolved_document(self, adapter_contracts: Sequence[BoundaryContract] = ()) -> dict[str, Any]:
        """A self-contained copy for the run bundle.

        Every contract that will be evaluated, including the adapter's own, is
        inlined and ``include_adapter_contracts`` is turned off. A replay then
        checks exactly the same contracts even if the adapter's defaults change
        later. Paths are made absolute.
        """
        definitions = [dict(c.definition) for c in self.load_contracts()]
        if self.include_adapter_contracts:
            definitions += [dict(c.definition) for c in adapter_contracts]
        return {
            "schema_version": SCHEMA_VERSION,
            "name": self.name,
            "description": self.description,
            "adapter": self.adapter,
            "seed": self.seed,
            "timeout_s": self.timeout_s,
            "parameters": dict(self.parameters),
            "faults": [f.to_dict() for f in self.faults],
            "contracts": definitions,
            "include_adapter_contracts": False,
            "inputs": [str(p) for p in self.inputs],
            "base_dir": str(self.base_dir),
            **({"ground_truth": dict(self.ground_truth)} if self.ground_truth else {}),
        }

    def config_hash(self, adapter_contracts: Sequence[BoundaryContract] = ()) -> str:
        """Identity of the *effective* experiment, independent of where files live.

        It covers every contract actually evaluated, so an experiment and its
        bundled replay hash the same, and a change to an adapter's default
        contracts changes the hash. Absolute paths are dropped and input files
        are hashed by content.
        """
        doc = self.resolved_document(adapter_contracts)
        doc.pop("base_dir")
        doc.pop("include_adapter_contracts")
        doc.pop("ground_truth", None)  # a label, not configuration
        doc["inputs"] = [_file_sha256(p) for p in self.inputs]
        return hashlib.sha256(canonical_json(doc).encode("utf-8")).hexdigest()


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 16), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _parse_fault(raw: Any, location: str) -> FaultRequest:
    if isinstance(raw, str) and raw:
        return FaultRequest(name=raw)
    if isinstance(raw, Mapping) and isinstance(raw.get("name"), str):
        unknown = raw.keys() - {"name", "params"}
        if unknown:
            raise ConfigError(f"unknown keys {sorted(unknown)}", location=location)
        params = raw.get("params", {})
        if not isinstance(params, Mapping):
            raise ConfigError("'params' must be a mapping", location=location)
        return FaultRequest(name=raw["name"], params=dict(params))
    raise ConfigError("fault must be a name or {name, params}", location=location)


def parse_experiment(doc: Any, *, base_dir: Path, source: str, source_path: Path | None = None) -> ExperimentConfig:
    if not isinstance(doc, Mapping):
        raise ConfigError("experiment must be a mapping", location=source)
    missing = _REQUIRED - doc.keys()
    if missing:
        raise ConfigError(f"missing required keys {sorted(missing)}", location=source)
    unknown = doc.keys() - _REQUIRED - _OPTIONAL
    if unknown:
        raise ConfigError(f"unknown keys {sorted(unknown)}", location=source)
    if doc["schema_version"] != SCHEMA_VERSION:
        raise ConfigError(f"schema_version must be {SCHEMA_VERSION}", location=f"{source}: schema_version")

    for key in ("name", "adapter"):
        if not isinstance(doc[key], str) or not doc[key].strip():
            raise ConfigError("must be a non-empty string", location=f"{source}: {key}")
    seed = doc["seed"]
    if isinstance(seed, bool) or not isinstance(seed, int) or seed < 0:
        raise ConfigError("must be a non-negative integer", location=f"{source}: seed")
    timeout = doc.get("timeout_s", DEFAULT_TIMEOUT_S)
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or timeout <= 0:
        raise ConfigError("must be a positive number", location=f"{source}: timeout_s")
    parameters = doc.get("parameters") or {}
    if not isinstance(parameters, Mapping):
        raise ConfigError("must be a mapping", location=f"{source}: parameters")
    include = doc.get("include_adapter_contracts", True)
    if not isinstance(include, bool):
        raise ConfigError("must be true or false", location=f"{source}: include_adapter_contracts")
    description = doc.get("description") or ""  # an empty YAML key is None, not ""
    if not isinstance(description, str):
        raise ConfigError("must be a string", location=f"{source}: description")

    if "base_dir" in doc:
        if not isinstance(doc["base_dir"], str):
            raise ConfigError("must be a path string", location=f"{source}: base_dir")
        base_dir = Path(doc["base_dir"])

    faults_raw = doc.get("faults") or []
    if not isinstance(faults_raw, list):
        raise ConfigError("must be a list", location=f"{source}: faults")
    faults = tuple(_parse_fault(f, f"{source}: faults[{i}]") for i, f in enumerate(faults_raw))

    contract_files: list[Path] = []
    inline: list[Mapping[str, Any]] = []
    contracts_raw = doc.get("contracts") or []
    if not isinstance(contracts_raw, list):
        raise ConfigError("must be a list", location=f"{source}: contracts")
    for i, item in enumerate(contracts_raw):
        if isinstance(item, str):
            path = (base_dir / item).resolve()
            if not path.is_file():
                raise ConfigError(f"contract file not found: {path}", location=f"{source}: contracts[{i}]")
            contract_files.append(path)
        elif isinstance(item, Mapping):
            inline.append(dict(item))
        else:
            raise ConfigError("must be a file path or an inline contract", location=f"{source}: contracts[{i}]")

    inputs_raw = doc.get("inputs") or []
    if not isinstance(inputs_raw, list) or not all(isinstance(p, str) for p in inputs_raw):
        raise ConfigError("must be a list of paths", location=f"{source}: inputs")
    inputs = []
    for p in inputs_raw:
        path = (base_dir / p).resolve()
        if not path.is_file():
            raise ConfigError(f"input file not found: {path}", location=f"{source}: inputs")
        inputs.append(path)

    ground_truth = _parse_ground_truth(doc.get("ground_truth"), f"{source}: ground_truth")

    config = ExperimentConfig(
        name=doc["name"].strip(),
        adapter=doc["adapter"].strip(),
        seed=seed,
        base_dir=base_dir,
        timeout_s=float(timeout),
        description=description.strip(),
        parameters=dict(parameters),
        faults=faults,
        contract_files=tuple(contract_files),
        inline_contracts=tuple(inline),
        include_adapter_contracts=include,
        inputs=tuple(inputs),
        source_path=source_path,
        ground_truth=ground_truth,
    )
    config.load_contracts()  # fail fast on invalid contracts
    return config


def _parse_ground_truth(raw: Any, where: str) -> dict[str, str] | None:
    if raw is None:
        return None
    if not isinstance(raw, Mapping):
        raise ConfigError("must be a mapping with subsystem and optional interface and note", location=where)
    unknown = set(raw) - _GROUND_TRUTH_KEYS
    if unknown:
        raise ConfigError(f"unknown keys {sorted(map(str, unknown))} (allowed: {sorted(_GROUND_TRUTH_KEYS)})",
                          location=where)
    for key in _GROUND_TRUTH_KEYS:
        value = raw.get(key)
        if (value is not None or key == "subsystem") and (not isinstance(value, str) or not value.strip()):
            raise ConfigError("must be a non-empty string", location=f"{where}.{key}")
    return {k: raw[k].strip() for k in ("subsystem", "interface", "note") if raw.get(k) is not None}


def parse_overrides(items: Sequence[str]) -> dict[str, Any]:
    """Parse ``key=value`` pairs from ``--set``; values are YAML scalars or flow collections."""
    out: dict[str, Any] = {}
    for item in items:
        key, sep, raw = item.partition("=")
        key = key.strip()
        if not sep or not key:
            raise ConfigError(f"expected key=value, got {item!r}", location="--set")
        if any(not part for part in key.split(".")):
            raise ConfigError(f"empty segment in key {key!r}", location="--set")
        if key in out:
            raise ConfigError(f"{key!r} is set more than once", location="--set")
        try:
            value = yaml.safe_load(raw)
        except yaml.YAMLError as exc:
            raise ConfigError(f"value for {key!r} is not valid YAML: {raw!r}", location="--set") from exc
        out[key] = value
    return out


def load_experiment(path: Path) -> ExperimentConfig:
    path = path.resolve()
    return parse_experiment(read_document(path), base_dir=path.parent, source=str(path), source_path=path)
