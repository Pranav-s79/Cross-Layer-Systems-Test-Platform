"""The contract-test runner: verifying contracts against known traces."""

from __future__ import annotations

from pathlib import Path

import pytest

from crosslayer.core.contracts.testing import load_contract_tests, run_contract_tests
from crosslayer.core.errors import ConfigError
from crosslayer.core.verdict import Verdict

CONTRACTS = """\
schema_version: 1
contracts:
  - id: demo.acked
    title: Every request is acknowledged within 1 ms
    owner: team
    source: client
    destination: server
    rule:
      kind: response
      trigger: {event_type: req}
      response: {event_type: ack}
      correlate_by: txn
      within_ms: 1
"""


def _write(tmp_path: Path, cases: str, contracts: str = CONTRACTS) -> Path:
    (tmp_path / "contracts.yaml").write_text(contracts, encoding="utf-8")
    path = tmp_path / "tests.yaml"
    path.write_text("schema_version: 1\ncontracts: [contracts.yaml]\ncases:\n" + cases, encoding="utf-8")
    return path


GOOD = """\
  - name: acked in time
    contract: demo.acked
    expect: pass
    events:
      - {t_us: 0, type: req, ids: {txn: 1}}
      - {t_us: 500, type: ack, ids: {txn: 1}}
"""
BAD = """\
  - name: never acked
    contract: demo.acked
    expect: fail
    expect_violations: 1
    events:
      - {t_ms: 0, type: req, ids: {txn: 1}}
      - {t_ms: 5, type: tick}
"""


def test_passing_cases_and_full_coverage(tmp_path):
    report = run_contract_tests(_write(tmp_path, GOOD + BAD))
    assert report.passed
    assert report.coverage() == {"demo.acked": {Verdict.PASS, Verdict.FAIL}}
    assert report.uncovered() == []


def test_wrong_expectation_is_reported(tmp_path):
    wrong = GOOD.replace("expect: pass", "expect: fail")
    [result] = run_contract_tests(_write(tmp_path, wrong)).results
    assert not result.passed and result.actual is Verdict.PASS
    assert "expected FAIL, got PASS" in result.problems[0]


def test_wrong_violation_count_is_reported(tmp_path):
    [result] = run_contract_tests(_write(tmp_path, BAD.replace("expect_violations: 1", "expect_violations: 2"))).results
    assert not result.passed and "expected 2 violations, got 1" in result.problems[0]


def test_missing_known_bad_case_is_uncovered(tmp_path):
    report = run_contract_tests(_write(tmp_path, GOOD))
    assert report.passed and report.uncovered() == ["demo.acked"]


def test_unordered_inline_events_are_sorted(tmp_path):
    shuffled = GOOD.replace(
        "      - {t_us: 0, type: req, ids: {txn: 1}}\n      - {t_us: 500, type: ack, ids: {txn: 1}}\n",
        "      - {t_us: 500, type: ack, ids: {txn: 1}}\n      - {t_us: 0, type: req, ids: {txn: 1}}\n",
    )
    assert run_contract_tests(_write(tmp_path, shuffled)).passed


def test_trace_file(tmp_path):
    (tmp_path / "trace.jsonl").write_text(
        '{"run_id":"r","event_id":"1","source_system":"s","subsystem":"client","timestamp_ns":0,'
        '"event_type":"req","correlation_ids":{"txn":"1"}}\n',
        encoding="utf-8",
    )
    cases = "  - {name: from file, contract: demo.acked, expect: fail, trace: trace.jsonl}\n"
    [result] = run_contract_tests(_write(tmp_path, cases)).results
    # a single request with nothing after it: deadline is past the end of the trace
    assert result.actual is Verdict.INCONCLUSIVE and not result.passed


def test_exact_microsecond_conversion(tmp_path):
    _, [case] = load_contract_tests(_write(tmp_path, "  - {name: t, contract: demo.acked, expect: pass, events: [{t_us: 1.5, type: req}]}\n"))
    assert case.events[0].timestamp_ns == 1_500


@pytest.mark.parametrize(
    "cases, match",
    [
        ("  - {name: a, contract: nope, expect: pass, events: []}\n", "unknown contract"),
        ("  - {name: a, contract: demo.acked, expect: maybe, events: []}\n", "expect must be"),
        ("  - {name: a, contract: demo.acked, expect: pass}\n", "exactly one of 'events' or 'trace'"),
        ("  - {name: a, contract: demo.acked, expect: pass, events: [], trace: x.jsonl}\n", "exactly one"),
        ("  - {name: a, contract: demo.acked, expect: pass, events: []}\n"
         "  - {name: a, contract: demo.acked, expect: fail, events: []}\n", "duplicate case name"),
        ("  - {name: a, contract: demo.acked, expect: pass, events: [{type: req}]}\n", "exactly one of t_ns"),
        ("  - {name: a, contract: demo.acked, expect: pass, events: [{t_ns: 1.5, type: req}]}\n", "integer"),
        ("  - {name: a, contract: demo.acked, expect: pass, events: [{t_us: 1, typ: req}]}\n", "unknown keys"),
        ("  - {name: a, contract: demo.acked, expect: pass, trace: missing.jsonl}\n", "cannot read trace"),
    ],
)
def test_configuration_errors(tmp_path, cases, match):
    with pytest.raises(ConfigError, match=match):
        load_contract_tests(_write(tmp_path, cases))
