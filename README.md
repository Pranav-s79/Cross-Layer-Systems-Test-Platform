# CrossLayer

[![CI](https://github.com/Pranav-s79/Cross-Layer-Systems-Test-Platform/actions/workflows/ci.yml/badge.svg)](https://github.com/Pranav-s79/Cross-Layer-Systems-Test-Platform/actions/workflows/ci.yml)
![Python](https://img.shields.io/badge/python-3.11%2B-blue)
![Status](https://img.shields.io/badge/phases%20done-1--4%2C%207%20of%2015-green)

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

Each violation in `report.md` and the interactive `report.html` points to its
raw evidence, such as `raw_artifacts/sim.log#L315`.

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

Seven rule kinds cover deadlines, state machines, percentiles, coverage, invariants, and
step-to-step relations such as `PC + 4`. → [Contract language](docs/contract-language.md)

## What works today

- **Adapters:** any system plugs in through one interface. The core never
  imports a target (enforced by a test).
- **Contract engine:** seven rule kinds, a sandboxed expression language, and
  PASS / FAIL / INCONCLUSIVE verdicts with no vacuous passes.
- **Reproducible runs:** seeded experiments, hashed run bundles, replay, and
  repeatability checks.
- **CI-ready:** JUnit output, exit codes, regression diffs, and suites with
  dependency order and expected verdicts.
- **Contract tooling:** contract tests, a linter, JSON Schemas, versioning,
  and `crosslayer new adapter` scaffolding.
- **RTL verification:** two unmodified RV32I cores (PicoRV32, and FemtoRV32
  Quark, which has no RVFI port) under Icarus Verilog; declarative core
  profiles (RTL, testbench, mutants in YAML); RVFI, Spike commit-log, and
  waveform (VCD) trace sources; structural and lockstep contracts; seven
  injected faults per core; seeded random programs.
- **Wireless log analysis:** declarative profiles for Open5GS AMF/SMF and
  OCUDU gNB logs; exact NGAP UE id-pair joins with labeled time fallbacks;
  registration, PDU-session, and RRC-state contracts over synthetic fixtures.
- **Fault localization:** every failing run ranks where the fault most likely
  started, with evidence, counter-evidence, and a stated confidence; top-1
  19/26 and top-3 26/26 over all labeled fault experiments.
- **Fault injection:** five faults in the reference pipeline and seven per RV32I
  core, plus three faulty 5G log fixtures, with clean baselines and explicit
  target labels.

> The sensor pipeline is a deterministic simulation. The 5G logs are labeled
> synthetic fixtures. The RV32I integration runs a real processor RTL core in
> a simulator; it is not hardware-in-the-loop.

## Roadmap

| Stage | Phases | Status |
|---|---|---|
| Foundation | Verification core · contract tooling | ✅ done |
| Real integrations | RV32I RTL · 5G RAN/core logs · live 5G · firmware HIL | RV32I and 5G log analysis done; live 5G next |
| Diagnosis | Fault localization · guided experiments · learning-assisted triage | localization done; guided experiments next |
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
| [RTL verification](docs/rtl-verification.md) | Run PicoRV32, inject mutants, and inspect lockstep evidence |
| [Log analysis](docs/log-analysis.md) | Turn text logs into events with YAML log profiles |
| [Fault localization](docs/fault-localization.md) | Ranked hypotheses for where a failure started, and their measured accuracy |
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
