# Roadmap

CrossLayer starts as a small, verified core. The goal is a production
platform that many teams use to integrate, validate, and debug independently
built hardware and software.

> ✅ done and verified · 🔜 next · 📋 planned. Anything not marked ✅ is a
> plan, not a capability.

| Stage | Phase | Focus | Status |
|---|---|---|---|
| **I. Foundation** | 1 | Verification core | ✅ |
| | 2 | Contract tooling | ✅ |
| **II. Real integrations** | 3 | RTL processor verification (RV32I) | 🔜 |
| | 4 | Wireless log analysis (5G RAN + core) | 🔜 |
| | 5 | Live 5G virtual radio | 📋 |
| | 6 | Firmware and hardware-in-the-loop | 📋 |
| **III. Diagnosis** | 7 | Fault localization | 📋 |
| | 8 | Guided follow-up experiments | 📋 |
| | 9 | Learning-assisted triage | 📋 |
| **IV. Scale** | 10 | Streaming evaluation engine | 📋 |
| | 11 | Results store and regression analytics | 📋 |
| | 12 | Distributed execution | 📋 |
| **V. Production** | 13 | Platform service | 📋 |
| | 14 | Observability interoperability | 📋 |
| | 15 | Security and governance | 📋 |

## Principles

1. **The core never depends on a target.** Each new domain should need zero
   changes under `crosslayer/core/`.
2. **Evidence over assertions.** Every verdict links to raw artifacts; missing
   evidence is INCONCLUSIVE, never PASS.
3. **Measure before scaling.** Stage IV and V phases start only when a
   measurement shows the current design is the limit.
4. **Every phase has an exit criterion**, a fault it must catch, and a clean
   baseline it must not flag.

---

## Stage I — Foundation

### Phase 1 · Verification core ✅

**Exit criterion:** a generic adapter runs a test, records observations, and
produces a contract-based verdict.

| Requirement | Verified by |
|---|---|
| Adapter interface, plugin discovery, explicit unsupported operations | `tests/core/test_adapters.py` |
| Normalized event schema and storage | `tests/core/test_events.py` |
| Six rule kinds, safe expressions, evidence gating | `test_rules.py`, `test_expressions.py`, `test_contracts.py` |
| Same seed gives an identical event stream | `test_same_seed_is_repeatable_and_fault_runs_share_traffic` |
| Replay matches config hash and fingerprint | `test_bundled_configuration_replays_identically` |
| Tampered or added bundle files are detected | `test_tampering_is_detected`, `test_added_files_are_detected_by_verify` |
| Each fault is caught by exactly its target contracts | `test_each_fault_trips_its_contracts` |
| No false failures on the baseline | `test_baseline_passes_every_contract` |
| Truncated evidence gives INCONCLUSIVE, not FAIL | `test_truncated_log_is_inconclusive_not_failed` |
| Core independent of integrations | `tests/core/test_import_boundary.py` |
| CI on Linux and Windows with a fault-detection gate | `.github/workflows/ci.yml` |

A follow-up review fixed 13 issues, each with a regression test. The largest
was a quadratic path in the response rule (80k events: 6.6 s → 0.21 s).

**Known limits:** evaluation is in-memory; execution is local; a
`response` rule satisfies several triggers with one response unless it sets
`exclusive: true`.

### Phase 2 · Contract tooling ✅

**Exit criterion:** a new contributor writes an adapter and contracts from the
scaffold and passes conformance and contract tests without reading core code.

| Requirement | Verified by |
|---|---|
| Contract tests: traces each contract must pass and fail (reference: 25 cases, 8/8 covered) | `tests/core/test_contract_testing.py` |
| Contract tests catch a weakened contract | `test_contract_tests_catch_a_weakened_contract` |
| Linter, including misspelled events and attributes via adapter event catalogs | `tests/core/test_lint.py` |
| Event catalogs match real output | `test_event_catalog_matches_every_emitted_event` |
| JSON Schemas, never stricter than the loaders | `tests/core/test_schemas.py` |
| Suites: dependency order, expected verdicts, blocking, cycle errors, validate first | `tests/core/test_suite.py`; CI runs the reference suite |
| Versioning: unversioned changes rejected; `compare` flags changed contracts | `tests/core/test_versioning.py` |
| Scaffold passes conformance, coverage, lint, and a sample run before edits | `tests/test_scaffold.py` |

Tests show the scaffold starts green. "Without reading core code" is a
usability claim, checked by building the Phase 3 and 4 adapters from it.

---

## Stage II — Real integrations

### Phase 3 · RTL processor verification (RV32I) 🔜
* Adapter for Verilator / Icarus: build, run with a cycle limit, normalize per-cycle traces.
* Contracts on signals the design exposes: `PC + 4` sequencing, branch targets, register write-back.
* Lockstep comparison with an ISA model, reporting the first divergent instruction.
* Faults as **mutant RTL** (for example a wrong branch offset), with waveform references as evidence.

**Exit:** the original design passes; each mutant fails at its earliest
violating cycle; the core is unchanged.

### Phase 4 · Wireless log analysis (5G RAN + core) 🔜
* Parsers for OCUDU RAN and Open5GS core logs, with fixture source and version recorded.
* Join RAN and core events on explicit ids (NGAP UE id pairs), with a recorded time-window fallback.
* Per-source clock metadata; contracts for registration, session setup, and RRC states.

**Exit:** RTL and wireless run on the same unmodified core; fixture results
are labeled as fixtures.

### Phase 5 · Live 5G virtual radio 📋
* Containerized gNB, core, and UE over a virtual radio link, with traffic and KPIs.
* Labeled impairments: channel degradation, core delay, CPU contention.

**Exit:** a live end-to-end workflow is validated, and each impairment is detected.

### Phase 6 · Firmware and hardware-in-the-loop 📋
* ESP32-class target: build, flash, UART capture, reset.
* Lab instruments (power supplies, signal sources) as fault injectors, with safety interlocks.
* Hardware reservation for experiments.

**Exit:** a physical device runs a suite unattended, and instrument-injected
faults are detected.

---

## Stage III — Diagnosis

### Phase 7 · Fault localization 📋
* A graph of subsystems, interfaces, and contract boundaries.
* Start at the earliest violation and trace back along dependencies,
  including to components that log nothing.
* Ranked hypotheses with evidence, **counter-evidence**, and uncertainty.

**Exit:** top-1 and top-3 accuracy measured against fault labels, including
faults whose symptoms appear downstream of their cause.

### Phase 8 · Guided follow-up experiments 📋
* Recommend the experiment that best separates competing hypotheses; later
  pick by expected information gain and run it automatically.

**Exit:** fewer experiments to the right diagnosis than the manual workflow, measured.

### Phase 9 · Learning-assisted triage 📋
* Cluster failures, detect flaky contracts, learn localization priors.

**Entry:** enough labeled history for a held-out test set.
**Exit:** beats the Phase 7 baseline on held-out data, with explanations.

---

## Stage IV — Scale

### Phase 10 · Streaming evaluation engine 📋
* Rules as incremental state machines; memory bounded by open correlations.
* One pass routes events to all contracts; expressions compiled once.
* Native code only where profiling shows it pays.

**Entry:** traces too large for in-memory evaluation, measured.

### Phase 11 · Results store and regression analytics 📋
* Run index (SQLite, then PostgreSQL); bundles in object storage.
* Statistically tested KPI regressions across builds; per-owner history.

**Entry:** too many runs to search as directories.
**Exit:** any contract's history is queryable; regressions link to the first bad build.

### Phase 12 · Distributed execution 📋
* Scheduler and worker pool with containerized runners.
* Simulators, RF benches, and devices as reservable resources.
* Sharded matrices with retries, flaky quarantine, and budgets.

**Entry:** suites exceed CI time budgets on one machine, measured.

---

## Stage V — Production

### Phase 13 · Platform service 📋
API and web UI (cross-layer timelines, graph explorer, run diffs); contract
registry with ownership and review; failures routed to owning teams; CI plugins.

### Phase 14 · Observability interoperability 📋
OpenTelemetry ingestion as an adapter; verdicts and KPIs exported as metrics.

### Phase 15 · Security and governance 📋
SSO and per-team access; sandboxed adapters; signed bundles and provenance;
audit logs, retention, and SLOs for the platform itself.

**Stage V exit:** several teams use CrossLayer as a shared service with access
control, auditability, and published reliability targets.
