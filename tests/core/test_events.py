from __future__ import annotations

import pytest

from crosslayer.core.telemetry import (
    NormalizedEvent,
    Severity,
    functional_fingerprint,
    order_events,
    read_events_jsonl,
    write_events_jsonl,
)
from tests.conftest import ev


def _event(**overrides):
    base = dict(
        run_id="r1",
        event_id="e1",
        source_system="sys",
        subsystem="amf",
        timestamp_ns=10,
        event_type="ue.registered",
    )
    base.update(overrides)
    return NormalizedEvent(**base)


def test_defaults_and_coercion():
    e = _event(severity="warning", correlation_ids={"ue": 7})
    assert e.severity is Severity.WARNING
    assert e.correlation_ids == {"ue": "7"}  # ids are normalized to strings


@pytest.mark.parametrize(
    "overrides",
    [
        {"event_type": "UE Registered"},
        {"timestamp_ns": 1.5},
        {"timestamp_ns": True},
        {"timestamp_ns": -1},
        {"run_id": ""},
        {"attributes": {"pc": 4}},  # not namespaced
        {"attributes": {"rtl.pc": float("nan")}},
        {"attributes": {"rtl.pc": object()}},
        {"correlation_ids": {"UE": "1"}},
        {"correlation_ids": {"ue": None}},
        {"severity": "loud"},
    ],
)
def test_rejects_invalid_events(overrides):
    with pytest.raises(ValueError):
        _event(**overrides)


def test_attributes_are_copied_not_aliased():
    attrs = {"rtl.pc": 4}
    e = _event(attributes=attrs)
    attrs["rtl.pc"] = 8
    assert e.attributes["rtl.pc"] == 4


def test_jsonl_round_trip(tmp_path):
    events = [
        _event(event_id="a", attributes={"rtl.pc": 4, "rtl.trace": [1, {"x": "y"}]}),
        _event(event_id="b", timestamp_ns=20, raw_artifact_ref="raw_artifacts/x.log#L2"),
    ]
    path = tmp_path / "events.jsonl"
    write_events_jsonl(path, events)
    assert read_events_jsonl(path) == events


def test_read_reports_line_number(tmp_path):
    path = tmp_path / "events.jsonl"
    path.write_text('{"run_id": "r"}\n', encoding="utf-8")
    with pytest.raises(ValueError, match=":1:"):
        read_events_jsonl(path)


def test_order_is_stable_for_equal_timestamps():
    a, b, c = ev("x.a", 5), ev("x.b", 5), ev("x.c", 1)
    assert [e.event_type for e in order_events([a, b, c])] == ["x.c", "x.a", "x.b"]


def test_fingerprint_ignores_run_id_but_not_content():
    e1 = _event(run_id="run-1")
    e2 = _event(run_id="run-2")
    assert functional_fingerprint([e1]) == functional_fingerprint([e2])
    assert functional_fingerprint([e1]) != functional_fingerprint([_event(timestamp_ns=11)])
