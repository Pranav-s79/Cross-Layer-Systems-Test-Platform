# Roadmap

CrossLayer is built in stages. The first stage is a small, verified core;
the end goal is a production verification platform that many teams use to
integrate, validate, and debug independently built hardware and software.

> **Status legend:** ✅ done and verified · 🔜 next · 📋 planned
>
> Everything not marked ✅ is a plan, not a capability. No performance,
> accuracy, or scale claim is made until it has been measured.

## At a glance

| Stage | Phase | Focus | Status |
|---|---|---|---|
| **I. Foundation** | 1 | Verification core | ✅ |
| | 2 | Contract tooling and developer experience | 📋 |
| **II. Real integrations** | 3 | RTL processor verification (RV32I) | 🔜 |
| | 4 | Wireless log analysis (5G RAN + core) | 🔜 |
| | 5 | Live 5G virtual-radio integration | 📋 |
| | 6 | Embedded firmware and hardware-in-the-loop | 📋 |
| **III. Diagnosis** | 7 | Dependency graph and fault localization | 📋 |
| | 8 | Guided follow-up experiments | 📋 |
| | 9 | Learning-assisted triage | 📋 |
| **IV. Scale** | 10 | Streaming evaluation engine | 📋 |
| | 11 | Results store and regression analytics | 📋 |
| | 12 | Distributed execution | 📋 |
| **V. Production** | 13 | Platform service and collaboration | 📋 |
| | 14 | Observability interoperability | 📋 |
| | 15 | Security, reliability, and governance | 📋 |

## Guiding principles

1. **The core never depends on a target.** Integrations plug in through
   adapters, and each new domain is a test of that rule: success means *zero*
   changes under `crosslayer/core/`.
2. **Evidence over assertions.** Every verdict links to raw artifacts. Missing
   evidence means INCONCLUSIVE, never PASS.
3. **Measure before scaling.** Stage IV and V phases have an *entry
   criterion*: a measured limit of the current design that justifies the
   added complexity.
4. **Every phase ends with a measurable exit criterion** and a fault-injection
   demo that the new capability must catch, plus a clean baseline it must not
   flag.

---

## Stage I — Foundation

### Phase 1 · Verification core ✅

**Exit criterion:** a generic adapter can run a test, record observations,
and produce a contract-based verdict.

| Requirement | Verified by |
|---|---|
| Adapter interface, plugin discovery, explicit unsupported operations | `tests/core/test_adapters.py` |
| Normalized event schema and storage | `tests/core/test_events.py` |
| Contract engine: six rule kinds, safe expressions, evidence gating | `tests/core/test_rules.py`, `test_expressions.py`, `test_contracts.py` |
| Deterministic runs: same seed gives an identical event stream | `test_sensor_pipeline.py::test_same_seed_is_repeatable_and_fault_runs_share_traffic` |
| Reproducible bundles: replay matches config hash and fingerprint | `test_runner.py::test_bundled_configuration_replays_identically` |
| Tamper detection on bundles | `test_runner.py::test_tampering_is_detected`, `test_added_files_are_detected_by_verify` |
| Controlled faults detected by exactly their target contracts | `test_sensor_pipeline.py::test_each_fault_trips_its_contracts` |
| No false failures on the known-good baseline | `test_sensor_pipeline.py::test_baseline_passes_every_contract` |
| Truncated evidence is INCONCLUSIVE, not FAIL | `test_sensor_pipeline.py::test_truncated_log_is_inconclusive_not_failed` |
| Core independent of integrations | `tests/core/test_import_boundary.py` |
| CI on Linux and Windows with a fault-detection gate | `.github/workflows/ci.yml` |

A post-implementation review found and fixed 13 issues, including a
quadratic-time path in the response rule (80k events: 6.6 s to 0.21 s). Each
is guarded by a regression test that fails on the old code.

**Known limits carried forward:** evaluation is in-memory; execution is local;
in time-window-only mode one response can satisfy several triggers.

### Phase 2 · Contract tooling and developer experience 📋

* JSON Schema export for contracts and experiments (editor validation, autocomplete).
* **Contract tests:** fixture traces that a contract must accept (known-good)
  and reject (known-bad), so contracts are verified like code.
* Contract linting: unreachable states, selectors that can never match,
  deadlines longer than the run.
* Experiment suites with dependency ordering (run B only if A passed).
* Contract versioning with a changelog per owner, and `crosslayer new adapter` scaffolding.

**Exit criterion:** a new contributor writes an adapter plus contracts from
the scaffold and passes conformance and contract tests without reading core code.

---

## Stage II — Real integrations

### Phase 3 · RTL processor verification (RV32I) 🔜

* Adapter for Verilator and Icarus Verilog: build the testbench, run with a
  cycle limit, normalize per-cycle traces (cycle-based timestamps).
* Contracts limited to signals the design actually exposes: PC sequencing
  (`PC + 4` for non-control-flow instructions), control-flow targets, register
  write-back consistency.
* Lockstep comparison against an ISA reference model, reporting the first
  divergent cycle and instruction.
* Fault injection through **mutant RTL** (for example a branch-offset or ALU
  operation bug), with VCD waveform references as evidence.

**Exit criterion:** the original design passes, each mutant fails with its
earliest violating cycle cited, and the core is unchanged.

### Phase 4 · Wireless log analysis (5G RAN + core) 🔜

* Parsers for documented OCUDU RAN logs and Open5GS core logs, with each
  fixture's source and version recorded.
* Cross-component identity mapping (for example NGAP UE id pairs) so RAN and
  core events join on explicit ids, and a recorded fallback to bounded time
  windows when they are absent.
* Clock-alignment metadata per source; contracts for registration, session
  establishment, and RRC state transitions.

**Exit criterion:** two engineering domains (RTL and wireless) run on the
same unmodified core; fixture results are labeled as fixtures, never as live runs.

### Phase 5 · Live 5G virtual-radio integration 📋

* Orchestrate gNB, 5G core, and UE processes over a virtual radio link
  (containerized), with traffic generation and KPI collection
  (registration latency, throughput, error rates).
* Controlled impairments: channel degradation, core-network delay, CPU
  contention, each with a ground-truth label.

**Exit criterion:** CrossLayer validates a live multi-component wireless
workflow end to end, and each injected impairment is detected.

### Phase 6 · Embedded firmware and hardware-in-the-loop 📋

* ESP32-class firmware target: build, flash, UART/log capture, reset control.
* Lab instrument control (programmable power supplies, signal sources) as
  adapter capabilities, with safety interlocks on fault injection.
* Device and fixture inventory so experiments reserve real hardware.

**Exit criterion:** a physical device runs a contract suite unattended, and
power or timing faults injected by instruments are detected and attributed.

---

## Stage III — Diagnosis

### Phase 7 · Dependency graph and fault localization 📋

* Build a graph from declared subsystems, interfaces, and contract boundaries.
* Locate the earliest observable violation and propagate it along declared
  dependencies, including into components that emit no events of their own.
* Ranked hypotheses, each with supporting evidence, **counter-evidence**
  (passing contracts that argue against it), missing evidence, and uncertainty.
  Several hypotheses can stay open.
* Outputs `dependency_graph.json` and `diagnosis.json`, plus an evaluation
  harness over a labeled fault corpus.

**Exit criterion:** top-1 and top-3 localization accuracy measured against
fault labels across every reference fault, including faults whose evidence
appears downstream of their cause.

### Phase 8 · Guided follow-up experiments 📋

* When hypotheses compete, recommend the experiment that best separates
  them (for example vary radio conditions independently of compute load).
* Start rule-based; later choose experiments by expected information gain,
  then close the loop by running them automatically.

**Exit criterion:** a controlled benchmark shows fewer experiments or less
manual effort to reach the correct diagnosis than the baseline workflow.

### Phase 9 · Learning-assisted triage 📋

* Cluster and deduplicate failures across runs and builds.
* Detect flaky contracts and anomalous log patterns.
* Learn localization priors from labeled history.

**Entry criterion:** enough labeled run history to train and hold out a test set.
**Exit criterion:** measurably better than the Phase 7 rule-based baseline on
held-out data, with explanations attached to every suggestion.

---

## Stage IV — Scale

### Phase 10 · Streaming evaluation engine 📋

* Rules as incremental state machines over an event stream: memory bounded by
  open correlations, not trace length.
* Single-pass routing of events to many contracts, and expressions compiled
  once instead of interpreted per event.
* Native hot paths (Rust or C++) only where profiling shows they pay off.

**Entry criterion:** traces that exceed in-memory evaluation, measured.
**Target:** multi-gigabyte traces evaluated with bounded memory and
throughput reported per rule kind.

### Phase 11 · Results store and regression analytics 📋

* Persistent run index (SQLite, then PostgreSQL) with bundles in
  S3-compatible object storage and retention policies.
* Cross-build trends, statistically tested KPI regressions, and per-owner
  quality history.

**Entry criterion:** run volume beyond what directory-based bundles can
search effectively.
**Exit criterion:** any contract's history across builds is queryable, and
regressions are flagged automatically with a link to the first bad build.

### Phase 12 · Distributed execution 📋

* Scheduler and worker pool with containerized runners.
* Resource-aware scheduling: simulators, RF lab benches, and physical devices
  as reservable resources.
* Experiment matrices sharded across workers, with retries, quarantine for
  flaky targets, and time or cost budgets.

**Entry criterion:** suite wall-clock time exceeds CI budgets on one machine, measured.
**Target:** thousands of runs per day across a worker fleet, with
reproducibility preserved per bundle.

---

## Stage V — Production

### Phase 13 · Platform service and collaboration 📋

* REST/gRPC API and a web UI: cross-layer timelines, dependency-graph
  explorer, run-to-run diffs.
* Contract registry with ownership, review, and approval workflows.
* Failure routing to the owning team (chat, issue tracker) and CI plugins
  (GitHub Actions, Jenkins, GitLab).

### Phase 14 · Observability interoperability 📋

* Ingest OpenTelemetry traces, logs, and metrics through a standard adapter.
* Export verdicts and KPIs as metrics for existing dashboards.

### Phase 15 · Security, reliability, and governance 📋

* SSO and role-based access per team; sandboxed adapter execution; secrets management.
* Signed run bundles and build provenance for tamper-evident evidence.
* Audit logs, data retention, multi-tenancy, and SLOs for the platform itself.

**Exit criterion for Stage V:** multiple teams use CrossLayer as a shared
service, with access control, auditability, and published reliability targets.
