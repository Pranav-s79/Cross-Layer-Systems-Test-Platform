# Milestones

The active work queue for roadmap Phases 3 and 4. Each milestone is one
`feature/<slug>` branch and one pull request; each step is one commit that
leaves every check green. Small, unrelated fixes stay in
[backlog.md](backlog.md); direction and exit criteria are in the
[roadmap](../ROADMAP.md).

**Rules.** The [backlog rules](backlog.md) apply. Work the first unticked step
of a milestone whose owner is you, in order. Tick a step in the commit that
completes it. Never take a milestone owned by someone else. A milestone
merges only when its exit check passes.

| Owner | Meaning |
|---|---|
| `lead` | Worked interactively; others must not touch its files |
| `routine` | The scheduled maintainer may take it |

## Phase 3 · More cores

### 3.1 · Land Phase 3 · `lead` · done
- [x] Rebase the PicoRV32 lockstep branch onto main and merge it.

### 3.2 · Core profiles · `lead` · done
- [x] `cores/<name>/core.yaml` with RTL, testbench, defines, provenance, and
  mutants; `parameters.core`; `SystemAdapter.faults_for`.

### 3.3 · Trace sources beyond RVFI · `lead` · done
- [x] Move line parsing out of `_TraceParser` into `traces/rvfi.py` behind a
  `TraceSource` interface that yields typed records (boot, retire, output,
  bus error, halt, timeout). No behavior change.
- [x] `trace: commit-log`: Spike-style commit lines (`core 0: 3 0x… (0x…) x5 0x…`,
  `mem 0x… 0x…`). Fields the format lacks stay absent, so the contracts that
  need them are INCONCLUSIVE, never PASS.
- [x] `trace: vcd-signals`: the profile maps RVFI-equivalent fields to
  waveform signals plus a `valid` signal and clock; a standard-library VCD
  reader samples them. Works for any core that can dump a waveform.

**Exit:** the PicoRV32 suite passes unchanged through each of the three
sources (RVFI text, a commit log derived from it, and its VCD).

### 3.4 · Second core · `lead` · done
- [x] Vendor a second permissively licensed Verilog-2005 RV32I core that
  Icarus can compile (check the license and record the upstream commit),
  with a profile that uses a non-RVFI trace source.
- [x] Port the mutants that apply to it and add `experiments/rv32i-<core>/`.

**Exit:** two cores, two trace formats, no changes under `crosslayer/core`.

### 3.5 · Seed sweeps and ISA coverage · `lead` · done
- [x] `crosslayer suite` entries can repeat an experiment over a seed list
  (`seeds: [1, 2, 3]` or `seeds: {count: N}`); each seed gets its own bundle
  and the suite reports per-seed verdicts.
- [x] The rv32i adapter records ISA coverage in `normalize` stats: retired
  count per mnemonic, taken and not-taken per branch kind, and each load and
  store width and alignment; shown in `report.md`.
- [x] A `coverage` contract kind or threshold contract that fails when a
  listed mnemonic never retires (for example the self-test must retire every
  RV32I mnemonic the assembler supports).

**Exit:** a 20-seed random-program sweep passes on PicoRV32, and the report
lists any RV32I mnemonic the sweep never retired.

### 3.6 · RV32I hardening · `routine` · `feature/rtl-hardening`
- [x] A JSON Schema for core profiles (`schemas/rv32i-core-profile.schema.json`)
  that is never stricter than `profile.py`: every built-in profile validates,
  and a test checks the schema against the loader's accepted keys. Reference it
  from the `yaml-language-server` comment in each `core.yaml`.
- [x] Assembler and decoder round-trip tests: for seeded random valid RV32I
  instructions, `decode(assemble(text))` gives back the mnemonic and operands,
  and every reserved or unsupported encoding decodes as illegal in `iss.py`.
- [x] `crosslayer show` (or the report) prints, for an RV32I bundle, the first
  failing retirement with its source line, PC, and observed vs reference
  fields side by side. Generic in the core: driven by the contract's first
  finding and the event's `*_ref`-style attribute pairs, not by RV32I names.

**Exit:** all three steps merged with tests; no change in any suite verdict.

## Phase 4 · Wireless log analysis

### 4.1 · Declarative log parser · `lead` · done
- [x] A `textlog` integration whose YAML log profile maps line regexes to
  event types and attributes, with a timestamp format, a time zone, and
  per-source clock metadata (`confidence_metadata`). Unmatched lines are
  diagnostics, not crashes.
- [x] Multiple sources in one experiment (`inputs:` with a profile per input),
  each event tagged with its source; contract tests for a two-source fixture.

**Exit:** a two-source synthetic fixture normalizes with zero diagnostics
and every event links to its raw line.

### 4.2 · 5G core and RAN logs · `lead` · done
- [x] Log profiles for Open5GS AMF/SMF and an OCUDU (srsRAN-derived) gNB.
  Fixtures must be real captures with source and version recorded, or
  clearly labeled synthetic fixtures written from the documented format.
- [x] Join RAN and core events on NGAP UE id pairs; fall back to a recorded
  time window, marking those joins as lower confidence.
- [x] Contracts: registration completes, PDU session setup completes, RRC
  state transitions are legal; faults: a dropped, a delayed, and a reordered
  message in the fixture, each caught by its target contract.

**Exit:** RTL and wireless run on the same unmodified core, and fixture
results are labeled as fixtures.

## Phase 7 · Fault localization

### 7.1 · Ranked hypotheses in every bundle · `lead` · done
- [x] A system graph from the adapter description (subsystems, interfaces)
  and contract boundaries; a deterministic localizer that ranks subsystems
  (with the most likely interface) from failing contracts, weighted by
  severity and by how early each first failed, including components that log
  nothing; counter-evidence from passing contracts; probabilities, the
  top-two margin, and an explicit ambiguous status. Written to
  `localization.json` and summarized in `report.md`. The localizer never
  reads fault labels.
- [x] `ground_truth: {subsystem, interface}` for experiments whose fault is in
  the inputs (analyze mode), recorded in the manifest next to fault labels.
- [x] `crosslayer localize` prints a bundle's hypotheses; `--score` computes
  top-1 and top-3 accuracy (subsystem and interface) over suite outputs
  against fault labels and ground truth.

**Exit:** top-1 and top-3 accuracy measured over every labeled fault
experiment (reference, RV32I on both cores, textlog, 5G), with each miss
explained, including faults whose symptoms appear downstream of their cause.

### 7.2 · Localization in the HTML report · `routine` · `feature/localization-report`
- [x] A step in `report.html` that shows the ranked hypotheses with their
  evidence and counter-evidence, linking evidence to contract results;
  `crosslayer show` prints the top three. Generic: driven only by
  `localization.json`.

**Exit:** a bundle without `localization.json` (older bundles) still renders.

## Phase 3 · Pipelined processor verification

### 3.7 · A pipelined core and its hazards · `lead` · `feature/pipelined-core`
- [ ] Choose and vendor a permissively licensed, Icarus-compatible RV32I core
  with a real pipeline (forwarding, stalls, flushes); record the candidates
  considered and why. Profile it, with a retirement monitor if it has no
  RVFI port, and pass all 10 contracts.
- [ ] Hazard-aware random programs (read-after-write at distances 1-3,
  load-use, branch after a dependent compute) and hazard coverage bins in the
  adapter's stats, reported per sweep.
- [ ] Pipeline mutants: a disabled forwarding path, a missing load-use stall,
  a missing branch flush; each caught at the instruction the reference
  model predicts, and localized.

**Exit:** three cores; every pipeline mutant caught by a hazard sweep; hazard
bins never hit are listed by the sweep report.

## Phase 8 · Guided follow-up experiments

### 8.1 · Diagnostic actions and the planner · `lead` · `feature/guided-experiments`
- [ ] Adapters may declare diagnostic actions: experiment variants
  (parameter or fault overrides, a component swapped for a reference) with
  the outcome each hypothesis predicts. The sensor pipeline and RV32I
  declare actions that separate producer from consumer faults.
- [ ] A planner picks the action with the highest expected information gain
  under the current localization probabilities, runs it, updates the
  probabilities, and stops when confident or out of budget;
  `crosslayer diagnose BUNDLE [--budget N]` writes the trail to the bundle.

**Exit:** over every labeled fault experiment, the diagnosis is right after
fewer experiments than running every action, measured; the Phase 7 misses
are re-scored.

## Phase 11 · Results store and regression analytics

Entry: a CI run now writes over 100 bundles (five suites with 20-seed
sweeps), and scoring and comparison walk directories to find them.

### 11.1 · Run index · `routine` · `feature/run-index`
- [ ] `crosslayer index add PATH...` records bundles in a SQLite file (stdlib
  `sqlite3`; default `runs/index.sqlite`): run, experiment, config hash,
  seed, verdict, timings, every contract result with first-violation time,
  fault labels and ground truth, and top localization hypotheses. Adding the
  same bundle twice is a no-op; the schema is versioned.
- [ ] `crosslayer history CONTRACT_ID [--experiment NAME] [--json]` lists that
  contract's verdicts over time; `crosslayer index runs` lists runs with
  filters (experiment, verdict, since).

### 11.2 · Regressions and flaky contracts · `routine` · `feature/regressions`
- [ ] `crosslayer regressions` compares the latest run of each experiment
  with its previous runs: verdict flips, newly failing contracts, and stage
  timing or threshold-attribute changes beyond a stated tolerance, each
  linked to the first run where it appeared.
- [ ] Flaky contracts: verdicts that differ between runs with the same config
  hash, reported with counts; seed sweeps are compared per seed.

### 11.3 · Dashboard · `routine` · `feature/results-dashboard`
- [ ] `crosslayer dashboard` writes one self-contained HTML page from the
  index: suite health over time, per-owner failing contracts, flaky list,
  localization accuracy over time. No network resources, readable without
  JavaScript, like `report.html`.

**Exit:** any contract's history is queryable, and a regression links to the
first run where it appeared.
