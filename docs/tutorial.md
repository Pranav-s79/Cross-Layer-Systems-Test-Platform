# Tutorial: your first contract

Add a contract to the reference sensor pipeline, test it, lint it, run it,
and read the report. Work in an empty directory; nothing here edits the repo.
Every command below was run with the output shown (Linux, Python 3.11+).

## 1. Write the contract

`my_contracts.yaml`. Each received packet must carry 1 to 64 payload bytes.

```yaml
schema_version: 1
contracts:
  - id: tutorial.payload_length
    title: Every received packet carries 1 to 64 payload bytes
    owner: firmware-rx
    source: sensor
    destination: sensor_rx
    severity: major
    rule:
      kind: invariant
      select: {event_type: packet.received}
      expect: e.attributes["pipeline.len"] >= 1 and e.attributes["pipeline.len"] <= 64
```

Fields and rule kinds: [Contract language](contract-language.md).

## 2. Write its contract tests

`my_tests.yaml`. One case must pass and one must fail, or the contract is untested.

```yaml
schema_version: 1
adapter: sensor_pipeline
contracts: [my_contracts.yaml]
cases:
  - name: an 8-byte packet is accepted
    contract: tutorial.payload_length
    expect: pass
    events:
      - {t_us: 10, type: packet.received, subsystem: sensor_rx, ids: {packet: 1}, attrs: {pipeline.len: 8}}
  - name: an empty packet is a violation
    contract: tutorial.payload_length
    expect: fail
    expect_violations: 1
    events:
      - {t_us: 10, type: packet.received, subsystem: sensor_rx, ids: {packet: 1}, attrs: {pipeline.len: 0}}
```

```sh
crosslayer test-contracts my_tests.yaml
```

```text
  ok   [tutorial.payload_length] an 8-byte packet is accepted
  ok   [tutorial.payload_length] an empty packet is a violation
all cases passed
```

The coverage lines for the nine built-in contracts are not an error. Add
`--require-coverage` only when the file should cover every contract.
See [Contract tooling](tooling.md).

## 3. Lint

```sh
crosslayer lint my_contracts.yaml --adapter sensor_pipeline
```

```text
my_contracts.yaml: 0 finding(s)
```

## 4. Run it

`my_exp.yaml` adds your file to the adapter's contracts:

```yaml
schema_version: 1
name: tutorial
adapter: sensor_pipeline
seed: 1234
contracts: [my_contracts.yaml]
parameters: {packets: 400, buffer_capacity: 8}
```

```sh
crosslayer run my_exp.yaml --out runs
```

```text
verdict   PASS
  PASS         tutorial.payload_length        held for 400/400 events
  PASS         pipeline.rx_accounted          400/400 triggers answered within 5 ms; 0 violations, 0 unresolved
  ...
```

## 5. Read a failure

Change `<= 64` to `<= 4` (every packet has 8 bytes) and run again. The exit
code is 1, and `report.md` in the bundle says:

```text
### `tutorial.payload_length`
First violation at 0.001163000 s.
- invariant `e.attributes["pipeline.len"] >= 1 and e.attributes["pipeline.len"] <= 4` is false
  - observed: pipeline.len=8
```

Each violation links to its raw log line (see [Run bundle](run-bundle.md)).
Next: [Writing an adapter](writing-an-adapter.md).
