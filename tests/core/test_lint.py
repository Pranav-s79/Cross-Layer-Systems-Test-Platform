"""Static contract checks."""

from __future__ import annotations

from crosslayer.core.adapters import EventSpec
from crosslayer.core.contracts import parse_contract
from crosslayer.core.contracts.lint import LintLevel, lint_contracts

CATALOG = [
    EventSpec("req", ("client",), ("net.size",), ("txn",)),
    EventSpec("ack", ("server",), ("net.latency_us",), ("txn",)),
    EventSpec("log.line", ("server",), ("net.raw_*",)),
]


def contract(rule, **extra):
    raw = {"id": "demo.c", "title": "t", "owner": "o", "source": "client", "destination": "server", "rule": rule}
    raw.update(extra)
    return parse_contract(raw, "test")


def codes(findings):
    return sorted(f.code for f in findings)


RESPONSE = {"kind": "response", "trigger": {"event_type": "req"}, "response": {"event_type": "ack"},
            "correlate_by": "txn", "within_ms": 5}


def test_clean_contract_has_no_findings():
    assert lint_contracts([contract(RESPONSE)], catalog=CATALOG, subsystems={"client", "server"}) == []


def test_undeclared_endpoint_and_critical_warn():
    c = contract(RESPONSE, source="nowhere", severity="critical", on_fail="warn")
    assert codes(lint_contracts([c], subsystems={"client", "server"})) == ["CL001", "CL002"]


def test_unreachable_state_and_missing_initial():
    rule = {"kind": "transitions", "select": {"event_type": "*"}, "state": "event_type",
            "allowed": {"req": ["ack"], "ack": [], "log.line": ["req"]}}
    assert codes(lint_contracts([contract(rule)])) == ["CL004"]
    [finding] = lint_contracts([contract({**rule, "initial": ["req"]})])
    assert finding.code == "CL003" and "log.line" in finding.message


def test_percentile_sample_floor():
    rule = {"kind": "threshold", "select": {"event_type": "ack"}, "attribute": "net.latency_us",
            "aggregate": "p95", "op": "<=", "value": 10, "min_samples": 19}
    [finding] = lint_contracts([contract(rule)])
    assert finding.code == "CL005" and finding.level is LintLevel.INFO and "fewer than 20" in finding.message
    assert lint_contracts([contract({**rule, "min_samples": 20})]) == []


def test_response_without_deadline_or_evidence():
    no_deadline = {k: v for k, v in RESPONSE.items() if k != "within_ms"}
    assert codes(lint_contracts([contract(no_deadline)])) == ["CL006"]
    assert lint_contracts([contract(no_deadline, evidence={"event_type": "log.line"})]) == []


def test_deadline_longer_than_timeout():
    assert codes(lint_contracts([contract({**RESPONSE, "within_ms": 60_000})], timeout_s=60)) == ["CL007"]


def test_misspelled_event_type_is_an_error():
    rule = {**RESPONSE, "trigger": {"event_type": "reqq"}}
    [finding] = lint_contracts([contract(rule)], catalog=CATALOG)
    assert finding.code == "CL010" and finding.level is LintLevel.ERROR and "trigger" in finding.message


def test_misspelled_attribute_in_expression_is_an_error():
    rule = {"kind": "invariant", "select": {"event_type": "ack"}, "expect": 'e.attributes["net.latncy_us"] < 5'}
    [finding] = lint_contracts([contract(rule)], catalog=CATALOG)
    assert finding.code == "CL011" and "net.latncy_us" in finding.message


def test_correlation_key_never_present_is_an_error():
    findings = lint_contracts([contract({**RESPONSE, "correlate_by": "ue"})], catalog=CATALOG)
    # reported once per side: neither triggers nor responses carry the id
    assert codes(findings) == ["CL011", "CL011"]
    assert {("on trigger" in f.message, "on response[0]" in f.message) for f in findings} == {(True, False), (False, True)}
    assert all("correlation id 'ue'" in f.message for f in findings)


def test_key_missing_on_some_matched_events_is_a_warning():
    rule = {"kind": "transitions", "select": {"event_type": "*"}, "state": "event_type", "group_by": "txn",
            "initial": ["req"], "allowed": {"req": ["ack"], "ack": [], "log.line": []}}
    findings = lint_contracts([contract(rule)], catalog=CATALOG)
    assert codes(findings) == ["CL003", "CL012"]
    assert any("log.line" in f.message for f in findings if f.code == "CL012")


def test_selector_attribute_filter_and_patterns():
    # filtering on an attribute narrows which events can match; patterns in the catalog count
    ok = {"kind": "count", "select": {"event_type": "log.line", "attributes": {"net.raw_x": 1}}, "min": 1}
    assert lint_contracts([contract(ok)], catalog=CATALOG) == []
    bad = {"kind": "count", "select": {"event_type": "req", "attributes": {"net.nope": 1}}, "min": 1}
    assert codes(lint_contracts([contract(bad)], catalog=CATALOG)) == ["CL011"]


def test_precondition_and_evidence_are_checked():
    c = contract(RESPONSE, precondition={"event_type": "boot"}, evidence={"subsystem": "nowhere"})
    assert codes(lint_contracts([c], catalog=CATALOG)) == ["CL010", "CL010"]


def test_errors_sort_before_warnings_and_info():
    rule = {"kind": "threshold", "select": {"event_type": "nope"}, "attribute": "a.b",
            "aggregate": "p99", "op": "<", "value": 1}
    findings = lint_contracts([contract(rule, severity="critical", on_fail="warn")], catalog=CATALOG)
    assert [f.level for f in findings] == [LintLevel.ERROR, LintLevel.WARNING, LintLevel.INFO]
