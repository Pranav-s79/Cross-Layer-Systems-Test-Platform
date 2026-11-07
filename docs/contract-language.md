# Contract language

Contracts are YAML (or JSON) files with `schema_version: 1`. Unknown keys are
errors, so a typo like `withn_ms` can't silently disable a check. Validate
without running: `crosslayer check path/to/contracts.yaml`.

## Fields

| Field | Required | Meaning |
|---|---|---|
| `id` | yes | Lowercase dotted id, unique per run (`ran.registration_timeout`) |
| `title` | yes | What must hold, in one line |
| `owner` | yes | Team accountable for this boundary |
| `source`, `destination` | yes | Boundary endpoints; must be declared subsystems |
| `rule` | yes | What to check (below) |
| `severity` | no | `critical`, `major` (default), `minor` |
| `on_fail` | no | `fail_run` (default) or `warn` (reported only) |
| `precondition` | no | Selector; if nothing matches, SKIPPED |
| `evidence` | no | Selector(s) that must match, or the result is INCONCLUSIVE |
| `version` | no | Integer, default 1; bump when what the contract checks changes ([tooling](tooling.md#contract-versioning)) |
| `description`, `tags` | no | Documentation |

## Selectors

All filters must match:

```yaml
event_type: "packet.*"          # shell pattern, or a list
subsystem: proc                 # pattern or list
source_system: sensor_pipeline  # exact or list
severity: [warning, error]
attributes: {pipeline.drop_reason: buffer_full}      # exact equality
where: 'e.attributes["pipeline.latency_us"] > 1000'  # expression over `e`
```

At least one filter is required. If `where` uses an attribute the event
lacks, the event doesn't match.

## Rule kinds

### `count`
Number of matching events within bounds.
```yaml
kind: count
select: {event_type: process.exit, attributes: {process.exit_code: 0}}
min: 1
max: 1
```

### `response`
Every trigger gets a response, optionally within a deadline.
```yaml
kind: response
trigger: {event_type: packet.received}
response: [{event_type: packet.processed}, {event_type: packet.dropped}]
correlate_by: packet     # id both events must share
within_ms: 5             # optional
min_triggers: 1          # default 1; 0 allows a vacuous pass
exclusive: false         # true: each response satisfies at most one trigger
```
* With `exclusive: true`, triggers claim responses first in, first out per
  correlation key; two triggers and one response leave the second violated.
* Without `correlate_by`, any later response counts (recorded as
  `correlation: time_window_only`).
* A late response is a violation at the deadline.
* A trigger whose deadline falls after the trace ends is **unresolved**, not
  violated.
* Without `within_ms`, the end of the trace is the deadline; add `evidence`
  (an end-of-run marker) so a truncated log is INCONCLUSIVE.

### `transitions`
Per-entity state machine.
```yaml
kind: transitions
select: {event_type: "packet.*"}
state: event_type                 # or {attribute: ran.rrc_state}
group_by: packet                  # id of the entity
initial: [packet.received]        # optional allowed first states
final: [packet.processed, packet.dropped]   # optional allowed last states
allowed:
  packet.received: [packet.processed, packet.dropped]
  packet.processed: []
  packet.dropped: []
allow_repeat: false               # allow X -> X
```
States missing from `allowed` are reported as unknown. With `final`, each
entity's last state in the trace must be one of the listed states; otherwise
a violation is reported at that entity's last event. Pair it with `evidence`
(an end-of-run marker) so a truncated log is INCONCLUSIVE rather than a
spurious violation. Lint reports an error (CL008) when a `final` state cannot
be reached from `initial`.

### `threshold`
An aggregate of a numeric attribute against a limit.
```yaml
kind: threshold
select: {event_type: packet.processed}
attribute: pipeline.latency_us
aggregate: p99          # max min mean sum count p50 p90 p95 p99
op: "<="                # < <= > >= == !=
value: 3000
min_samples: 20
```
Percentiles use **nearest rank**, so the result is a real sample. If some
events lack the attribute, a pass becomes INCONCLUSIVE; a fail stands only
when missing samples couldn't change it (`max` vs an upper bound, `min` vs a
lower bound).

### `invariant`
An expression holds for every matching event.
```yaml
kind: invariant
select: {event_type: packet.processed}
expect: e.attributes["pipeline.crc_rx"] == e.attributes["pipeline.crc_calc"]
```

### `successive`
A relation between each event and the previous one, optionally per group.
```yaml
kind: successive
select: {event_type: rtl.fetch}
when: prev.attributes["rtl.next_kind"] == "sequential"
expect: curr.attributes["rtl.pc"] == (prev.attributes["rtl.pc"] + 4) & 0xFFFFFFFF
group_by: hart          # optional
```

## Expressions

Parsed with Python's `ast` and checked against a whitelist **at load time**.
Never passed to `eval`.

Allowed:
* names: `e` (selectors, invariant), `prev` and `curr` (successive)
* fields: `event_type subsystem source_system severity timestamp_ns event_id raw_artifact_ref attributes correlation_ids`
* subscripts: `e.attributes["ns.key"]`, `e.correlation_ids["ue"]`
* literals, tuples, lists, `+ - * / // % & | ^ << >>`, comparisons, `in`, `and or not`, `x if c else y`
* functions: `abs min max len int float round`

Rejected at load: other names, attributes, or functions; method calls;
comprehensions; lambdas; `**`; keyword or star arguments.

At evaluation, a missing attribute or id makes the event **unresolved** (not
a violation). A type error or division by zero is an **ERROR** in the
contract. Sequence repetition (`"a" * n`) and shifts over 256 bits are refused.

## Verdicts

| Verdict | Meaning | Effect on the run (`fail_run` contracts) |
|---|---|---|
| PASS | Held, with enough evidence | — |
| FAIL | At least one evidence-backed violation | FAIL |
| INCONCLUSIVE | Not enough evidence | INCONCLUSIVE unless something failed |
| SKIPPED | Precondition not observed | none |
| ERROR | The check itself broke | ERROR |

If no `fail_run` contract passes, the run is INCONCLUSIVE: nothing was verified.
