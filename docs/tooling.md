# Contract tooling

Tools for writing and maintaining contracts. None of them need a running system.

## Contract tests

A contract never seen to fail is untested. Contract tests give each contract
traces it must pass and traces it must fail:

```yaml
schema_version: 1
adapter: sensor_pipeline          # use this adapter's contracts
contracts: [extra_contracts.yaml] # and/or contract files
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
    trace: fixtures/silent_drop_40.jsonl   # normalized_events.jsonl from a run bundle
```

* **Inline events:** one of `t_ns`/`t_us`/`t_ms` (converted exactly), `type`,
  and optional `subsystem`, `source`, `severity`, `ids`, `attrs`.
* **Recorded traces** come from a bundle, so a real failure can become a
  permanent test.
* Cases run the whole contract (precondition and evidence included).

```sh
crosslayer test-contracts path/to/contract_tests.yaml --require-coverage
```

Add `--json` for one JSON document (`passed`, `failed_cases`, per-file `cases`
and `coverage`) instead of text. Exit codes are unchanged.

A contract is **covered** when it has a passing `pass` case and a passing
`fail` case. The reference adapter has 25 cases covering all 8 contracts, and
a test confirms they catch deliberately weakened contracts.

## Linting

```sh
crosslayer lint experiments/reference/*.yaml --strict      # experiments
crosslayer lint contracts.yaml --adapter sensor_pipeline   # contract file + adapter
```

`--json` prints `passed` and per-target `findings` (`code`, `level`,
`contract_id`, `message`) instead of text.

| Code | Level | Finding |
|---|---|---|
| CL001 | error | endpoint is not a declared subsystem |
| CL002 | warning | `critical` with `on_fail: warn` can never fail the run |
| CL003 | warning | state unreachable from `initial` |
| CL004 | info | no `initial` states, so the first state is unchecked |
| CL005 | info | too few samples: below 100, nearest-rank p99 *is* the max |
| CL006 | warning | response rule with no deadline and no end-of-run evidence |
| CL007 | warning | deadline not shorter than the run timeout |
| CL008 | error | a `final` state is unreachable from `initial` |
| CL010 | error | selector matches no event type the adapter emits |
| CL011 | error | a needed attribute or id never appears on matched events |
| CL012 | warning | that key is missing on *some* matched event types |

Exit 1 on any error, or any warning with `--strict`.

**Event catalogs.** CL010–CL012 use the adapter's optional `event_catalog()`:
each event type with its subsystems, attribute keys, and ids.
`check_events_against_catalog` in the adapter's tests keeps the catalog
honest against real output.

## Editor support (JSON Schema)

```sh
crosslayer schema contracts            # print one schema
crosslayer schema --write schemas/     # regenerate the checked-in copies
```

With the YAML language server (for example Red Hat YAML in VS Code), add this
first line for validation and autocomplete:

```yaml
# yaml-language-server: $schema=https://raw.githubusercontent.com/Pranav-s79/Cross-Layer-Systems-Test-Platform/main/schemas/contracts.schema.json
```

The Python loaders stay authoritative and check more than a schema can.
Tests keep the schemas **no stricter** than the loaders, so the editor never
flags a file CrossLayer accepts.

## Experiment suites

```yaml
schema_version: 1
name: reference
experiments:
  - id: baseline
    path: baseline.yaml
  - id: silent_drop
    path: fault_silent_drop.yaml
    expect: fail                   # default: pass
    depends_on: [baseline]
```

```sh
crosslayer suite experiments/reference/suite.yaml --out runs
```

* An experiment **meets** its expectation when its verdict equals `expect`.
* If a dependency isn't met, the experiment is **blocked** (transitively). A
  fault result means nothing if the baseline already fails.
* Order is a topological sort; ties go to file order. Cycles are rejected
  with the cycle named (`a -> b -> c -> a`).
* Every experiment is validated before any runs.
* Output: `runs/suite-<name>-<time>/` with `summary.json`, `report.md`,
  `junit.xml`, and each run's bundle. Exit 0 only if every experiment met its
  expectation.

## Dry runs

`crosslayer run EXPERIMENT --dry-run` validates the experiment (adapter,
parameters, faults, contracts) and prints the adapter, mode, faults, and
contract ids without executing anything or writing a bundle. It exits 0, or 4
on a configuration error.

## Contract versioning

Bump `version` when **what the contract checks** changes: `rule`, `source`,
`destination`, `precondition`, `evidence`, `severity`, or `on_fail`. Wording
and `owner` can change freely. Defaults are normalized (omitted `severity` =
`major`).

```sh
git show main:path/contracts.yaml > /tmp/old.yaml
crosslayer diff-contracts /tmp/old.yaml path/contracts.yaml             # exit 1 on problems
crosslayer diff-contracts /tmp/old.yaml path/contracts.yaml --markdown  # changelog per owner
```

Problems: a semantic change without a bump, or a version going backwards.

`crosslayer compare` marks contracts that changed between two bundles
(`[contract changed: v1 -> v2]`), so a tightened contract isn't mistaken for a
regression. The event fingerprint shows whether the system itself changed.

## Scaffolding an adapter

```sh
crosslayer new adapter rv32i_core --dir integrations/
cd integrations/rv32i_core && pip install -e . && pytest
```

The generated project passes its tests **before any edits**:

| File | Purpose |
|---|---|
| `rv32i_core/adapter.py` | Log-only adapter with an event catalog and TODOs |
| `rv32i_core/contracts.yaml` | Two example contracts |
| `rv32i_core/contract_tests.yaml` | A pass and a fail case for each |
| `examples/sample.log`, `experiments/analyze_sample.yaml` | Sample log and experiment |
| `tests/test_rv32i_core.py` | Conformance, coverage, lint, sample run, catalog check |
| `pyproject.toml` | Registers the adapter entry point |

Its README walks through adapting it step by step. CrossLayer's CI generates a
project and runs its tests, so the scaffold can't rot.
