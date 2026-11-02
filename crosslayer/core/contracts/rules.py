"""Built-in rule kinds.

| kind          | checks                                                        |
|---------------|---------------------------------------------------------------|
| count         | number of matching events is within [min, max]                |
| response      | every trigger is followed by a response (optionally in time)  |
| transitions   | per-entity state changes follow an allowed state machine      |
| threshold     | an aggregate (max, mean, p99...) of an attribute is in bounds |
| invariant     | an expression holds for every matching event                  |
| successive    | an expression holds between consecutive matching events       |

Every rule except ``count`` has a minimum-observations knob (``min_triggers``,
``min_events``, ``min_samples``, ``min_matches``, ``min_pairs``) defaulting to
1. With zero observations a rule is INCONCLUSIVE, never a vacuous PASS.
"""

from __future__ import annotations

import math
import operator
from bisect import bisect_left
from collections import defaultdict
from typing import Any, Callable, Mapping, Sequence

from crosslayer.core.contracts.expressions import Expression
from crosslayer.core.contracts.model import FindingCollector, Rule, RuleOutcome
from crosslayer.core.contracts.selectors import Selector, parse_selector
from crosslayer.core.errors import ConfigError, MissingEvidence
from crosslayer.core.telemetry.events import NormalizedEvent
from crosslayer.core.units import format_ns, ms_to_ns
from crosslayer.core.verdict import Verdict

RULE_TYPES: dict[str, type[Rule]] = {}


def register_rule(cls: type[Rule]) -> type[Rule]:
    if cls.kind in RULE_TYPES:
        raise ValueError(f"rule kind {cls.kind!r} already registered")
    RULE_TYPES[cls.kind] = cls
    return cls


def parse_rule(raw: Any, location: str) -> Rule:
    if not isinstance(raw, Mapping):
        raise ConfigError("rule must be a mapping", location=location)
    kind = raw.get("kind")
    if kind not in RULE_TYPES:
        raise ConfigError(
            f"unknown rule kind {kind!r} (available: {sorted(RULE_TYPES)})",
            location=f"{location}.kind",
        )
    body = {k: v for k, v in raw.items() if k != "kind"}
    return RULE_TYPES[kind].parse(body, location)


# -- parsing helpers ------------------------------------------------------------


def _check_keys(raw: Mapping[str, Any], required: set[str], optional: set[str], location: str) -> None:
    missing = required - raw.keys()
    if missing:
        raise ConfigError(f"missing required keys {sorted(missing)}", location=location)
    unknown = raw.keys() - required - optional
    if unknown:
        raise ConfigError(
            f"unknown keys {sorted(unknown)} (allowed: {sorted(required | optional)})",
            location=location,
        )


def _int(raw: Mapping[str, Any], key: str, location: str, default: int | None, minimum: int = 0) -> int | None:
    if key not in raw:
        return default
    value = raw[key]
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ConfigError(f"must be an integer >= {minimum}", location=f"{location}.{key}")
    return value


def _str(raw: Mapping[str, Any], key: str, location: str, default: str | None = None) -> str | None:
    if key not in raw:
        return default
    value = raw[key]
    if not isinstance(value, str) or not value:
        raise ConfigError("must be a non-empty string", location=f"{location}.{key}")
    return value


def _group_of(event: NormalizedEvent, group_by: str | None) -> str | None:
    if group_by is None:
        return "*"
    return event.correlation_ids.get(group_by)


def _select(selector: Selector, events: Sequence[NormalizedEvent]) -> list[NormalizedEvent]:
    return [e for e in events if selector.matches(e)]


def _insufficient(observed: int, required: int, what: str, stats: dict[str, Any]) -> RuleOutcome:
    return RuleOutcome(
        verdict=Verdict.INCONCLUSIVE,
        summary=f"only {observed} {what} observed (need >= {required}); nothing to verify",
        stats=stats,
    )


# -- count -----------------------------------------------------------------------


@register_rule
class CountRule(Rule):
    kind = "count"

    def __init__(self, select: Selector, minimum: int | None, maximum: int | None) -> None:
        self.select = select
        self.minimum = minimum
        self.maximum = maximum

    @classmethod
    def parse(cls, raw: Mapping[str, Any], location: str) -> "CountRule":
        _check_keys(raw, {"select"}, {"min", "max"}, location)
        minimum = _int(raw, "min", location, None)
        maximum = _int(raw, "max", location, None)
        if minimum is None and maximum is None:
            raise ConfigError("count rule needs 'min', 'max', or both", location=location)
        if minimum is not None and maximum is not None and minimum > maximum:
            raise ConfigError("'min' must be <= 'max'", location=location)
        return cls(parse_selector(raw["select"], f"{location}.select"), minimum, maximum)

    def evaluate(self, events: Sequence[NormalizedEvent]) -> RuleOutcome:
        matched = _select(self.select, events)
        n = len(matched)
        f = FindingCollector()
        bounds = f"[{self.minimum if self.minimum is not None else 0}, {self.maximum if self.maximum is not None else 'inf'}]"
        if self.minimum is not None and n < self.minimum:
            end = events[-1].timestamp_ns if events else None
            f.violation(
                f"expected at least {self.minimum} events matching ({self.select.describe()}), observed {n}",
                matched[-5:],
                at_ns=end,
                observed=n,
            )
        if self.maximum is not None and n > self.maximum:
            excess = matched[self.maximum :]
            f.violation(
                f"expected at most {self.maximum} events matching ({self.select.describe()}), observed {n}",
                excess[:5],
                at_ns=excess[0].timestamp_ns,
                observed=n,
            )
        return RuleOutcome(
            verdict=f.verdict(),
            summary=f"{n} matching events, allowed range {bounds}",
            findings=f.findings,
            stats={"observed": n, "violations": f.violations},
        )


# -- response --------------------------------------------------------------------


@register_rule
class ResponseRule(Rule):
    """Every trigger must be followed by a matching response.

    With ``correlate_by``, trigger and response must share that correlation id
    (strong evidence). Without it, any response after the trigger counts and
    the result is flagged as time-window-only correlation (weaker evidence).

    With ``within_ms``, a trigger whose deadline falls after the end of the
    observed trace is *unresolved*, not failed: the system may still have
    answered after the logs stopped.
    """

    kind = "response"

    def __init__(
        self,
        trigger: Selector,
        responses: tuple[Selector, ...],
        correlate_by: str | None,
        within_ns: int | None,
        min_triggers: int,
    ) -> None:
        self.trigger = trigger
        self.responses = responses
        self.correlate_by = correlate_by
        self.within_ns = within_ns
        self.min_triggers = min_triggers

    @classmethod
    def parse(cls, raw: Mapping[str, Any], location: str) -> "ResponseRule":
        _check_keys(raw, {"trigger", "response"}, {"correlate_by", "within_ms", "min_triggers"}, location)
        response_raw = raw["response"]
        if isinstance(response_raw, Mapping):
            response_raw = [response_raw]
        if not isinstance(response_raw, list) or not response_raw:
            raise ConfigError("must be a selector or non-empty list of selectors", location=f"{location}.response")
        within_ns = None
        if "within_ms" in raw:
            value = raw["within_ms"]
            if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
                raise ConfigError("must be a positive number", location=f"{location}.within_ms")
            within_ns = ms_to_ns(value)
        return cls(
            trigger=parse_selector(raw["trigger"], f"{location}.trigger"),
            responses=tuple(
                parse_selector(r, f"{location}.response[{i}]") for i, r in enumerate(response_raw)
            ),
            correlate_by=_str(raw, "correlate_by", location),
            within_ns=within_ns,
            min_triggers=_int(raw, "min_triggers", location, 1) or 0,
        )

    def _is_response(self, event: NormalizedEvent) -> bool:
        return any(s.matches(event) for s in self.responses)

    def evaluate(self, events: Sequence[NormalizedEvent]) -> RuleOutcome:
        observation_end = max((e.timestamp_ns for e in events), default=0)
        triggers = _select(self.trigger, events)

        # Index responses by correlation key so each lookup is a binary search.
        index: dict[str, list[NormalizedEvent]] = defaultdict(list)
        for e in events:
            if self._is_response(e):
                key = _group_of(e, self.correlate_by)
                if key is not None:
                    index[key].append(e)
        times = {k: [e.timestamp_ns for e in v] for k, v in index.items()}

        f = FindingCollector()
        latencies: list[int] = []
        deadline = f" within {format_ns(self.within_ns)}" if self.within_ns is not None else ""

        for t in triggers:
            key = _group_of(t, self.correlate_by)
            if key is None:
                f.unresolved(f"trigger has no '{self.correlate_by}' correlation id", [t])
                continue
            candidates = index.get(key, [])
            pos = bisect_left(times.get(key, []), t.timestamp_ns)
            response = next(
                (c for c in candidates[pos:] if c.event_id != t.event_id), None
            )
            label = f"{self.correlate_by}={key}" if self.correlate_by else t.event_id

            if response is not None:
                latency = response.timestamp_ns - t.timestamp_ns
                if self.within_ns is None or latency <= self.within_ns:
                    latencies.append(latency)
                    continue
                f.violation(
                    f"{label}: response '{response.event_type}' after {format_ns(latency)}, "
                    f"limit {format_ns(self.within_ns)}",
                    [t, response],
                    at_ns=t.timestamp_ns + self.within_ns,
                    latency_ns=latency,
                )
                continue

            if self.within_ns is not None and t.timestamp_ns + self.within_ns > observation_end:
                f.unresolved(
                    f"{label}: no response yet, but the trace ends before the deadline",
                    [t],
                )
                continue
            f.violation(
                f"{label}: no response{deadline} to '{t.event_type}'",
                [t],
                at_ns=t.timestamp_ns + self.within_ns if self.within_ns is not None else observation_end,
            )

        stats: dict[str, Any] = {
            "triggers": len(triggers),
            "satisfied": len(latencies),
            "violations": f.violations,
            "unresolved": f.unresolved_count,
            "correlation": f"id:{self.correlate_by}" if self.correlate_by else "time_window_only",
        }
        if latencies:
            stats["latency_ns_max"] = max(latencies)
            stats["latency_ns_mean"] = sum(latencies) // len(latencies)

        if len(triggers) < self.min_triggers:
            return _insufficient(len(triggers), self.min_triggers, "triggers", stats)
        return RuleOutcome(
            verdict=f.verdict(),
            summary=(
                f"{len(latencies)}/{len(triggers)} triggers answered{deadline}; "
                f"{f.violations} violations, {f.unresolved_count} unresolved"
            ),
            findings=f.findings,
            stats=stats,
        )


# -- transitions -----------------------------------------------------------------


@register_rule
class TransitionsRule(Rule):
    """Per-entity state machine check.

    ``state`` is either ``event_type`` (the event type is the state) or
    ``{attribute: ns.key}``. ``group_by`` names the correlation id that
    identifies the entity (a UE, a packet, a transaction).
    """

    kind = "transitions"

    def __init__(
        self,
        select: Selector,
        state_attribute: str | None,
        group_by: str | None,
        allowed: dict[str, frozenset[str]],
        initial: frozenset[str] | None,
        allow_repeat: bool,
        min_events: int,
    ) -> None:
        self.select = select
        self.state_attribute = state_attribute
        self.group_by = group_by
        self.allowed = allowed
        self.initial = initial
        self.allow_repeat = allow_repeat
        self.min_events = min_events
        self.known_states = set(allowed) | {s for targets in allowed.values() for s in targets}

    @classmethod
    def parse(cls, raw: Mapping[str, Any], location: str) -> "TransitionsRule":
        _check_keys(
            raw,
            {"select", "state", "allowed"},
            {"group_by", "initial", "allow_repeat", "min_events"},
            location,
        )
        state = raw["state"]
        if state == "event_type":
            state_attribute = None
        elif isinstance(state, Mapping) and set(state) == {"attribute"} and isinstance(state["attribute"], str):
            state_attribute = state["attribute"]
        else:
            raise ConfigError("must be 'event_type' or {attribute: <name>}", location=f"{location}.state")

        allowed_raw = raw["allowed"]
        if not isinstance(allowed_raw, Mapping) or not allowed_raw:
            raise ConfigError("must be a mapping of state -> list of next states", location=f"{location}.allowed")
        allowed: dict[str, frozenset[str]] = {}
        for src, targets in allowed_raw.items():
            if targets is None:
                targets = []
            if not isinstance(targets, list) or not all(isinstance(t, str) for t in targets):
                raise ConfigError("must be a list of state names", location=f"{location}.allowed.{src}")
            allowed[str(src)] = frozenset(targets)

        initial = None
        if "initial" in raw:
            init = raw["initial"]
            if isinstance(init, str):
                init = [init]
            if not isinstance(init, list) or not init:
                raise ConfigError("must be a state or list of states", location=f"{location}.initial")
            initial = frozenset(str(s) for s in init)

        allow_repeat = raw.get("allow_repeat", False)
        if not isinstance(allow_repeat, bool):
            raise ConfigError("must be true or false", location=f"{location}.allow_repeat")

        return cls(
            select=parse_selector(raw["select"], f"{location}.select"),
            state_attribute=state_attribute,
            group_by=_str(raw, "group_by", location),
            allowed=allowed,
            initial=initial,
            allow_repeat=allow_repeat,
            min_events=_int(raw, "min_events", location, 1) or 0,
        )

    def _state(self, event: NormalizedEvent) -> str:
        if self.state_attribute is None:
            return event.event_type
        if self.state_attribute not in event.attributes:
            raise MissingEvidence(f"state attribute '{self.state_attribute}' missing")
        return str(event.attributes[self.state_attribute])

    def evaluate(self, events: Sequence[NormalizedEvent]) -> RuleOutcome:
        selected = _select(self.select, events)
        f = FindingCollector()
        last: dict[str, tuple[NormalizedEvent, str]] = {}
        transitions = 0

        for e in selected:
            group = _group_of(e, self.group_by)
            if group is None:
                f.unresolved(f"event has no '{self.group_by}' correlation id", [e])
                continue
            try:
                state = self._state(e)
            except MissingEvidence as exc:
                f.unresolved(str(exc), [e])
                continue
            label = f"{self.group_by}={group}" if self.group_by else "stream"

            if state not in self.known_states:
                f.violation(f"{label}: unknown state '{state}'", [e], state=state)
            elif group not in last:
                if self.initial is not None and state not in self.initial:
                    f.violation(
                        f"{label}: starts in '{state}', expected one of {sorted(self.initial)}",
                        [e],
                        state=state,
                    )
            else:
                prev_event, prev_state = last[group]
                transitions += 1
                repeat_ok = self.allow_repeat and state == prev_state
                if not repeat_ok and state not in self.allowed.get(prev_state, frozenset()):
                    f.violation(
                        f"{label}: illegal transition '{prev_state}' -> '{state}'",
                        [prev_event, e],
                        from_state=prev_state,
                        to_state=state,
                    )
            last[group] = (e, state)

        stats = {
            "events": len(selected),
            "entities": len(last),
            "transitions": transitions,
            "violations": f.violations,
            "unresolved": f.unresolved_count,
        }
        if len(selected) < self.min_events:
            return _insufficient(len(selected), self.min_events, "events", stats)
        return RuleOutcome(
            verdict=f.verdict(),
            summary=f"{transitions} transitions across {len(last)} entities; {f.violations} illegal",
            findings=f.findings,
            stats=stats,
        )


# -- threshold -------------------------------------------------------------------

_COMPARATORS: dict[str, Callable[[float, float], bool]] = {
    "<": operator.lt,
    "<=": operator.le,
    ">": operator.gt,
    ">=": operator.ge,
    "==": operator.eq,
    "!=": operator.ne,
}
_PERCENTILES = {"p50": 50, "p90": 90, "p95": 95, "p99": 99}
_AGGREGATES = {"max", "min", "mean", "sum", "count", *_PERCENTILES}


def nearest_rank_percentile(sorted_values: Sequence[float], pct: float) -> float:
    """Nearest-rank percentile: the smallest value with at least pct% of
    samples at or below it. Always returns an observed sample (no interpolation),
    so the evidence can point at a real event."""
    rank = max(1, math.ceil(pct / 100 * len(sorted_values)))
    return sorted_values[rank - 1]


@register_rule
class ThresholdRule(Rule):
    kind = "threshold"

    def __init__(
        self,
        select: Selector,
        attribute: str | None,
        aggregate: str,
        op: str,
        value: float,
        min_samples: int,
    ) -> None:
        self.select = select
        self.attribute = attribute
        self.aggregate = aggregate
        self.op = op
        self.value = value
        self.min_samples = min_samples

    @classmethod
    def parse(cls, raw: Mapping[str, Any], location: str) -> "ThresholdRule":
        _check_keys(raw, {"select", "aggregate", "op", "value"}, {"attribute", "min_samples"}, location)
        aggregate = raw["aggregate"]
        if aggregate not in _AGGREGATES:
            raise ConfigError(f"must be one of {sorted(_AGGREGATES)}", location=f"{location}.aggregate")
        op = raw["op"]
        if op not in _COMPARATORS:
            raise ConfigError(f"must be one of {sorted(_COMPARATORS)}", location=f"{location}.op")
        value = raw["value"]
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ConfigError("must be a number", location=f"{location}.value")
        attribute = _str(raw, "attribute", location)
        if attribute is None and aggregate != "count":
            raise ConfigError(f"'attribute' is required for aggregate '{aggregate}'", location=location)
        return cls(
            select=parse_selector(raw["select"], f"{location}.select"),
            attribute=attribute,
            aggregate=aggregate,
            op=op,
            value=value,
            min_samples=_int(raw, "min_samples", location, 1) or 0,
        )

    def _aggregate(self, samples: list[tuple[float, NormalizedEvent]]) -> tuple[float, NormalizedEvent | None]:
        values = sorted(samples, key=lambda s: s[0])
        if self.aggregate == "max":
            return values[-1]
        if self.aggregate == "min":
            return values[0]
        if self.aggregate == "mean":
            return sum(v for v, _ in values) / len(values), None
        if self.aggregate == "sum":
            return sum(v for v, _ in values), None
        pct = _PERCENTILES[self.aggregate]
        rank = max(1, math.ceil(pct / 100 * len(values)))
        return values[rank - 1]

    def evaluate(self, events: Sequence[NormalizedEvent]) -> RuleOutcome:
        selected = _select(self.select, events)
        expected = f"{self.aggregate} {self.op} {self.value:g}"

        if self.aggregate == "count":
            observed: float = len(selected)
            ok = _COMPARATORS[self.op](observed, self.value)
            f = FindingCollector()
            if not ok:
                f.violation(f"count {observed:g} violates {expected}", selected[:5], at_ns=selected[-1].timestamp_ns if selected else None)
            return RuleOutcome(
                verdict=f.verdict(),
                summary=f"count = {observed:g} (expected {expected})",
                findings=f.findings,
                stats={"observed": observed, "samples": len(selected)},
            )

        samples: list[tuple[float, NormalizedEvent]] = []
        missing = 0
        for e in selected:
            v = e.attributes.get(self.attribute)  # type: ignore[arg-type]
            if isinstance(v, bool) or not isinstance(v, (int, float)):
                missing += 1
                continue
            samples.append((v, e))

        stats: dict[str, Any] = {"samples": len(samples), "missing_samples": missing}
        if len(samples) < max(self.min_samples, 1):
            return _insufficient(len(samples), max(self.min_samples, 1), f"'{self.attribute}' samples", stats)

        observed, witness = self._aggregate(samples)
        stats["observed"] = observed
        ok = _COMPARATORS[self.op](observed, self.value)
        f = FindingCollector()

        if not ok:
            # Upper bounds are broken by the largest samples, lower bounds by the smallest.
            ranked = sorted(samples, key=lambda s: s[0], reverse=self.op in ("<", "<="))
            offenders = [witness] if witness is not None else []
            offenders += [e for _, e in ranked if e is not witness][: 5 - len(offenders)]
            # Missing samples cannot rescue a max-upper-bound or min-lower-bound
            # violation, but they could change a mean or percentile.
            definitive = missing == 0 or (self.aggregate, self.op) in {
                ("max", "<"), ("max", "<="), ("min", ">"), ("min", ">="),
            }
            message = f"{self.attribute} {self.aggregate} = {observed:g} violates {expected}"
            if definitive:
                f.violation(message, offenders, at_ns=offenders[0].timestamp_ns if offenders else None, observed=observed)
            else:
                f.unresolved(
                    f"{message}, but {missing} matching events lacked the attribute",
                    offenders,
                    observed=observed,
                )
        elif missing:
            f.unresolved(
                f"{self.attribute} {self.aggregate} = {observed:g} satisfies {expected} over "
                f"{len(samples)} samples, but {missing} matching events lacked the attribute",
                [],
            )

        return RuleOutcome(
            verdict=f.verdict(),
            summary=f"{self.attribute} {self.aggregate} = {observed:g} over {len(samples)} samples (expected {expected})",
            findings=f.findings,
            stats=stats,
        )


# -- invariant -------------------------------------------------------------------


def _referenced_attributes(expr: Expression, event: NormalizedEvent) -> dict[str, Any]:
    """Attribute values an expression mentions, for readable violation reports."""
    return {k: v for k, v in event.attributes.items() if k in expr.source}


@register_rule
class InvariantRule(Rule):
    kind = "invariant"

    def __init__(self, select: Selector, expect: Expression, min_matches: int) -> None:
        self.select = select
        self.expect = expect
        self.min_matches = min_matches

    @classmethod
    def parse(cls, raw: Mapping[str, Any], location: str) -> "InvariantRule":
        _check_keys(raw, {"select", "expect"}, {"min_matches"}, location)
        return cls(
            select=parse_selector(raw["select"], f"{location}.select"),
            expect=Expression(raw["expect"], names={"e"}, location=f"{location}.expect"),
            min_matches=_int(raw, "min_matches", location, 1) or 0,
        )

    def evaluate(self, events: Sequence[NormalizedEvent]) -> RuleOutcome:
        selected = _select(self.select, events)
        f = FindingCollector()
        held = 0
        for e in selected:
            try:
                ok = self.expect.evaluate(e=e)
            except MissingEvidence as exc:
                f.unresolved(str(exc), [e])
                continue
            if ok:
                held += 1
            else:
                f.violation(
                    f"invariant `{self.expect.source}` is false",
                    [e],
                    observed=_referenced_attributes(self.expect, e),
                )
        stats = {"matches": len(selected), "held": held, "violations": f.violations, "unresolved": f.unresolved_count}
        if len(selected) < self.min_matches:
            return _insufficient(len(selected), self.min_matches, "matching events", stats)
        return RuleOutcome(
            verdict=f.verdict(),
            summary=f"held for {held}/{len(selected)} events",
            findings=f.findings,
            stats=stats,
        )


# -- successive ------------------------------------------------------------------


@register_rule
class SuccessiveRule(Rule):
    """Relation between each matching event and the previous one in the same
    group, e.g. ``curr.attributes["rtl.pc"] == prev.attributes["rtl.pc"] + 4``.
    ``when`` filters which pairs the relation applies to."""

    kind = "successive"

    def __init__(
        self,
        select: Selector,
        expect: Expression,
        when: Expression | None,
        group_by: str | None,
        min_pairs: int,
    ) -> None:
        self.select = select
        self.expect = expect
        self.when = when
        self.group_by = group_by
        self.min_pairs = min_pairs

    @classmethod
    def parse(cls, raw: Mapping[str, Any], location: str) -> "SuccessiveRule":
        _check_keys(raw, {"select", "expect"}, {"when", "group_by", "min_pairs"}, location)
        names = {"prev", "curr"}
        return cls(
            select=parse_selector(raw["select"], f"{location}.select"),
            expect=Expression(raw["expect"], names=names, location=f"{location}.expect"),
            when=Expression(raw["when"], names=names, location=f"{location}.when") if "when" in raw else None,
            group_by=_str(raw, "group_by", location),
            min_pairs=_int(raw, "min_pairs", location, 1) or 0,
        )

    def evaluate(self, events: Sequence[NormalizedEvent]) -> RuleOutcome:
        selected = _select(self.select, events)
        f = FindingCollector()
        last: dict[str, NormalizedEvent] = {}
        checked = held = 0

        for curr in selected:
            group = _group_of(curr, self.group_by)
            if group is None:
                f.unresolved(f"event has no '{self.group_by}' correlation id", [curr])
                continue
            prev = last.get(group)
            last[group] = curr
            if prev is None:
                continue
            try:
                if self.when is not None and not self.when.evaluate(prev=prev, curr=curr):
                    continue
                checked += 1
                ok = self.expect.evaluate(prev=prev, curr=curr)
            except MissingEvidence as exc:
                f.unresolved(str(exc), [prev, curr])
                continue
            if ok:
                held += 1
            else:
                f.violation(
                    f"`{self.expect.source}` is false between consecutive events",
                    [prev, curr],
                    prev=_referenced_attributes(self.expect, prev),
                    curr=_referenced_attributes(self.expect, curr),
                )

        stats = {"pairs_checked": checked, "held": held, "violations": f.violations, "unresolved": f.unresolved_count}
        if checked < self.min_pairs and not f.violations:
            return _insufficient(checked, self.min_pairs, "event pairs", stats)
        return RuleOutcome(
            verdict=f.verdict(),
            summary=f"held for {held}/{checked} consecutive pairs",
            findings=f.findings,
            stats=stats,
        )
