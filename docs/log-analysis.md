# Log analysis

The `textlog` adapter turns existing line-oriented text logs into events.
Each log format is described by a YAML **log profile**, so a new format needs
no Python. It runs in analyze mode only: it reads logs, it does not run a
system, and it has no faults.

## Try it

```sh
crosslayer suite experiments/textlog/suite.yaml --out runs
```

The example is a **synthetic** two-source fixture: a client log in UTC and a
server log in local time, without a year, on a clock 250 ms ahead. Two of its
three contracts join the logs by request id.

| Experiment | Expected | Result |
|---|---|---|
| `baseline.yaml` | PASS | 24 events (12 per log), 0 parse diagnostics, 3/3 contracts PASS |
| `slow_reply.yaml` | FAIL | `demo.request_answered` FAIL: `request=1003: response 'response.sent' after 182 ms, limit 100 ms` |

The 5G example is also explicitly **synthetic**. Its formats and provenance
are recorded in [`experiments/5g-logs/README.md`](../experiments/5g-logs/README.md).
It reads Open5GS AMF/SMF and OCUDU gNB logs, joins NGAP UE id pairs, and checks
registration, PDU-session setup, and RRC state order. The suite includes one
dropped, delayed, and reordered message variant; each fails only its target
contract.

```sh
crosslayer suite experiments/5g-logs/suite.yaml --out runs
```

## Experiment

List every log in `inputs`, then give exactly one source per input:

```yaml
adapter: textlog
inputs: [logs/client.log, logs/server.log]
parameters:
  sources:
    - {input: logs/client.log, profile: synthetic_client}
    - {input: logs/server.log, profile: synthetic_server, utc_offset: "+01:00", year: 2026, offset_ms: 250}
  joins:
    - name: ngap_ue
      keys: [ran_ue_ngap_id, amf_ue_ngap_id]
      fallback_window_ms: 250
contracts: [contracts.yaml]
```

| Key | Required | Meaning |
|---|---|---|
| `input` | yes | The file, as written in `inputs` (relative to the experiment) |
| `profile` | yes | A built-in profile name, or a path to a `.yaml` profile relative to the experiment |
| `offset_ms` | no | How far this source's clock is ahead of the reference clock; subtracted from every timestamp. Default 0 |
| `utc_offset` | no | `+HH:MM` offset of the timestamps; overrides the profile's |
| `year` | no | Year for timestamps without one; overrides the profile's |

`utc_offset` and `year` describe one capture, not the format, so they can be
given here. Each input is copied to `raw_artifacts/` under its file name; two
inputs with the same file name are rejected.

### Composite correlation joins

`joins` is optional. A join derives one correlation id from exactly two ids
emitted by profiles. An event containing both ids gets an exact, high-confidence
join. An event missing one or both ids gets the join only when every compatible
anchor inside `fallback_window_ms` has the same pair. If two different pairs
are possible, the event stays unjoined.

Every joined event records the method, ids, and confidence under
`confidence_metadata.correlation_joins.<name>`. A time fallback also records
the configured window, time delta, and anchor event id. Normalization statistics
count exact, fallback, ambiguous, and unmatched events for each join. Built-in
profiles list possible derived ids in `derived_correlation`, so contract lint
can validate `correlate_by` and `group_by` uses.

## Profile

Built-in profiles live in `crosslayer/integrations/textlog/profiles/<name>.yaml`.

```yaml
schema_version: 1
name: my_server
source_system: my-server            # recorded on every event
subsystem: server                   # default for every rule
timestamp:
  group: ts                         # named group holding the timestamp
  format: "%b %d %H:%M:%S.%f"       # strptime format
  utc_offset: "+01:00"              # optional here; required here or in the source unless the format has %z
  year: 2026                        # optional here; required here or in the source if the format has no year
clock: {source: server host wall clock, sync: none, resolution_ms: 1}
derived_correlation: [ngap_ue]        # optional ids produced by parameters.joins
skip: ['^#']                        # lines dropped silently (counted)
unmatched: diagnostic               # or: ignore
rules:                              # first match wins
  - name: replied
    match: '^(?P<ts>\w{3} [ \d]\d \S+) api-server\[\d+\]: INFO replied request=(?P<req>\d+) status=(?P<status>\d{3})$'
    event_type: response.sent
    severity: info                  # optional, default info
    subsystem: server               # optional
    attributes:
      app.status: {group: status, type: int}   # str | int | hex | float
      app.raw: status                          # shorthand for type str
    correlation:
      request: req                  # or {group: req, type: int | hex | str}
```

* `match` is a Python regex searched in the line; anchor it with `^` and `$`.
* Every rule's `match` must define the timestamp group and every group it references.
* `hex` and `int` correlation ids are stored in decimal, so `0x1F` and `31` join.
* An optional group that does not take part in a match leaves its attribute out.
* Unknown keys, bad regexes, bad names and bad types are config errors that name the field.

## Events

| Field | Value |
|---|---|
| `event_id` | `<file name>:<line>` |
| `raw_artifact_ref` | `raw_artifacts/<file name>#L<line>` |
| `timestamp_ns` | Nanoseconds since the Unix epoch (UTC), after `utc_offset` and `offset_ms` |
| `source_system`, `subsystem`, `severity` | From the profile and rule |
| `attributes` | The rule's attributes plus `log.source` (the profile name) |
| `correlation_ids` | The rule's correlation ids |
| `confidence_metadata` | `timestamp_source: log_text`, `utc_offset`, `offset_ms`, `clock`, and `assumed_year` when the year came from configuration |

Events from all sources are ordered by time; ties keep source order, then line order.

## Lines that do not become events

Nothing crashes. `normalization.json` counts each case per source.

| Line | Result |
|---|---|
| Blank | Counted as `blank` |
| Matches a `skip` regex | Counted as `skipped` |
| No rule matches | Diagnostic, or counted as `unmatched` with `unmatched: ignore` |
| A value or timestamp does not convert | Diagnostic naming the rule and field |
| Not valid UTF-8 | Diagnostic |

## Validation

| When | What |
|---|---|
| `plan()`, before a bundle exists | Source keys and types, profiles, input files, duplicate file names, `utc_offset` and `year` |
| Start of `collect` | Every input has exactly one source and every source is an input; otherwise the run is ERROR |

The runner does not give plan-time hooks the experiment's `inputs`, so the
last check runs at `collect`.

## Limits

* One event per line; no multi-line records.
* Microsecond resolution (`%f`).
* Fixed UTC offsets: a capture that crosses a daylight-saving change must be split.
* Clock offsets are constant per source; no drift correction.
* `%b` and `%a` use the process's `LC_TIME` locale (English unless the process changes it).
* `crosslayer lint` knows only the built-in profiles' events; contracts over a profile loaded from a path can get CL010 or CL011.
* A profile loaded from a path may only use subsystems that the built-in profiles declare.

## Adding a format

1. Add `profiles/<name>.yaml` (or keep it next to your experiment).
2. Write an experiment with one source per input and run it; fix diagnostics until `parse_diagnostics` is 0.
3. Add contracts with a PASS and a FAIL case each, and check them with
   `crosslayer test-contracts ... --require-coverage`.
