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

## Linting

```sh
crosslayer lint experiments/reference/*.yaml --strict           # experiments: full checks
crosslayer lint contracts.yaml --adapter sensor_pipeline         # contract file + adapter knowledge
```

Nothing runs. Experiments are linted against their adapter automatically.
Contract files are linted on their own, or against an adapter with `--adapter`.

| Code | Level | Finding |
|---|---|---|
| CL001 | error | contract endpoint is not a subsystem the adapter declares |
| CL002 | warning | `severity: critical` with `on_fail: warn`, so a failure can never fail the run |
| CL003 | warning | state unreachable from the `initial` states |
| CL004 | info | state machine has no `initial` states, so the first state is never checked |
| CL005 | info | percentile `min_samples` too low: below 100 samples, nearest-rank p99 *is* the maximum |
| CL006 | warning | response rule with no deadline and no required end-of-run evidence |
| CL007 | warning | response deadline not shorter than the run timeout |
| CL010 | error | selector matches no event type the adapter emits (for example a misspelled `event_type`) |
| CL011 | error | an attribute or correlation id the contract needs is never present on the matched events |
| CL012 | warning | that key is missing on *some* matched event types, which will be unresolved |

Exit code 1 on any error (or any warning with `--strict`).

### Event catalogs

CL010–CL012 need the adapter's **event catalog**, an optional
`event_catalog()` method listing each event type it can emit with its
subsystems, attribute keys (shell patterns allowed), and correlation ids.
`check_events_against_catalog(events, catalog)` verifies a real event stream
stays within the catalog. Run it in the adapter's tests so the catalog cannot
drift from reality. The reference adapter does this against every reference
experiment.

## Editor support (JSON Schema)

```sh
crosslayer schema contracts            # print one schema
crosslayer schema --write schemas/     # regenerate the checked-in copies
```

Schemas for contract files, experiments, and contract tests are in
[`schemas/`](../schemas). With the YAML language server (for example the
Red Hat YAML extension in VS Code), add a first-line modeline to get
validation and autocomplete while typing:

```yaml
# yaml-language-server: $schema=https://raw.githubusercontent.com/Pranav-s79/Cross-Layer-Systems-Test-Platform/main/schemas/contracts.schema.json
```

The repository's own YAML files use relative modelines. The Python loaders
remain authoritative. They also check what a schema cannot (expression
safety, `min <= max`, references between files). Tests keep the schemas
**no stricter** than the loaders on every YAML file in the repository and on
a set of valid and invalid documents, so the editor never flags a file that
CrossLayer accepts.

## Experiment suites

```yaml
schema_version: 1
name: reference
experiments:
  - id: baseline
    path: baseline.yaml            # relative to the suite file
  - id: silent_drop
    path: fault_silent_drop.yaml
    expect: fail                   # default: pass
    depends_on: [baseline]
```

```sh
crosslayer suite experiments/reference/suite.yaml --out runs
```

* An experiment **meets its expectation** when its verdict equals `expect`.
  Fault-injection experiments should `expect: fail`.
* An experiment runs only if everything it `depends_on` met its expectation;
  otherwise it is **blocked** and does not run. Fault results are meaningless
  if the baseline already fails, so faults depend on the baseline. Blocking is
  transitive.
* Order is a topological sort (Kahn's algorithm). Among ready experiments,
  the one listed first runs first, so runs are deterministic. Cycles are
  rejected when the suite is loaded, with the cycle spelled out
  (`dependency cycle: a -> b -> c -> a`).
* Output goes to `runs/suite-<name>-<time>/`: `summary.json`, `report.md`,
  `junit.xml` (unmet = failure, blocked = skipped), and each run's bundle
  under `runs/`. The exit code is 0 only if every experiment met its expectation.

`crosslayer check` and `crosslayer lint` accept suite files too. Lint checks
every experiment in the suite.

## Contract versioning

Contracts carry an optional `version` (integer, default 1). Bump it whenever
**what the contract checks** changes: `rule`, `source`, `destination`,
`precondition`, `evidence`, `severity`, or `on_fail`. Wording (`title`,
`description`, `tags`) and `owner` can change without a bump. Defaults are
normalized, so omitting `severity` equals `severity: major`.

```sh
git show main:path/contracts.yaml > /tmp/old.yaml
crosslayer diff-contracts /tmp/old.yaml path/contracts.yaml             # exit 1 on problems
crosslayer diff-contracts /tmp/old.yaml path/contracts.yaml --markdown  # changelog per owner
```

Problems (exit code 1): a semantic change without a version bump, or a
version that went backwards. Use it in review: the owning team sees exactly
which of their contracts changed and how.

**Across runs:** `crosslayer compare` marks a contract whose definition
differs between the two bundles (`[contract changed: v1 -> v2]`). A
"regression" that comes from tightening a contract is then not mistaken for
the system getting worse; the event-stream fingerprint shows whether the
system's behavior changed at all.

## Scaffolding a new adapter

```sh
crosslayer new adapter rv32i_core --dir integrations/
cd integrations/rv32i_core && pip install -e . && pytest
```

Generates a complete project that works **before any edits**:

| File | Purpose |
|---|---|
| `rv32i_core/adapter.py` | Log-only adapter: collect + normalize, event catalog, TODOs where your system differs |
| `rv32i_core/contracts.yaml` | Two example contracts (a response deadline and a lifecycle state machine) |
| `rv32i_core/contract_tests.yaml` | A known-good and a known-bad case for each contract |
| `examples/sample.log`, `experiments/analyze_sample.yaml` | A sample log and an experiment that analyzes it |
| `tests/test_rv32i_core.py` | Conformance, contract tests with full coverage, lint, sample run, catalog check |
| `pyproject.toml` | Registers the adapter under the `crosslayer.adapters` entry point |

The generated README walks through adapting it: parse your log format,
describe your subsystems, declare the event catalog, write contracts, and add
contract tests. Keep `pytest` green at each step. CrossLayer's own test
suite generates a project and runs its tests, so the scaffold cannot rot.
