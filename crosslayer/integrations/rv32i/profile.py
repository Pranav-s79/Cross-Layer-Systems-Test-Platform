"""Core profiles: a declarative description of one CPU core for the rv32i adapter.

A profile is a ``core.yaml`` next to the core's RTL. It names the sources,
the testbench, the trace format, compile-time defines, provenance, and the
RTL mutants the core supports. Built-in profiles live in ``cores/<name>/``;
an experiment can also point ``parameters.core`` at any ``core.yaml`` (or
the directory that holds one), so a new core needs no Python changes.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Collection, Mapping

import yaml

from crosslayer.core.errors import ConfigError
from crosslayer.integrations.rv32i.traces import TRACE_FORMATS

CORES_DIR = Path(__file__).resolve().parent / "cores"
PROFILE_NAME = "core.yaml"
DEFAULT_CORE = "picorv32"
SCHEMA_VERSION = 1
ISAS = frozenset({"rv32i"})

_NAME_RE = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")
_DEFINE_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(=[^\s]*)?$")
_TOP_KEYS = {
    "schema_version", "name", "description", "isa", "trace", "testbench", "sources", "defines",
    "clock_period_ns", "provenance", "mutants",
}
_REQUIRED = ("schema_version", "name", "isa", "trace", "testbench", "sources")
_MUTANT_KEYS = {"description", "target_subsystem", "target_interface", "model_fault", "edits", "defines"}
_EDIT_KEYS = {"file", "find", "replace"}


@dataclass(frozen=True)
class Edit:
    file: str
    find: str
    replace: str


@dataclass(frozen=True)
class Mutant:
    name: str
    description: str
    target_subsystem: str
    target_interface: str | None
    model_fault: str | None
    edits: tuple[Edit, ...]
    defines: tuple[str, ...]


@dataclass(frozen=True)
class CoreProfile:
    name: str
    path: Path
    description: str
    isa: str
    trace: str
    testbench: str
    sources: tuple[str, ...]
    defines: tuple[str, ...]
    clock_period_ns: int
    provenance: Mapping[str, Any]
    mutants: Mapping[str, Mutant] = field(default_factory=dict)

    @property
    def root(self) -> Path:
        return self.path.parent

    def file(self, name: str) -> Path:
        return self.root / name

    def digest(self) -> str:
        """Short SHA-256 over the profile, testbench, and sources, in order."""
        h = hashlib.sha256()
        for name in (PROFILE_NAME, self.testbench, *self.sources):
            h.update(name.encode() + b"\0")
            h.update(self.file(name).read_bytes())
        return h.hexdigest()[:12]

    def summary(self) -> dict[str, Any]:
        return {
            "name": self.name, "isa": self.isa, "trace": self.trace, "sources": list(self.sources),
            "testbench": self.testbench, "sha256": self.digest(), "provenance": dict(self.provenance),
        }


def builtin_cores() -> list[str]:
    return sorted(p.parent.name for p in CORES_DIR.glob(f"*/{PROFILE_NAME}"))


def profile_path(core: str, base_dir: Path | None = None) -> Path:
    """Resolve a built-in core name, a ``core.yaml`` path, or a directory holding one."""
    if _NAME_RE.match(core) and (CORES_DIR / core / PROFILE_NAME).is_file():
        return CORES_DIR / core / PROFILE_NAME
    path = Path(core)
    if not path.is_absolute() and base_dir is not None:
        path = base_dir / path
    if path.is_dir():
        path = path / PROFILE_NAME
    if not path.is_file():
        raise ConfigError(f"unknown core {core!r} (built-in: {builtin_cores()}, or a path to a {PROFILE_NAME})",
                          location="parameters.core")
    return path.resolve()


def load_profile(
    core: str,
    base_dir: Path | None = None,
    *,
    subsystems: Collection[str] = (),
    interfaces: Collection[str] = (),
    model_faults: Collection[str] = (),
) -> CoreProfile:
    """Load and validate a profile. Empty ``subsystems`` / ``interfaces`` /
    ``model_faults`` skip the corresponding cross-checks."""
    path = profile_path(core, base_dir)
    where = str(path)
    try:
        doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ConfigError(f"invalid YAML: {exc}", location=where) from None
    if not isinstance(doc, dict):
        raise ConfigError("must be a mapping", location=where)
    _keys(doc, _TOP_KEYS, where)
    for key in _REQUIRED:
        if key not in doc:
            raise ConfigError("is required", location=f"{where}: {key}")
    if doc["schema_version"] != SCHEMA_VERSION:
        raise ConfigError(f"unsupported schema_version (expected {SCHEMA_VERSION})", location=f"{where}: schema_version")
    name = _str(doc["name"], f"{where}: name")
    if not _NAME_RE.match(name):
        raise ConfigError("must be lowercase letters, digits, '_' or '-'", location=f"{where}: name")
    isa = _choice(doc["isa"], ISAS, f"{where}: isa")
    trace = _choice(doc["trace"], TRACE_FORMATS, f"{where}: trace")
    root = path.parent
    testbench = _file(root, doc["testbench"], f"{where}: testbench")
    raw_sources = _str_list(doc.get("sources"), f"{where}: sources", nonempty=True)
    sources = [_file(root, s, f"{where}: sources[{i}]") for i, s in enumerate(raw_sources)]
    if len(set(sources)) != len(sources) or testbench in sources:
        raise ConfigError("must list each file once, separate from the testbench", location=f"{where}: sources")
    defines = _defines(doc.get("defines", []), f"{where}: defines")
    period = doc.get("clock_period_ns", 10)
    if isinstance(period, bool) or not isinstance(period, int) or period < 1:
        raise ConfigError("must be a positive integer", location=f"{where}: clock_period_ns")
    provenance = doc.get("provenance", {})
    if not isinstance(provenance, dict):
        raise ConfigError("must be a mapping", location=f"{where}: provenance")

    raw_mutants = doc.get("mutants", {}) or {}
    if not isinstance(raw_mutants, dict):
        raise ConfigError("must be a mapping of mutant name to definition", location=f"{where}: mutants")
    mutants: dict[str, Mutant] = {}
    for mname, spec in raw_mutants.items():
        loc = f"{where}: mutants.{mname}"
        if not isinstance(mname, str) or not _NAME_RE.match(mname):
            raise ConfigError("mutant names must be lowercase identifiers", location=loc)
        mutants[mname] = _mutant(mname, spec, loc, sources, subsystems, interfaces, model_faults)

    return CoreProfile(
        name=name, path=path, description=str(doc.get("description", "")), isa=isa, trace=trace,
        testbench=testbench, sources=tuple(sources), defines=tuple(defines), clock_period_ns=period,
        provenance=provenance, mutants=mutants,
    )


def _mutant(
    name: str, spec: Any, loc: str, sources: list[str],
    subsystems: Collection[str], interfaces: Collection[str], model_faults: Collection[str],
) -> Mutant:
    if not isinstance(spec, dict):
        raise ConfigError("must be a mapping", location=loc)
    _keys(spec, _MUTANT_KEYS, loc)
    subsystem = _str(spec.get("target_subsystem"), f"{loc}.target_subsystem")
    if subsystems and subsystem not in subsystems:
        raise ConfigError(f"unknown subsystem {subsystem!r} (adapter declares {sorted(subsystems)})",
                          location=f"{loc}.target_subsystem")
    interface = spec.get("target_interface")
    if interface is not None and (not isinstance(interface, str) or (interfaces and interface not in interfaces)):
        raise ConfigError(f"unknown interface {interface!r} (adapter declares {sorted(interfaces)})",
                          location=f"{loc}.target_interface")
    model_fault = spec.get("model_fault")
    if model_fault is not None and (not isinstance(model_fault, str) or (model_faults and model_fault not in model_faults)):
        raise ConfigError(f"unknown reference-model fault {model_fault!r} (known: {sorted(model_faults)})",
                          location=f"{loc}.model_fault")
    raw_edits = spec.get("edits", [])
    if not isinstance(raw_edits, list):
        raise ConfigError("must be a list", location=f"{loc}.edits")
    edits = []
    for i, raw in enumerate(raw_edits):
        eloc = f"{loc}.edits[{i}]"
        if not isinstance(raw, dict):
            raise ConfigError("must be a mapping with file, find and replace", location=eloc)
        _keys(raw, _EDIT_KEYS, eloc)
        file = _str(raw.get("file"), f"{eloc}.file")
        if file not in sources:
            raise ConfigError(f"must name one of the core's sources {sources}", location=f"{eloc}.file")
        find, replace = _str(raw.get("find"), f"{eloc}.find"), raw.get("replace")
        if not isinstance(replace, str) or replace == find:
            raise ConfigError("must be a string that differs from find", location=f"{eloc}.replace")
        edits.append(Edit(file, find, replace))
    defines = _defines(spec.get("defines", []), f"{loc}.defines")
    if not edits and not defines:
        raise ConfigError("needs at least one edit or define", location=loc)
    return Mutant(name, str(spec.get("description", "")), subsystem, interface, model_fault, tuple(edits),
                  tuple(defines))


def apply_mutants(profile: CoreProfile, names: Collection[str]) -> tuple[dict[str, str], list[str]]:
    """Return the mutated text of every source and the extra defines.

    Raises ``ConfigError`` if a mutant's ``find`` text does not match exactly
    once, so a mutant that no longer fits the RTL can't silently do nothing.
    """
    texts = {s: profile.file(s).read_text(encoding="utf-8") for s in profile.sources}
    defines: list[str] = []
    for name in names:
        mutant = profile.mutants[name]
        for i, edit in enumerate(mutant.edits):
            found = texts[edit.file].count(edit.find)
            if found != 1:
                raise ConfigError(f"no longer applies to {edit.file} (expected 1 match, found {found})",
                                  location=f"{profile.path}: mutants.{name}.edits[{i}].find")
            texts[edit.file] = texts[edit.file].replace(edit.find, edit.replace)
        defines.extend(mutant.defines)
    return texts, defines


def _keys(doc: Mapping[str, Any], allowed: set[str], loc: str) -> None:
    unknown = set(doc) - allowed
    if unknown:
        raise ConfigError(f"unknown keys {sorted(map(str, unknown))} (allowed: {sorted(allowed)})", location=loc)


def _str(value: Any, loc: str) -> str:
    if not isinstance(value, str) or not value:
        raise ConfigError("must be a non-empty string", location=loc)
    return value


def _choice(value: Any, allowed: frozenset[str], loc: str) -> str:
    text = _str(value, loc)
    if text not in allowed:
        raise ConfigError(f"unsupported value {text!r} (supported: {sorted(allowed)})", location=loc)
    return text


def _str_list(value: Any, loc: str, *, nonempty: bool = False) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(v, str) and v for v in value) or (nonempty and not value):
        raise ConfigError("must be a list of non-empty strings" + (" with at least one entry" if nonempty else ""),
                          location=loc)
    return list(value)


def _defines(value: Any, loc: str) -> list[str]:
    defines = _str_list(value, loc)
    for d in defines:
        if not _DEFINE_RE.match(d):
            raise ConfigError(f"invalid define {d!r} (expected NAME or NAME=VALUE)", location=loc)
    return defines


def _file(root: Path, value: Any, loc: str) -> str:
    """A relative path inside the profile's directory, in POSIX form."""
    name = _str(value, loc)
    path = Path(name)
    if path.is_absolute() or ".." in path.parts or "\\" in name:
        raise ConfigError("must be a relative path inside the profile's directory, using '/'", location=loc)
    if not (root / path).is_file():
        raise ConfigError(f"file not found: {root / path}", location=loc)
    return path.as_posix()
