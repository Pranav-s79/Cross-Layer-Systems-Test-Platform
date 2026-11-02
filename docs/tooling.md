# Contract tooling

Tools for writing, checking, and maintaining contracts. All of them run
without a system under test.

## Contract tests

A contract that has never been seen to fail is untested. Contract tests
pair each contract with small traces it must accept (known-good) and reject
(known-bad):

```yaml
schema_version: 1
adapter: sensor_pipeline          # use this adapter's default contracts
contracts: [extra_contracts.yaml] # and/or contract files (paths relative to this file)
cases:
  - name: a packet that silently disappears is a violation
    contract: pipeline.rx_accounted
    expect: fail                  # pass | fail | inconclusive | skipped | error
    expect_violations: 1          # optional
    events:
      - {t_us: 0, type: system.boot, subsystem: sys}
      - {t_us: 10, type: packet.received, subsystem: sensor_rx, ids: {packet: 1}}
      - {t_us: 9000, type: system.halt, subsystem: sys}
  - name: recorded silent-drop run
    contract: pipeline.rx_accounted
    expect: fail
    trace: fixtures/silent_drop_40.jsonl   # normalized_events.jsonl from any run bundle
```

* **Inline events** use a compact form: exactly one of `t_ns`, `t_us`, `t_ms`
  (converted exactly, never through floats), `type`, and optional `subsystem`,
  `source`, `severity`, `ids` (correlation ids), `attrs` (attributes). They are
  sorted by time before evaluation.
* **Recorded traces** are copied from a run bundle's `normalized_events.jsonl`,
  so a real failure can become a permanent regression case.
* Cases evaluate the whole contract, including its precondition and required
  evidence, so `skipped` and `inconclusive` can be tested too.

```sh
crosslayer test-contracts path/to/contract_tests.yaml --require-coverage
```

**Coverage:** a contract is covered when it has at least one passing `pass`
case *and* one passing `fail` case. `--require-coverage` makes missing
coverage fail the command.

The reference adapter ships 25 cases covering all 8 of its contracts
(`crosslayer/integrations/sensor_pipeline/contract_tests.yaml`). A test
weakens contracts on purpose (a looser deadline, a looser threshold, a wrong
sequence step) and checks that the contract tests catch each change.
