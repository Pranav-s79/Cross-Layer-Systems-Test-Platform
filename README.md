# CrossLayer

**A cross-stack verification, experimentation, and fault-localization platform.**

Individual components can pass their own tests while the integrated system
still fails. These failures tend to happen at subsystem boundaries: timing
violations, lost or corrupted data, inconsistent assumptions between teams.
CrossLayer lets you state what must hold at those boundaries as
machine-readable **contracts**, run controlled **experiments** (including
deliberate fault injection), and get **evidence-backed verdicts** that point
back to the exact raw log lines involved.

The core is domain-independent. A system under test plugs in through an
**adapter**, which runs it, collects its output, and normalizes it into a
common event schema. The same contract engine, runner, and reports then work
for RTL simulations, wireless-stack logs, or firmware traces.

## Status

| Area | State |
|---|---|
| Adapter interface, entry-point discovery, conformance checks | Implemented |
| Normalized event schema (JSONL, integer-ns timestamps) | Implemented |
| Contract engine: 6 rule kinds, safe expressions, evidence gating | Implemented |
| Experiment runner: seeds, timeouts, process-tree kill, fault injection | Implemented |
| Reproducible run bundles: hashed manifest, replay script, JUnit, Markdown report | Implemented |
| Run comparison: regressions, identical-seed repeatability | Implemented |
| Reference system under test (simulated sensor pipeline, 4 faults) | Implemented |
| CI (Linux + Windows, reference fault-detection gate) | Configured |
| RV32I RTL integration (Icarus Verilog / Verilator) | Planned (Phase 2) |
| OCUDU-format wireless log integration | Planned (Phase 2) |
| Dependency graph and ranked fault localization | Planned (Phase 3) |
| Live OCUDU + Open5GS + UE virtual-radio run | Planned (Phase 4) |
| Follow-up experiment recommendation | Planned (Phase 5) |

The reference system is a deterministic **simulation** used to validate the
core end to end. It is not real firmware, and results from it are not claims
about any real product.

## Quick start

```sh
python -m venv .venv
. .venv/bin/activate            # Windows: .venv\Scripts\activate
pip install -e ".[dev]"

pytest                          # 154 tests
crosslayer adapters             # list discovered adapters
crosslayer run experiments/reference/baseline.yaml
crosslayer run experiments/reference/fault_silent_drop.yaml
crosslayer compare runs/<baseline-run> runs/<faulty-run>
```

Example output for the silent-drop fault:

```
run       20261102T182901Z-8a7b35f0
verdict   FAIL
events    800 (fingerprint 0654f85f7b561e31)
faults    silent_drop

  FAIL         pipeline.rx_accounted          397/400 triggers answered within 5 ms; 3 violations, 0 unresolved
  PASS         pipeline.packet_lifecycle      397 transitions across 400 entities; 0 illegal
  ...
  FAIL         pipeline.counter_conservation  held for 0/1 events
```

Each violation in `report.md` links to its raw evidence, for example
`raw_artifacts/sim.log#L315`.

Exit codes: `0` pass, `1` fail or regression, `2` inconclusive, `3` error,
`4` configuration error.

## A contract

```yaml
- id: pipeline.rx_accounted
  title: Every received packet is processed or explicitly dropped within 5 ms
  owner: firmware-rx
  source: sensor_rx
  destination: proc
  severity: critical
  evidence: [{event_type: system.halt}]   # truncated log -> INCONCLUSIVE, not FAIL
  rule:
    kind: response
    trigger: {event_type: packet.received}
    response: [{event_type: packet.processed}, {event_type: packet.dropped}]
    correlate_by: packet
    within_ms: 5
```

Rule kinds: `count`, `response`, `transitions`, `threshold`, `invariant`,
`successive`. See [docs/contract-language.md](docs/contract-language.md).

## Repository layout

```
crosslayer/
  core/                 domain-independent platform (never imports integrations)
    adapters/           SystemAdapter interface, registry, conformance checks
    telemetry/          NormalizedEvent schema, JSONL store, fingerprints
    contracts/          contract model, rules, safe expressions, evaluator
    orchestrator/       experiment config, process runner, runner, compare
    artifacts/          run bundle, environment capture, JUnit, report
  integrations/
    sensor_pipeline/    reference simulated system + adapter + contracts
  cli.py
experiments/reference/  baseline and fault-injection experiments
benchmarks/             measurement scripts
tests/                  unit, integration, and architecture tests
docs/                   architecture, contract language, adapter guide, bundle format
```

## Documentation

- [Architecture](docs/architecture.md)
- [Contract language](docs/contract-language.md)
- [Writing an adapter](docs/writing-an-adapter.md)
- [Run bundle format](docs/run-bundle.md)
