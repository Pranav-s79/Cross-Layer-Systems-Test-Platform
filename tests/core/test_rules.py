"""Behavior of each built-in rule kind: pass, fail, and inconclusive paths."""

from __future__ import annotations

import pytest

from crosslayer.core.contracts.model import FindingKind
from crosslayer.core.contracts.rules import nearest_rank_percentile, parse_rule
from crosslayer.core.errors import ConfigError
from crosslayer.core.verdict import Verdict
from tests.conftest import ev

MS = 1_000_000


def rule(**raw):
    return parse_rule(raw, "test")


# -- count -----------------------------------------------------------------------


def test_count_within_bounds_passes():
    r = rule(kind="count", select={"event_type": "x.*"}, min=1, max=2)
    assert r.evaluate([ev("x.a", 1), ev("y.b", 2)]).verdict is Verdict.PASS


def test_count_too_few_and_too_many():
    r = rule(kind="count", select={"event_type": "x.a"}, min=2)
    out = r.evaluate([ev("x.a", 1)])
    assert out.verdict is Verdict.FAIL and "at least 2" in out.findings[0].message

    r = rule(kind="count", select={"event_type": "x.a"}, max=1)
    events = [ev("x.a", 1), ev("x.a", 7)]
    out = r.evaluate(events)
    assert out.verdict is Verdict.FAIL
    assert out.findings[0].timestamp_ns == 7  # the event that exceeded the bound


def test_count_requires_a_bound():
    with pytest.raises(ConfigError):
        rule(kind="count", select={"event_type": "x"})


# -- response --------------------------------------------------------------------


def _response(**extra):
    return rule(
        kind="response",
        trigger={"event_type": "req"},
        response=[{"event_type": "ack"}, {"event_type": "nack"}],
        correlate_by="ue",
        **extra,
    )


def test_response_correlated_within_deadline():
    events = [
        ev("req", 0, ids={"ue": 1}),
        ev("req", 1 * MS, ids={"ue": 2}),
        ev("nack", 2 * MS, ids={"ue": 2}),
        ev("ack", 3 * MS, ids={"ue": 1}),
        ev("tick", 20 * MS),
    ]
    out = _response(within_ms=5).evaluate(events)
    assert out.verdict is Verdict.PASS
    assert out.stats["satisfied"] == 2
    assert out.stats["latency_ns_max"] == 3 * MS
    assert out.stats["correlation"] == "id:ue"


def test_response_wrong_id_does_not_count():
    events = [ev("req", 0, ids={"ue": 1}), ev("ack", 1, ids={"ue": 9}), ev("tick", 20 * MS)]
    out = _response(within_ms=5).evaluate(events)
    assert out.verdict is Verdict.FAIL


def test_response_late_is_violation_observable_at_deadline():
    events = [ev("req", 0, ids={"ue": 1}), ev("ack", 7 * MS, ids={"ue": 1})]
    out = _response(within_ms=5).evaluate(events)
    assert out.verdict is Verdict.FAIL
    finding = out.findings[0]
    assert finding.timestamp_ns == 5 * MS  # the deadline, not the late ack
    assert len(finding.evidence) == 2


def test_response_pending_at_end_of_trace_is_unresolved():
    events = [ev("req", 0, ids={"ue": 1}), ev("tick", 2 * MS)]
    out = _response(within_ms=5).evaluate(events)
    assert out.verdict is Verdict.INCONCLUSIVE
    assert out.findings[0].kind is FindingKind.UNRESOLVED


def test_response_without_deadline_uses_trace_end():
    out = _response().evaluate([ev("req", 0, ids={"ue": 1}), ev("tick", 9)])
    assert out.verdict is Verdict.FAIL


def test_response_trigger_without_id_is_unresolved():
    out = _response(within_ms=5).evaluate([ev("req", 0), ev("tick", 20 * MS)])
    assert out.verdict is Verdict.INCONCLUSIVE


def test_response_response_before_trigger_does_not_count():
    events = [ev("ack", 0, ids={"ue": 1}), ev("req", 1, ids={"ue": 1}), ev("tick", 20 * MS)]
    assert _response(within_ms=5).evaluate(events).verdict is Verdict.FAIL


def test_response_no_triggers_is_inconclusive_not_vacuous_pass():
    out = _response(within_ms=5).evaluate([ev("tick", 0)])
    assert out.verdict is Verdict.INCONCLUSIVE
    assert _response(within_ms=5, min_triggers=0).evaluate([ev("tick", 0)]).verdict is Verdict.PASS


def test_response_time_window_only_mode():
    r = rule(kind="response", trigger={"event_type": "req"}, response={"event_type": "ack"}, within_ms=1)
    out = r.evaluate([ev("req", 0), ev("ack", 10), ev("tick", 5 * MS)])
    assert out.verdict is Verdict.PASS
    assert out.stats["correlation"] == "time_window_only"


def test_response_findings_are_capped_but_counted():
    events = [ev("req", i, ids={"ue": i}) for i in range(120)] + [ev("tick", 100 * MS)]
    out = _response(within_ms=1).evaluate(events)
    assert out.stats["violations"] == 120
    assert len(out.findings) == 50


# -- transitions -----------------------------------------------------------------

_LIFECYCLE = dict(
    kind="transitions",
    select={"event_type": "pkt.*"},
    state="event_type",
    group_by="pkt",
    initial=["pkt.rx"],
    allowed={"pkt.rx": ["pkt.done", "pkt.drop"], "pkt.done": [], "pkt.drop": []},
)


def test_transitions_legal_paths():
    events = [ev("pkt.rx", 0, ids={"pkt": 1}), ev("pkt.rx", 1, ids={"pkt": 2}),
              ev("pkt.done", 2, ids={"pkt": 1}), ev("pkt.drop", 3, ids={"pkt": 2})]
    out = rule(**_LIFECYCLE).evaluate(events)
    assert out.verdict is Verdict.PASS and out.stats["entities"] == 2


def test_transitions_illegal_repeat_and_bad_initial():
    events = [ev("pkt.rx", 0, ids={"pkt": 1}), ev("pkt.done", 1, ids={"pkt": 1}),
              ev("pkt.done", 2, ids={"pkt": 1}), ev("pkt.done", 3, ids={"pkt": 2})]
    out = rule(**_LIFECYCLE).evaluate(events)
    assert out.verdict is Verdict.FAIL
    messages = [f.message for f in out.findings]
    assert any("illegal transition 'pkt.done' -> 'pkt.done'" in m for m in messages)
    assert any("starts in 'pkt.done'" in m for m in messages)


def test_transitions_attribute_state_and_unknown_state():
    r = rule(kind="transitions", select={"event_type": "ue.state"}, state={"attribute": "ran.state"},
             group_by="ue", allowed={"IDLE": ["CONNECTED"], "CONNECTED": ["IDLE"]}, allow_repeat=True)
    good = [ev("ue.state", 0, ids={"ue": 1}, attrs={"ran.state": "IDLE"}),
            ev("ue.state", 1, ids={"ue": 1}, attrs={"ran.state": "IDLE"}),
            ev("ue.state", 2, ids={"ue": 1}, attrs={"ran.state": "CONNECTED"})]
    assert r.evaluate(good).verdict is Verdict.PASS
    bad = good + [ev("ue.state", 3, ids={"ue": 1}, attrs={"ran.state": "LIMBO"})]
    assert r.evaluate(bad).verdict is Verdict.FAIL
    missing = [ev("ue.state", 0, ids={"ue": 1})]
    assert r.evaluate(missing).verdict is Verdict.INCONCLUSIVE


# -- threshold -------------------------------------------------------------------


def _latencies(values, missing=0):
    events = [ev("pkt.done", i, attrs={"p.lat": v}) for i, v in enumerate(values)]
    events += [ev("pkt.done", 1000 + i) for i in range(missing)]
    return events


def test_nearest_rank_percentile():
    values = list(range(1, 101))
    assert nearest_rank_percentile(values, 99) == 99
    assert nearest_rank_percentile(values, 50) == 50
    assert nearest_rank_percentile([7], 99) == 7


def test_threshold_pass_and_fail_with_witness():
    r = rule(kind="threshold", select={"event_type": "pkt.done"}, attribute="p.lat",
             aggregate="max", op="<=", value=10)
    assert r.evaluate(_latencies([1, 5, 10])).verdict is Verdict.PASS
    out = r.evaluate(_latencies([1, 50, 10]))
    assert out.verdict is Verdict.FAIL
    assert out.findings[0].evidence[0].timestamp_ns == 1  # the event holding the max


def test_threshold_missing_samples():
    r = rule(kind="threshold", select={"event_type": "pkt.done"}, attribute="p.lat",
             aggregate="mean", op="<=", value=10)
    # passes over what was observed, but some events lacked the attribute
    assert r.evaluate(_latencies([1, 2], missing=1)).verdict is Verdict.INCONCLUSIVE
    # a mean violation could be changed by the missing samples -> not definitive
    assert r.evaluate(_latencies([100, 100], missing=1)).verdict is Verdict.INCONCLUSIVE
    # a max upper-bound violation cannot be rescued by missing samples
    r_max = rule(kind="threshold", select={"event_type": "pkt.done"}, attribute="p.lat",
                 aggregate="max", op="<=", value=10)
    assert r_max.evaluate(_latencies([100], missing=3)).verdict is Verdict.FAIL


def test_threshold_min_samples_and_count():
    r = rule(kind="threshold", select={"event_type": "pkt.done"}, attribute="p.lat",
             aggregate="p99", op="<=", value=10, min_samples=5)
    assert r.evaluate(_latencies([1, 2])).verdict is Verdict.INCONCLUSIVE
    c = rule(kind="threshold", select={"event_type": "pkt.done"}, aggregate="count", op=">=", value=3)
    assert c.evaluate(_latencies([1, 2])).verdict is Verdict.FAIL


def test_threshold_requires_attribute_unless_count():
    with pytest.raises(ConfigError):
        rule(kind="threshold", select={"event_type": "x"}, aggregate="max", op="<", value=1)


# -- invariant -------------------------------------------------------------------


def test_invariant_reports_observed_values():
    r = rule(kind="invariant", select={"event_type": "pkt.done"},
             expect='e.attributes["p.crc_rx"] == e.attributes["p.crc_calc"]')
    good = ev("pkt.done", 0, attrs={"p.crc_rx": 1, "p.crc_calc": 1, "p.other": 9})
    bad = ev("pkt.done", 1, attrs={"p.crc_rx": 1, "p.crc_calc": 2, "p.other": 9})
    out = r.evaluate([good, bad])
    assert out.verdict is Verdict.FAIL
    assert out.findings[0].details["observed"] == {"p.crc_rx": 1, "p.crc_calc": 2}


def test_invariant_missing_attribute_is_unresolved():
    r = rule(kind="invariant", select={"event_type": "pkt.done"}, expect='e.attributes["p.x"] > 0')
    assert r.evaluate([ev("pkt.done", 0)]).verdict is Verdict.INCONCLUSIVE


# -- successive ------------------------------------------------------------------

_PC = dict(
    kind="successive",
    select={"event_type": "rtl.fetch"},
    when='prev.attributes["rtl.kind"] == "seq"',
    expect='curr.attributes["rtl.pc"] == prev.attributes["rtl.pc"] + 4',
)


def _fetch(t, pc, kind="seq"):
    return ev("rtl.fetch", t, attrs={"rtl.pc": pc, "rtl.kind": kind})


def test_successive_pc_plus_four():
    out = rule(**_PC).evaluate([_fetch(0, 0), _fetch(1, 4), _fetch(2, 8, kind="branch"), _fetch(3, 64)])
    assert out.verdict is Verdict.PASS
    assert out.stats["pairs_checked"] == 2  # the branch pair is excluded by `when`


def test_successive_violation_has_both_events():
    out = rule(**_PC).evaluate([_fetch(0, 0), _fetch(1, 4), _fetch(2, 12)])
    assert out.verdict is Verdict.FAIL
    finding = out.findings[0]
    assert [r.timestamp_ns for r in finding.evidence] == [1, 2]
    assert finding.details["prev"]["rtl.pc"] == 4 and finding.details["curr"]["rtl.pc"] == 12


def test_successive_grouping():
    r = rule(kind="successive", select={"event_type": "seq.msg"}, group_by="flow",
             expect='curr.attributes["s.n"] == prev.attributes["s.n"] + 1')
    events = [ev("seq.msg", 0, ids={"flow": "a"}, attrs={"s.n": 1}),
              ev("seq.msg", 1, ids={"flow": "b"}, attrs={"s.n": 10}),
              ev("seq.msg", 2, ids={"flow": "a"}, attrs={"s.n": 2}),
              ev("seq.msg", 3, ids={"flow": "b"}, attrs={"s.n": 11})]
    assert r.evaluate(events).verdict is Verdict.PASS


# -- parsing ---------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw",
    [
        {"kind": "nope"},
        {"kind": "count", "select": {"event_type": "x"}, "min": 1, "withn_ms": 3},
        {"kind": "response", "trigger": {"event_type": "x"}, "response": [], "within_ms": 1},
        {"kind": "response", "trigger": {"event_type": "x"}, "response": {"event_type": "y"}, "within_ms": -1},
        {"kind": "transitions", "select": {"event_type": "x"}, "state": "weird", "allowed": {"a": []}},
        {"kind": "threshold", "select": {"event_type": "x"}, "attribute": "a.b", "aggregate": "p42", "op": "<", "value": 1},
        {"kind": "invariant", "select": {}, "expect": "True"},
        {"kind": "invariant", "select": {"event_type": "x"}, "expect": "e.attributes.items()"},
    ],
)
def test_rule_parse_errors(raw):
    with pytest.raises(ConfigError):
        parse_rule(raw, "test")


# -- regressions found in the Phase 1 review ---------------------------------------


def test_response_time_window_mode_scales_linearly():
    """Slicing the candidate list per trigger made this mode quadratic
    (80k events took 6.6 s). It must now be near-linear."""
    import time

    events = []
    for i in range(50_000):
        events += [ev("req", i * 10_000), ev("ack", i * 10_000 + 3_000)]
    r = rule(kind="response", trigger={"event_type": "req"}, response={"event_type": "ack"}, within_ms=5)
    start = time.perf_counter()
    out = r.evaluate(events)
    assert out.verdict is Verdict.PASS and out.stats["satisfied"] == 50_000
    assert time.perf_counter() - start < 5.0  # the quadratic version took far longer


def test_response_trigger_matching_its_own_response_selector_is_skipped():
    r = rule(kind="response", trigger={"event_type": "msg"}, response={"event_type": "msg"}, correlate_by="k")
    out = r.evaluate([ev("msg", 0, ids={"k": 1}), ev("msg", 5, ids={"k": 1})])
    # the first message is answered by the second; the second has nothing after it
    assert out.stats["satisfied"] == 1 and out.verdict is Verdict.FAIL


def test_transitions_yaml_booleans_get_a_clear_error():
    with pytest.raises(ConfigError, match="quote it"):
        rule(kind="transitions", select={"event_type": "p"}, state="event_type", allowed={True: [False]})


def test_transitions_initial_states_must_exist():
    with pytest.raises(ConfigError, match="do not appear"):
        rule(kind="transitions", select={"event_type": "p"}, state="event_type",
             allowed={"IDLE": ["BUSY"], "BUSY": []}, initial=["IDEL"])


def test_percentile_rank_matches_exact_rational_definition():
    from fractions import Fraction
    import math

    for pct in (50, 90, 95, 99):
        for n in range(1, 2_001):
            values = list(range(1, n + 1))
            exact_rank = max(1, math.ceil(Fraction(pct * n, 100)))
            assert nearest_rank_percentile(values, pct) == exact_rank
