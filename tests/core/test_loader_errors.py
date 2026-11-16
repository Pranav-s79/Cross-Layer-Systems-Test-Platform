"""Loader errors must name the offending key path so messages stay actionable."""

from __future__ import annotations

import copy

import pytest

from crosslayer.core.contracts.loader import parse_contract, parse_contracts_document
from crosslayer.core.errors import ConfigError

VALID = {
    "id": "a.b",
    "title": "T",
    "owner": "o",
    "source": "x",
    "destination": "y",
    "rule": {"kind": "invariant", "select": {"event_type": "x"}, "expect": "True"},
}


def _with(**changes):
    raw = copy.deepcopy(VALID)
    raw.update(changes)
    return raw


def test_valid_baseline_parses():
    assert parse_contract(VALID, "doc").id == "a.b"


@pytest.mark.parametrize(
    "raw, expected_location, expected_text",
    [
        (_with(title=""), "doc(a.b).title", "non-empty string"),
        (_with(id="Bad Id"), "doc.id", "lowercase dotted"),
        (_with(severity="nope"), "doc(a.b).severity", "must be one of"),
        (_with(on_fail="nope"), "doc(a.b).on_fail", "must be one of"),
        (_with(evidence=5), "doc(a.b).evidence", "list of selectors"),
        (_with(tags=[1]), "doc(a.b).tags", "list of strings"),
        (_with(version=0), "doc(a.b).version", "integer >= 1"),
        (_with(version=True), "doc(a.b).version", "integer >= 1"),
        (_with(description=3), "doc(a.b).description", "must be a string"),
        (_with(unexpected=1), "doc", "unknown keys ['unexpected']"),
    ],
)
def test_contract_errors_name_the_key_path(raw, expected_location, expected_text):
    with pytest.raises(ConfigError) as info:
        parse_contract(raw, "doc")
    assert info.value.location is not None
    assert info.value.location.startswith(expected_location)
    assert expected_text in str(info.value)


def test_missing_required_keys_are_listed():
    raw = copy.deepcopy(VALID)
    del raw["owner"]
    del raw["rule"]
    with pytest.raises(ConfigError, match=r"doc: missing required keys \['owner', 'rule'\]"):
        parse_contract(raw, "doc")


def test_non_mapping_contract_names_its_index():
    doc = {"schema_version": 1, "contracts": ["oops"]}
    with pytest.raises(ConfigError, match=r"f\.yaml: contracts\[0\]: contract must be a mapping"):
        parse_contracts_document(doc, "f.yaml")


@pytest.mark.parametrize(
    "doc, text",
    [
        ([], "contract document must be a mapping"),
        ({"schema_version": 1, "extra": 1}, "unknown top-level keys ['extra']"),
        ({"schema_version": 2}, "schema_version must be 1, got 2"),
        ({"schema_version": 1, "contracts": {}}, "'contracts' must be a list"),
    ],
)
def test_document_errors_name_the_file(doc, text):
    with pytest.raises(ConfigError) as info:
        parse_contracts_document(doc, "f.yaml")
    assert str(info.value).startswith("f.yaml: ")
    assert text in str(info.value)


def test_bad_rule_error_is_located_under_the_contract():
    raw = _with(rule={"kind": "no_such_rule"})
    with pytest.raises(ConfigError) as info:
        parse_contract(raw, "doc")
    assert "doc(a.b).rule" in str(info.value)
