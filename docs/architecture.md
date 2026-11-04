# Architecture

What is implemented today. Planned work is in the [roadmap](../ROADMAP.md).

## Data flow

```
 experiment.yaml ──► ExperimentRunner.plan()          validate everything first
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

| Layer | Status | Code |
|---|---|---|
| 1. Systems under test | Reference simulation only | `integrations/sensor_pipeline/sim.py` |
| 2. Adapters | Implemented | `core/adapters/` |
| 3. Orchestrator | Implemented (local) | `core/orchestrator/` |
| 4. Contract engine | Implemented | `core/contracts/` |
| 5. Diagnosis | Planned (Phase 7) | — |
| 6. Output | JSON, Markdown, JUnit, comparison | `core/artifacts/`, `orchestrator/compare.py` |

**Adapters.** `SystemAdapter` (`core/adapters/base.py`) defines `describe`,
`capabilities`, `build`, `execute`, `collect`, `normalize`, `contracts`, and
`faults`. Unimplemented operations raise `UnsupportedOperation`.
`check_adapter_conformance` checks that declared capabilities match the code
and that faults and contracts reference declared subsystems. Adapters are
found through the `crosslayer.adapters` entry point or a `module:Class` path.

**Orchestrator.**
* `experiment.py`: strict YAML with a required seed. Computes the **config
  hash** over the effective experiment, including every contract and the
  contents of input files.
* `process.py`: runs the system in its own process group, so a timeout kills
  the whole tree.
* `runner.py`: the pipeline above. Config errors stop before any bundle
  exists; runtime failures are recorded with verdict ERROR.
* `compare.py`: per-contract diff of two bundles, plus a fingerprint check
  for repeatability.
* `suite.py`: experiments in dependency order with expected verdicts.

Container and distributed execution are planned (Phase 12).

**Contracts.** A `BoundaryContract` has an owner, a `source -> destination`
boundary, a rule, a severity, `on_fail` (`fail_run` or `warn`), an optional
precondition, and required evidence. Evaluation order:

1. Precondition not observed → **SKIPPED**
2. Rule raises → **ERROR** (the check is broken, not the system)
3. Required evidence missing → **INCONCLUSIVE** (the rule's opinion is kept as `rule_verdict`)
4. Otherwise the rule's verdict

With zero observations, rules give INCONCLUSIVE, never a vacuous PASS. See
[contract-language.md](contract-language.md).

**Diagnosis (planned).** The inputs exist: contracts name their boundary,
adapters declare subsystems and interfaces, findings record when they became
observable, and faults declare their true target.

**Tooling** ([tooling.md](tooling.md)):

| Module | Purpose |
|---|---|
| `core/contracts/testing.py` | Contract tests and coverage |
| `core/contracts/lint.py`, `core/orchestrator/lint.py` | Static checks, using adapter event catalogs |
| `core/schemas.py` | JSON Schemas (copies in `schemas/`) |
| `core/orchestrator/suite.py` | Suites |
| `core/contracts/versioning.py` | Contract diffs and changelogs |
| `scaffold/` | `crosslayer new adapter` templates (outside the core) |

## Rules enforced by tests

| Rule | Enforced by |
|---|---|
| `crosslayer.core` never imports `crosslayer.integrations` | `tests/core/test_import_boundary.py` |
| Core depends only on the standard library and PyYAML | `tests/core/test_import_boundary.py` |
| Adapter declarations match implementations | `check_adapter_conformance` |
| Every injected fault is detected; the baseline stays clean | `tests/integrations/test_sensor_pipeline.py`, CI |

`core` and `integrations` live under one package, `crosslayer/`, which avoids
a top-level package named `core` and keeps one namespace for entry points.
