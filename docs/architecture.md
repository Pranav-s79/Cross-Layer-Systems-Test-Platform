# Architecture

This document describes the architecture **as implemented**. Planned work is
in the [roadmap](../ROADMAP.md). Each layer below is marked with its status.

## Data flow

```
 experiment.yaml ──► ExperimentRunner.plan()          validate before running anything
                         │  adapter capabilities, fault names/params,
                         │  contract syntax + ids + subsystem references
                         ▼
                    RunBundle.create()                configuration.yaml, versions.json
                         │
        ┌────────────────┼──────────────── adapter (system-specific) ───────────┐
        │  build() ─► execute() ─► collect() ─► normalize()                      │
        │  (optional)  (optional)   raw_artifacts/   NormalizedEvent[] + diagnostics
        └────────────────┼───────────────────────────────────────────────────────┘
                         ▼
                    order_events()                    stable sort by timestamp_ns
                         ▼
                    evaluate_contracts()              precondition -> rule -> evidence gate
                         ▼
                    aggregate_verdict()               PASS / FAIL / INCONCLUSIVE / ERROR
                         ▼
          contracts.json, violations.json, report.md, junit.xml, replay.sh
                         ▼
                    manifest.json                     SHA-256 of every file, written last
```

## Layers

| Layer | Name | Status | Code |
|---|---|---|---|
| 1 | Systems under test | Reference simulation only | `crosslayer/integrations/sensor_pipeline/sim.py` |
| 2 | Integration adapters | Implemented | `crosslayer/core/adapters/` |
| 3 | Experiment orchestrator | Implemented (local execution) | `crosslayer/core/orchestrator/` |
| 4 | Contract verification engine | Implemented | `crosslayer/core/contracts/` |
| 5 | Fault propagation and diagnosis | Planned (roadmap Phase 7) | — |
| 6 | Engineering output | Partial: JSON, Markdown, JUnit, comparison | `crosslayer/core/artifacts/`, `orchestrator/compare.py` |

### Layer 2: adapters

`SystemAdapter` (`core/adapters/base.py`) has the lifecycle
`describe / capabilities / build / execute / collect / normalize / contracts / faults`.
Operations an adapter does not support are simply not implemented: the base
class raises `UnsupportedOperation`. `check_adapter_conformance`
(`core/adapters/conformance.py`) checks that the declared capabilities match
the overridden methods, that faults and contracts reference declared
subsystems, and that ids are unique.

Adapters are discovered through the `crosslayer.adapters` entry-point group
(`core/adapters/registry.py`), or loaded by `module:Class` path.

### Layer 3: orchestrator

* `experiment.py`: strict YAML schema with a required seed and explicit
  timeout. It also computes the **config hash**, the identity of the
  effective experiment, which covers every evaluated contract and hashes input
  files by content.
* `process.py`: launches the system under test in its own process group, so a
  timeout kills the whole tree. Durations come from `time.monotonic()`.
* `runner.py`: the pipeline above. Configuration errors raise before any
  bundle exists. Runtime failures are recorded in the bundle with verdict
  ERROR, so there is always something to inspect.
* `compare.py`: compares two bundles per contract (regression, fixed,
  lost or gained coverage) and checks event-stream fingerprints for
  identical-seed repeatability.

Not yet implemented: experiment suites with dependency ordering (roadmap
Phase 2), and container or distributed execution (Phase 12).

### Layer 4: contracts

A `BoundaryContract` names an owner, a `source -> destination` boundary, a
rule, a severity, a failure policy (`fail_run` or `warn`), an optional
precondition, and required evidence. Evaluation (`core/contracts/evaluator.py`):

1. The precondition is not observed: **SKIPPED**.
2. The rule raises: **ERROR** (the verification is broken, not the system).
3. Required evidence is missing: **INCONCLUSIVE**, and the rule's own opinion
   is kept as `rule_verdict`.
4. Otherwise the rule's verdict stands.

Rules (`core/contracts/rules.py`) never report a vacuous PASS. With zero
observations they are INCONCLUSIVE unless the author opts out. See
[contract-language.md](contract-language.md).

### Layer 5: diagnosis (planned)

The foundations are in place: contracts carry `source`/`destination`,
adapters declare subsystems and interfaces, every finding records when it
became observable (`timestamp_ns`) with evidence references, and every fault
declares its ground-truth target. Roadmap Phase 7 will build the dependency graph and
rank hypotheses on top of these.

### Tooling (Phase 2)

Static and developer tooling around the same core, documented in
[tooling.md](tooling.md):

| Module | Purpose |
|---|---|
| `core/contracts/testing.py` | Contract tests: known-good and known-bad traces per contract, coverage |
| `core/contracts/lint.py`, `core/orchestrator/lint.py` | Static checks, including against an adapter's `event_catalog()` |
| `core/schemas.py` | JSON Schemas for every YAML file type (checked-in copies under `schemas/`) |
| `core/orchestrator/suite.py` | Suites: topological order, expected verdicts, blocking |
| `core/contracts/versioning.py` | Contract diffs and per-owner changelogs |
| `scaffold/` | `crosslayer new adapter` project templates (outside the core) |

## Architectural rules (enforced by tests)

| Rule | Enforced by |
|---|---|
| `crosslayer.core` never imports `crosslayer.integrations` | `tests/core/test_import_boundary.py` |
| Core depends only on the standard library + PyYAML | `tests/core/test_import_boundary.py` |
| Adapter declarations match implementations | `check_adapter_conformance`, `tests/integrations/test_sensor_pipeline.py` |
| Every injected fault is detected; baseline stays clean | `tests/integrations/test_sensor_pipeline.py`, CI `reference-experiments` job |

## Package layout note

`core` and `integrations` live under one importable package, `crosslayer/`,
as `crosslayer.core` and `crosslayer.integrations`, rather than as separate
top-level packages. That avoids a top-level
package named `core`, which collides easily with other packages, and it keeps
one namespace for entry points. The import-boundary rule is unchanged and
is enforced by a test.
