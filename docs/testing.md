# How CrossLayer is tested

A verification tool has to be trustworthy, so it's tested at five levels:
its own code, its contracts, a real end-to-end system, CI, and every bug fix.

## Run everything

```sh
python -m pytest                     # all automated tests
crosslayer lint experiments/reference/suite.yaml --strict
crosslayer test-contracts crosslayer/integrations/sensor_pipeline/contract_tests.yaml --require-coverage
crosslayer suite experiments/reference/suite.yaml --out runs
```

CI runs all four on every push: pytest on Linux and Windows across Python
3.11–3.14, then the last three in a "reference experiments" job.

## 1. Unit tests (`tests/core/`)

| Area | File | What it proves |
|---|---|---|
| Events and time | `test_events.py`, `test_units.py` | Invalid events can't be built; timestamps convert exactly |
| Expressions | `test_expressions.py` | Unsafe code is rejected at load; missing data is "unresolved", not a crash |
| Rules | `test_rules.py` | Each rule kind gives PASS and FAIL on hand-built traces, including edge cases |
| Contracts | `test_contracts.py` | Loader errors name the field; preconditions, evidence gating, verdict rules |
| Adapters | `test_adapters.py` | Declared capabilities match the code; plugin discovery works |
| Processes | `test_process.py` | Timeouts kill the whole process tree |
| Runner and bundles | `test_runner.py` | Complete bundles, tamper detection, exact replay, config errors leave no bundle |
| Tooling | `test_contract_testing.py`, `test_lint.py`, `test_schemas.py`, `test_suite.py`, `test_versioning.py` | Each Phase 2 tool; schemas never stricter than the loaders |
| Architecture | `test_import_boundary.py` | The core never imports an integration or a non-approved library |

## 2. Tests for the contracts themselves

A contract never seen to fail might check nothing. Each reference contract has
a trace it must PASS and one it must FAIL
(`crosslayer/integrations/sensor_pipeline/contract_tests.yaml`).
`--require-coverage` fails if any contract is missing either case. A mutation
test loosens contracts on purpose (longer deadline, higher threshold) and
checks that these cases catch it.

## 3. End-to-end on a reference system (`tests/integrations/`)

The reference system is a simulated sensor → ring buffer → consumer pipeline
with four injectable faults, each labeled with its true location.

| Test | What it proves |
|---|---|
| `test_baseline_passes_every_contract` | No false alarms on the healthy system |
| `test_each_fault_trips_its_contracts` | Each fault fails exactly the contracts meant to catch it |
| `test_silent_drop_evidence_points_at_raw_log` | Findings link to the right log lines |
| `test_same_seed_is_repeatable_and_fault_runs_share_traffic` | Same seed, same events; faults change only what they target |
| `test_truncated_log_is_inconclusive_not_failed` | Missing evidence gives INCONCLUSIVE, not FAIL |
| `test_event_catalog_matches_every_emitted_event` | The adapter's declared events match its real output |

The suite in CI repeats this from the command line: the baseline must pass and
each fault experiment must fail.

## 4. CLI and scaffold (`tests/test_cli.py`, `tests/test_scaffold.py`)

Every command's exit codes and output, plus `--help` for every subcommand.
The scaffold test generates a new adapter project and runs *its* test suite
in a subprocess, so the template can't silently break.

## 5. Every bug gets a regression test

Each fix adds a test, confirmed to fail on the old code before the fix is
committed (for example, `test_added_files_are_detected_by_verify`).

## Where to add a test

| You changed | Add or extend |
|---|---|
| A rule kind | `tests/core/test_rules.py` (a PASS and a FAIL case) |
| Contract loading or verdicts | `tests/core/test_contracts.py` |
| A lint check | `tests/core/test_lint.py` |
| A file format | `tests/core/test_schemas.py`, then `crosslayer schema --write schemas` |
| A CLI command | `tests/test_cli.py` |
| The reference system or its contracts | `tests/integrations/test_sensor_pipeline.py` and `contract_tests.yaml` |

Performance is measured separately with `python benchmarks/bench_core.py`,
because timings vary by machine. One test,
`test_response_time_window_mode_scales_linearly`, uses a generous time limit
to stop the old quadratic slowdown from returning.
