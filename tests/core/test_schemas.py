"""JSON Schemas must stay valid, current, and never stricter than the loaders."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

jsonschema = pytest.importorskip("jsonschema")

from crosslayer.core.contracts.loader import parse_contracts_document  # noqa: E402
from crosslayer.core.errors import ConfigError  # noqa: E402
from crosslayer.core.schemas import SCHEMAS  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]


def validator(name: str):
    schema = SCHEMAS[name]()
    jsonschema.Draft202012Validator.check_schema(schema)
    return jsonschema.Draft202012Validator(schema)


def load(path: Path):
    return yaml.safe_load(path.read_text(encoding="utf-8"))


@pytest.mark.parametrize("name", sorted(SCHEMAS))
def test_schema_is_valid_and_checked_in_copy_is_current(name):
    validator(name)
    checked_in = ROOT / "schemas" / f"{name}.schema.json"
    assert checked_in.read_text(encoding="utf-8") == json.dumps(SCHEMAS[name](), indent=2) + "\n", (
        "regenerate with: crosslayer schema --write schemas"
    )


def _repo_files():
    yield from (("contracts", p) for p in ROOT.glob("crosslayer/integrations/*/contracts.yaml"))
    yield from (("contract-tests", p) for p in ROOT.glob("crosslayer/integrations/*/contract_tests.yaml"))
    yield from (("experiment", p) for p in ROOT.glob("experiments/**/*.yaml") if "suite" not in p.name)
    yield from (("suite", p) for p in ROOT.glob("experiments/**/suite*.yaml"))


@pytest.mark.parametrize("name, path", list(_repo_files()), ids=lambda x: getattr(x, "name", x))
def test_every_repository_file_validates(name, path):
    errors = sorted(e.message for e in validator(name).iter_errors(load(path)))
    assert errors == []


def test_bundled_configuration_validates_as_experiment(tmp_path):
    from crosslayer.core.orchestrator import ExperimentRunner, load_experiment

    result = ExperimentRunner(tmp_path).run(load_experiment(ROOT / "experiments" / "reference" / "baseline.yaml"))
    config = load(result.bundle_dir / "configuration.yaml")
    assert list(validator("experiment").iter_errors(config)) == []


BASE = {"id": "a.b", "title": "t", "owner": "o", "source": "s", "destination": "d",
        "rule": {"kind": "count", "select": {"event_type": "x"}, "min": 1}}


def _doc(**overrides):
    return {"schema_version": 1, "contracts": [{**BASE, **overrides}]}


VALID = [
    _doc(),
    _doc(description=None, severity="critical", on_fail="warn", tags=["x"]),
    _doc(evidence={"event_type": "end"}, precondition={"subsystem": "s"}),
    _doc(rule={"kind": "response", "trigger": {"event_type": "a"}, "response": [{"event_type": "b"}],
               "correlate_by": "k", "within_ms": 0.5, "min_triggers": 0}),
    _doc(rule={"kind": "transitions", "select": {"event_type": "*"}, "state": {"attribute": "a.s"},
               "allowed": {"A": ["B"], "B": None}, "initial": "A", "allow_repeat": True}),
    _doc(rule={"kind": "threshold", "select": {"event_type": "x"}, "aggregate": "count", "op": ">=", "value": 1}),
    _doc(rule={"kind": "successive", "select": {"event_type": "x"}, "expect": "True", "when": "True",
               "group_by": "g", "min_pairs": 2}),
]

INVALID = [
    {"schema_version": 1, "contracts": [], "extra": 1},          # unknown top-level key
    {"schema_version": 2, "contracts": []},                       # wrong schema version
    _doc(owner=""),                                               # empty owner
    _doc(id="Bad Id"),                                            # id format
    _doc(severity="apocalyptic"),
    _doc(unexpected=1),                                           # unknown contract key
    _doc(rule={"kind": "nope"}),
    _doc(rule={"kind": "count", "select": {"event_type": "x"}}),  # neither min nor max
    _doc(rule={"kind": "count", "select": {}, "min": 1}),         # empty selector
    _doc(rule={"kind": "response", "trigger": {"event_type": "a"}, "response": {"event_type": "b"},
               "withn_ms": 5}),                                   # typo'd key
    _doc(rule={"kind": "response", "trigger": {"event_type": "a"}, "response": [], "within_ms": 5}),
    _doc(rule={"kind": "threshold", "select": {"event_type": "x"}, "aggregate": "max", "op": "<", "value": 1}),
    _doc(rule={"kind": "transitions", "select": {"event_type": "x"}, "state": "weird", "allowed": {"a": []}}),
]


@pytest.mark.parametrize("doc", VALID)
def test_schema_accepts_what_the_loader_accepts(doc):
    parse_contracts_document(doc, "test")  # loader accepts
    assert list(validator("contracts").iter_errors(doc)) == []


@pytest.mark.parametrize("doc", INVALID)
def test_schema_and_loader_both_reject(doc):
    with pytest.raises(ConfigError):
        parse_contracts_document(doc, "test")
    assert list(validator("contracts").iter_errors(doc)) != []
