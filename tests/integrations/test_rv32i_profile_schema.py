"""The core-profile JSON Schema is current and never stricter than profile.py."""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest
import yaml

jsonschema = pytest.importorskip("jsonschema")

from crosslayer.core.errors import ConfigError  # noqa: E402
from crosslayer.integrations.rv32i import profile as prof  # noqa: E402
from crosslayer.integrations.rv32i.profile import CORES_DIR, builtin_cores, load_profile  # noqa: E402
from crosslayer.integrations.rv32i.profile_schema import core_profile_schema  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
SCHEMA = core_profile_schema()
VALIDATOR = jsonschema.Draft202012Validator(SCHEMA)

BASE = {
    "schema_version": 1, "name": "toy", "isa": "rv32i", "trace": "rvfi", "testbench": "tb.v", "sources": ["core.v"],
}
SIGNALS = {"clock": "tb.clk", "valid": "tb.v", "fields": {"pc": "tb.pc", "insn": "tb.insn"}}
MUTANT = {"target_subsystem": "alu", "edits": [{"file": "core.v", "find": "a", "replace": "b"}]}

# (name, overrides): each accepted by the loader and therefore by the schema.
ACCEPTED = [
    ("minimal", {}),
    ("trace list", {"trace": ["rvfi", "commit-log"]}),
    ("full", {"description": "x", "defines": ["A", "B=1"], "clock_period_ns": 5, "provenance": {"license": "ISC"}}),
    ("vcd", {"trace": ["vcd-signals"], "signals": SIGNALS}),
    ("mutant edits", {"mutants": {"m": MUTANT}}),
    ("mutant defines", {"mutants": {"m": {"target_subsystem": "alu", "defines": ["FAULT"]}}}),
    ("empty mutants", {"mutants": None}),
    ("optional mutant keys", {"mutants": {"m": {**MUTANT, "target_interface": None, "model_fault": None,
                                                 "description": "d"}}}),
]
# Rejected by the loader and expected to be rejected by the schema too.
REJECTED = [
    ("unknown key", {"surprise": 1}),
    ("bad version", {"schema_version": 2}),
    ("bad name", {"name": "Bad Name"}),
    ("bad isa", {"isa": "rv64i"}),
    ("bad trace", {"trace": "waves"}),
    ("duplicate traces", {"trace": ["rvfi", "rvfi"]}),
    ("no sources", {"sources": []}),
    ("bad define", {"defines": ["1bad"]}),
    ("zero period", {"clock_period_ns": 0}),
    ("vcd without signals", {"trace": "vcd-signals"}),
    ("unknown signal field", {"trace": "vcd-signals", "signals": {**SIGNALS, "fields": {"pc": "a", "insn": "b", "zz": "c"}}}),
    ("signals missing insn", {"trace": "vcd-signals", "signals": {**SIGNALS, "fields": {"pc": "a"}}}),
    ("mutant without change", {"mutants": {"m": {"target_subsystem": "alu"}}}),
    ("mutant extra key", {"mutants": {"m": {**MUTANT, "x": 1}}}),
    ("mutant name", {"mutants": {"Bad": MUTANT}}),
]


def _profile(tmp_path: Path, doc: dict) -> Path:
    for name in ("tb.v", "core.v"):
        (tmp_path / name).write_text("// synthetic\n", encoding="utf-8")
    path = tmp_path / "core.yaml"
    path.write_text(yaml.safe_dump(doc), encoding="utf-8")
    return path


def _errors(doc: dict) -> list[str]:
    return sorted(e.message for e in VALIDATOR.iter_errors(doc))


def test_schema_is_valid_and_checked_in_copy_is_current():
    jsonschema.Draft202012Validator.check_schema(SCHEMA)
    text = (ROOT / "schemas" / "rv32i-core-profile.schema.json").read_text(encoding="utf-8")
    assert text == json.dumps(SCHEMA, indent=2) + "\n", "regenerate with: crosslayer schema --write schemas"


@pytest.mark.parametrize("core", builtin_cores())
def test_builtin_profiles_validate_and_reference_the_schema(core):
    path = CORES_DIR / core / "core.yaml"
    assert _errors(yaml.safe_load(path.read_text(encoding="utf-8"))) == []
    first = path.read_text(encoding="utf-8").splitlines()[0]
    ref = first.split("$schema=", 1)[1]
    assert (path.parent / ref).resolve() == (ROOT / "schemas" / "rv32i-core-profile.schema.json").resolve()


def test_schema_properties_match_the_loader_keys():
    props = SCHEMA["properties"]
    assert set(props) == prof._TOP_KEYS
    assert set(props["signals"]["properties"]) == prof._SIGNAL_KEYS
    mutant = props["mutants"]["additionalProperties"]
    assert set(mutant["properties"]) == prof._MUTANT_KEYS
    assert set(mutant["properties"]["edits"]["items"]["properties"]) == prof._EDIT_KEYS
    assert set(SCHEMA["required"]) == set(prof._REQUIRED)


@pytest.mark.parametrize("label, extra", ACCEPTED, ids=[a[0] for a in ACCEPTED])
def test_schema_accepts_what_the_loader_accepts(tmp_path, label, extra):
    doc = {**BASE, **copy.deepcopy(extra)}
    load_profile(str(_profile(tmp_path, doc)))
    assert _errors(doc) == []


@pytest.mark.parametrize("label, extra", REJECTED, ids=[r[0] for r in REJECTED])
def test_schema_and_loader_both_reject(tmp_path, label, extra):
    doc = {**BASE, **copy.deepcopy(extra)}
    with pytest.raises(ConfigError):
        load_profile(str(_profile(tmp_path, doc)))
    assert _errors(doc) != []
