# Contract language

Contracts are YAML (or JSON) documents with `schema_version: 1`. Validation
is strict: unknown keys are errors, so a misspelled `within_ms` cannot
silently disable a timing check. Validate files without running anything:

```sh
crosslayer check path/to/contracts.yaml
```

## Contract fields

| Field | Required | Meaning |
|---|---|---|
| `id` | yes | Lowercase dotted id, unique per run (`ran.registration_timeout`) |
| `title` | yes | One-line statement of what must hold |
| `owner` | yes | Team or subsystem accountable for this boundary |
| `source`, `destination` | yes | Boundary endpoints. Must be subsystems the adapter declares |
| `rule` | yes | What to check (below) |
| `severity` | no | `critical`, `major` (default), `minor` |
| `on_fail` | no | `fail_run` (default) or `warn` (reported, does not fail the run) |
| `precondition` | no | Selector. If nothing matches, the contract is SKIPPED |
| `evidence` | no | Selector(s) that must match before a verdict counts. Missing gives INCONCLUSIVE |
| `description`, `tags` | no | Documentation |

## Selectors

A selector is a conjunction of filters over normalized events:

```yaml
event_type: "packet.*"          # shell-style pattern, or a list of patterns
subsystem: proc                 # pattern or list
source_system: sensor_pipeline  # exact or list
severity: [warning, error]
attributes: {pipeline.drop_reason: buffer_full}   # exact equality
where: 'e.attributes["pipeline.latency_us"] > 1000'  # expression over `e`
```

At least one filter is required. If a `where` expression references an
attribute the event does not have, that event does not match.

## Rule kinds

### `count`
Number of matching events is within bounds.
```yaml
kind: count
select: {event_type: process.exit, attributes: {process.exit_code: 0}}
min: 1
max: 1
```

### `response`
Every trigger is followed by a response, optionally within a deadline.
```yaml
kind: response
trigger: {event_type: packet.received}
response: [{event_type: packet.processed}, {event_type: packet.dropped}]
correlate_by: packet     # correlation-id key both events must share
within_ms: 5             # optional deadline
min_triggers: 1          # default 1; 0 allows a vacuous pass
```
* With `correlate_by`, matching uses explicit ids (strong evidence). Without
  it, any later response counts, and the stats record
  `correlation: time_window_only`.
* A late response is a violation that becomes observable *at the deadline*.
* A trigger whose deadline falls after the end of the trace is **unresolved**,
  not violated: the response may have come after logging stopped.
* Without `within_ms`, the end of the trace is the deadline. Pair it with
  `evidence` (for example an end-of-run marker) so truncated logs are
  INCONCLUSIVE.

### `transitions`
Per-entity state machine.
```yaml
kind: transitions
select: {event_type: "packet.*"}
state: event_type                 # or {attribute: ran.rrc_state}
group_by: packet                  # correlation key identifying the entity
initial: [packet.received]        # optional allowed first states
allowed:
  packet.received: [packet.processed, packet.dropped]
  packet.processed: []
  packet.dropped: []
allow_repeat: false               # whether X -> X is allowed
```
A state not mentioned anywhere in `allowed` is reported as unknown.

### `threshold`
An aggregate of a numeric attribute compared to a limit.
```yaml
kind: threshold
select: {event_type: packet.processed}
attribute: pipeline.latency_us
aggregate: p99          # max min mean sum count p50 p90 p95 p99
op: "<="                # < <= > >= == !=
value: 3000
min_samples: 20
```
Percentiles use the **nearest-rank** method, so the result is always an
observed sample and the evidence points at a real event. If some matching
events lack the attribute, a pass becomes INCONCLUSIVE. A failure is
reported only when the missing samples could not change it (`max` against an
upper bound, `min` against a lower bound). Otherwise it is also INCONCLUSIVE.

### `invariant`
An expression must hold for every matching event.
```yaml
kind: invariant
select: {event_type: packet.processed}
expect: e.attributes["pipeline.crc_rx"] == e.attributes["pipeline.crc_calc"]
```

### `successive`
A relation between each matching event and the previous one, optionally per
group.
```yaml
kind: successive
select: {event_type: rtl.fetch}
when: prev.attributes["rtl.next_kind"] == "sequential"
expect: curr.attributes["rtl.pc"] == (prev.attributes["rtl.pc"] + 4) & 0xFFFFFFFF
group_by: hart          # optional
```

## Expressions

Expressions are parsed with Python's `ast` module and checked against a
whitelist **when the contract is loaded**. They are never passed to `eval`.

Allowed:
* names bound by the rule: `e` (selectors, invariant), `prev` and `curr` (successive)
* event fields: `event_type subsystem source_system severity timestamp_ns event_id raw_artifact_ref attributes correlation_ids`
* subscripts: `e.attributes["ns.key"]`, `e.correlation_ids["ue"]`
* literals, tuples and lists, `+ - * / // % & | ^ << >>`, comparisons, `in`, `and or not`, `x if c else y`
* functions `abs min max len int float round`

Rejected at load time: any other name, attribute, or function; method calls;
comprehensions; lambdas; `**`; keyword or star arguments.

At evaluation:
* a missing attribute or correlation id raises *missing evidence*. The event
  becomes an **unresolved** finding, not a violation.
* a type error or division by zero is an **ERROR** of the contract itself.
* sequence repetition and shifts larger than 256 bits are refused.

## Verdicts

| Verdict | Meaning | Run impact (`fail_run` contracts) |
|---|---|---|
| PASS | Evaluated against sufficient evidence and held | — |
| FAIL | At least one evidence-backed violation | Run FAIL |
| INCONCLUSIVE | Not enough evidence either way | Run INCONCLUSIVE unless something failed |
| SKIPPED | Precondition not observed | None |
| ERROR | Verification itself broke | Run ERROR |

If no `fail_run` contract reaches PASS (none exist, or all were skipped), the
run is INCONCLUSIVE: nothing was actually verified.
