"""Direct tests for event selectors: each filter form and its error messages."""

from __future__ import annotations

import pytest

from crosslayer.core.contracts.selectors import Selector, parse_selector
from crosslayer.core.errors import ConfigError
from crosslayer.core.telemetry.events import NormalizedEvent, Severity


def _event(**overrides) -> NormalizedEvent:
    base = dict(
        run_id="r",
        event_id="e1",
        source_system="sim",
        subsystem="rx.fifo",
        timestamp_ns=1,
        event_type="packet.received",
        severity=Severity.INFO,
        attributes={"pkt.seq": 3, "pkt.ok": True, "pkt.name": "a"},
    )
    base.update(overrides)
    return NormalizedEvent(**base)


@pytest.mark.parametrize(
    "raw, event, expected",
    [
        ({"event_type": "packet.received"}, {}, True),
        ({"event_type": "packet.received"}, {"event_type": "packet.sent"}, False),
        ({"event_type": "packet.*"}, {}, True),
        ({"event_type": "packet.*"}, {"event_type": "frame.sent"}, False),
        ({"event_type": ["frame.*", "packet.*"]}, {}, True),
        ({"event_type": "Packet.*"}, {}, False),  # patterns are case-sensitive
        ({"subsystem": "rx.*"}, {}, True),
        ({"subsystem": "tx.*"}, {}, False),
        ({"source_system": "sim"}, {}, True),
        ({"source_system": ["a", "sim"]}, {}, True),
        ({"source_system": "si*"}, {}, False),  # exact match, no patterns
        ({"severity": "info"}, {}, True),
        ({"severity": ["warning", "error"]}, {}, False),
        ({"severity": ["warning", "error"]}, {"severity": Severity.ERROR}, True),
        ({"attributes": {"pkt.seq": 3}}, {}, True),
        ({"attributes": {"pkt.seq": 4}}, {}, False),
        ({"attributes": {"pkt.missing": 1}}, {}, False),
        ({"attributes": {"pkt.seq": 3, "pkt.name": "a"}}, {}, True),
        ({"attributes": {"pkt.seq": 3, "pkt.name": "b"}}, {}, False),
        ({"attributes": {"pkt.ok": 1}}, {}, False),  # True is not 1
        ({"attributes": {"pkt.seq": True}}, {"attributes": {"pkt.seq": 1}}, False),
        ({"where": "e.attributes['pkt.seq'] > 2"}, {}, True),
        ({"where": "e.attributes['pkt.seq'] > 5"}, {}, False),
        ({"where": "e.attributes['pkt.nope'] > 5"}, {}, False),  # missing evidence: no match
        (
            {"event_type": "packet.*", "attributes": {"pkt.seq": 3}, "where": "e.attributes['pkt.seq'] > 2"},
            {},
            True,
        ),
        ({"event_type": "packet.*", "attributes": {"pkt.seq": 4}}, {}, False),  # conjunction
    ],
)
def test_matches(raw, event, expected):
    assert parse_selector(raw, "sel").matches(_event(**event)) is expected


def test_empty_selector_object_matches_everything():
    assert Selector().matches(_event())


@pytest.mark.parametrize(
    "raw, location, text",
    [
        ("packet.*", "sel", "must be a mapping"),
        (["packet.*"], "sel", "must be a mapping"),
        ({}, "sel", "at least one filter"),
        ({"bogus": 1}, "sel", "unknown selector keys ['bogus']"),
        ({"event_type": ""}, "sel.event_type", "non-empty string or list"),
        ({"event_type": []}, "sel.event_type", "non-empty string or list"),
        ({"event_type": [1]}, "sel.event_type", "non-empty string or list"),
        ({"event_type": ["a", ""]}, "sel.event_type", "non-empty string or list"),
        ({"subsystem": 5}, "sel.subsystem", "non-empty string or list"),
        ({"source_system": []}, "sel.source_system", "non-empty string or list"),
        ({"severity": None}, "sel.severity", "non-empty string or list"),
        ({"attributes": {}}, "sel.attributes", "non-empty mapping"),
        ({"attributes": ["a"]}, "sel.attributes", "non-empty mapping"),
        ({"where": "__import__('os')"}, "sel.where", ""),
    ],
)
def test_parse_errors_name_location(raw, location, text):
    with pytest.raises(ConfigError) as exc:
        parse_selector(raw, "sel")
    assert location in str(exc.value)
    assert text in str(exc.value)


def test_to_dict_round_trips():
    raw = {
        "event_type": ["packet.*"],
        "subsystem": ["rx.*"],
        "source_system": ["sim"],
        "severity": ["info"],
        "attributes": {"pkt.seq": 3},
        "where": "e.attributes['pkt.seq'] > 2",
    }
    sel = parse_selector(raw, "sel")
    assert sel.to_dict() == raw
    assert parse_selector(sel.to_dict(), "sel").to_dict() == raw
    assert "event_type=['packet.*']" in sel.describe()
