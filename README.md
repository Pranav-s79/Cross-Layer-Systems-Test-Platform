# CrossLayer

[![CI](https://github.com/Pranav-s79/Cross-Layer-Systems-Test-Platform/actions/workflows/ci.yml/badge.svg)](https://github.com/Pranav-s79/Cross-Layer-Systems-Test-Platform/actions/workflows/ci.yml)
![Python](https://img.shields.io/badge/python-3.11%2B-blue)
![Status](https://img.shields.io/badge/status-Phase%202%20of%2015-orange)

**Cross-stack verification for systems built by many teams.**

Components often pass their own tests and still fail together, because the
bugs live *between* them: missed deadlines, dropped data, mismatched
assumptions. CrossLayer lets you write those expectations as **contracts**,
run **experiments** (including fault injection), and get **verdicts backed by
evidence** that link to the raw log line.

```mermaid
flowchart LR
    SUT["System under test<br/>(RTL sim, RAN stack, firmware)"] -->|raw logs| A[Adapter]
    A -->|normalized events| E[Contract engine]
    C[Contracts<br/>YAML] --> E
    E --> B["Run bundle<br/>verdicts · evidence · replay"]
```

## Quick start

```sh
pip install -e ".[dev]"

crosslayer run experiments/reference/baseline.yaml            # PASS
crosslayer run experiments/reference/fault_silent_drop.yaml   # FAIL, with evidence
crosslayer compare runs/<baseline> runs/<faulty>              # regression diff
crosslayer suite experiments/reference/suite.yaml             # baseline + every fault
```

```text
verdict   FAIL
events    800 (fingerprint 0654f85f7b561e31)
faults    silent_drop

  FAIL         pipeline.rx_accounted          397/400 triggers answered within 5 ms; 3 violations, 0 unresolved
  PASS         pipeline.packet_lifecycle      397 transitions across 400 entities; 0 illegal
  ...
  FAIL         pipeline.counter_conservation  held for 0/1 events
```

Each violation in `report.md` points to its source, such as `raw_artifacts/sim.log#L315`.

## A contract

```yaml
- id: pipeline.rx_accounted
  title: Every received packet is processed or explicitly dropped within 5 ms
  owner: firmware-rx
  source: sensor_rx
  destination: proc
  evidence: [{event_type: system.halt}]   # truncated log -> INCONCLUSIVE, not FAIL
  rule:
    kind: response
    trigger: {event_type: packet.received}
    response: [{event_type: packet.processed}, {event_type: packet.dropped}]
    correlate_by: packet
    within_ms: 5
```

Six rule kinds cover deadlines, state machines, percentiles, invariants, and
step-to-step relations such as `PC + 4`. → [Contract language](docs/contract-language.md)

## What works today

- **Adapters:** any system plugs in through one interface. The core never
  imports a target (enforced by a test).
- **Contract engine:** six rule kinds, a sandboxed expression language, and
  PASS / FAIL / INCONCLUSIVE verdicts with no vacuous passes.
- **Reproducible runs:** seeded experiments, hashed run bundles, replay, and
  repeatability checks.
- **CI-ready:** JUnit output, exit codes, regression diffs, and suites with
  dependency order and expected verdicts.
- **Contract tooling:** contract tests, a linter, JSON Schemas, versioning,
  and `crosslayer new adapter` scaffolding.
- **Fault injection:** four faults in a reference pipeline, each caught by
  exactly its target contracts, with a clean baseline.

> The reference system is a deterministic **simulation**, not real firmware.

## Roadmap

| Stage | Phases | Status |
|---|---|---|
| Foundation | Verification core · contract tooling | ✅ done |
| Real integrations | RV32I RTL · 5G RAN/core logs · live 5G · firmware HIL | 🔜 next |
| Diagnosis | Fault localization · guided experiments · learning-assisted triage | 📋 planned |
| Scale | Streaming engine · results store · distributed execution | 📋 planned |
| Production | Platform service · OpenTelemetry · security and governance | 📋 planned |

→ [Full roadmap](ROADMAP.md)

## Documentation

| | |
|---|---|
| [Architecture](docs/architecture.md) | Layers, data flow, enforced boundaries |
| [Tutorial](docs/tutorial.md) | Add, test, and run your first contract |
| [Contract language](docs/contract-language.md) | Fields, selectors, rules, expressions, verdicts |
| [Contract tooling](docs/tooling.md) | Tests, lint, schemas, suites, versioning, scaffolding |
| [Writing an adapter](docs/writing-an-adapter.md) | Connect a new system |
| [Run bundle format](docs/run-bundle.md) | What every run records |
| [Testing](docs/testing.md) | How CrossLayer is tested, and where to add tests |
| [Extending](docs/extending.md) | Extension points and where the project is going |
| [Contributing](CONTRIBUTING.md) | Dev setup, checks, schemas, commit style |

## Development

```sh
python -m venv .venv && . .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
pytest
python benchmarks/bench_core.py
```

Exit codes: `0` pass · `1` fail/regression · `2` inconclusive · `3` error · `4` config error
