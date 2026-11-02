"""Contract loading, evidence gating, and verdict aggregation."""

from __future__ import annotations

import pytest

from crosslayer.core.contracts import (
    aggregate_verdict,
    ensure_unique_ids,
    evaluate_contract,
    load_contracts_file,
    parse_contract,
    parse_contracts_document,
)
from crosslayer.core.contracts.model import FailurePolicy
from crosslayer.core.errors import ConfigError
from crosslayer.core.verdict import Verdict
from tests.conftest import ev


def contract(**overrides):
    raw = {
        "id": "demo.contract",
        "title": "Demo",
        "owner": "team-a",
        "source": "a",
        "destination": "b",
        "rule": {"kind": "count", "select": {"event_type": "x.ok"}, "min": 1},
    }
    raw.update(overrides)
    return parse_contract(raw, "test")


def test_parse_defaults():
    c = contract()
    assert c.severity == "major" and c.on_fail is FailurePolicy.FAIL_RUN
    assert c.definition["id"] == "demo.contract"


@pytest.mark.parametrize(
    "overrides",
    [
        {"id": "Bad Id"},
        {"severity": "apocalyptic"},
        {"on_fail": "ignore"},
        {"owner": ""},
        {"tags": "not-a-list"},
        {"unexpected": 1},
        {"evidence": "x"},
    ],
)
def test_parse_errors(overrides):
    with pytest.raises(ConfigError):
        contract(**overrides)


def test_error_location_names_the_field():
    with pytest.raises(ConfigError) as info:
        contract(rule={"kind": "count", "select": {"event_type": "x"}, "min": "one"})
    assert "rule.min" in str(info.value)


def test_document_schema_version(tmp_path):
    with pytest.raises(ConfigError, match="schema_version"):
        parse_contracts_document({"schema_version": 2, "contracts": []}, "doc")
    path = tmp_path / "c.yaml"
    path.write_text(
        "schema_version: 1\ncontracts:\n  - id: a.b\n    title: t\n    owner: o\n    source: s\n"
        "    destination: d\n    rule: {kind: count, select: {event_type: x}, max: 0}\n",
        encoding="utf-8",
    )
    [c] = load_contracts_file(path)
    assert c.origin == str(path)


def test_duplicate_ids_rejected():
    with pytest.raises(ConfigError, match="duplicate"):
        ensure_unique_ids([contract(), contract()])


def test_precondition_not_met_skips():
    c = contract(precondition={"event_type": "system.boot"})
    assert evaluate_contract(c, [ev("x.ok", 0)]).verdict is Verdict.SKIPPED


def test_missing_evidence_makes_failure_inconclusive():
    c = contract(evidence=[{"event_type": "system.halt"}])
    result = evaluate_contract(c, [ev("x.other", 0)])  # rule alone would FAIL (no x.ok)
    assert result.verdict is Verdict.INCONCLUSIVE
    assert result.rule_verdict is Verdict.FAIL
    assert result.missing_evidence == [{"event_type": ["system.halt"]}]


def test_evidence_present_lets_rule_verdict_stand():
    c = contract(evidence={"event_type": "system.halt"})
    assert evaluate_contract(c, [ev("x.ok", 0), ev("system.halt", 1)]).verdict is Verdict.PASS


def test_rule_crash_is_error_not_fail():
    c = contract(rule={"kind": "invariant", "select": {"event_type": "x.ok"}, "expect": 'e.attributes["a.b"] + 1'})
    result = evaluate_contract(c, [ev("x.ok", 0, attrs={"a.b": "text"})])
    assert result.verdict is Verdict.ERROR
    assert "ExpressionRuntimeError" in result.summary and result.error


def _result(verdict, on_fail="fail_run", cid="c"):
    c = contract(id=cid, on_fail=on_fail)
    r = evaluate_contract(c, [ev("x.ok", 0)])
    r.verdict = verdict
    return r


@pytest.mark.parametrize(
    "verdicts, expected",
    [
        ([Verdict.PASS, Verdict.PASS], Verdict.PASS),
        ([Verdict.PASS, Verdict.FAIL], Verdict.FAIL),
        ([Verdict.FAIL, Verdict.ERROR], Verdict.ERROR),
        ([Verdict.PASS, Verdict.INCONCLUSIVE], Verdict.INCONCLUSIVE),
        ([Verdict.FAIL, Verdict.INCONCLUSIVE], Verdict.FAIL),
        ([Verdict.PASS, Verdict.SKIPPED], Verdict.PASS),
        ([Verdict.SKIPPED, Verdict.SKIPPED], Verdict.INCONCLUSIVE),
        ([], Verdict.INCONCLUSIVE),
    ],
)
def test_aggregate_verdict(verdicts, expected):
    results = [_result(v, cid=f"c{i}") for i, v in enumerate(verdicts)]
    assert aggregate_verdict(results) is expected


def test_warn_contracts_do_not_gate():
    results = [_result(Verdict.PASS), _result(Verdict.FAIL, on_fail="warn", cid="w")]
    assert aggregate_verdict(results) is Verdict.PASS
