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

### 3.3 · Trace sources beyond RVFI · `lead` · `feature/rtl-trace-sources`
- [x] Move line parsing out of `_TraceParser` into `traces/rvfi.py` behind a
  `TraceSource` interface that yields typed records (boot, retire, output,
  bus error, halt, timeout). No behavior change.
- [ ] `trace: commit-log`: Spike-style commit lines (`core 0: 3 0x… (0x…) x5 0x…`,
  `mem 0x… 0x…`). Fields the format lacks stay absent, so the contracts that
  need them are INCONCLUSIVE, never PASS.
- [ ] `trace: vcd-signals`: the profile maps RVFI-equivalent fields to
  waveform signals plus a `valid` signal and clock; a standard-library VCD
  reader samples them. Works for any core that can dump a waveform.

**Exit:** the PicoRV32 suite passes unchanged through each of the three
sources (RVFI text, a commit log derived from it, and its VCD).

### 3.4 · Second core · `lead` · `feature/rtl-second-core`
- [ ] Vendor a second permissively licensed Verilog-2005 RV32I core that
  Icarus can compile (check the license and record the upstream commit),
  with a profile that uses a non-RVFI trace source.
- [ ] Port the mutants that apply to it and add `experiments/rv32i-<core>/`.

**Exit:** two cores, two trace formats, no changes under `crosslayer/core`.

### 3.5 · Seed sweeps and ISA coverage · `routine` · `feature/rtl-coverage`
- [ ] `crosslayer suite` entries can repeat an experiment over a seed list
  (`seeds: [1, 2, 3]` or `seeds: {count: N}`); each seed gets its own bundle
  and the suite reports per-seed verdicts.
- [ ] The rv32i adapter records ISA coverage in `normalize` stats: retired
  count per mnemonic, taken and not-taken per branch kind, and each load and
  store width and alignment; shown in `report.md`.
- [ ] A `coverage` contract kind or threshold contract that fails when a
  listed mnemonic never retires (for example the self-test must retire every
  RV32I mnemonic the assembler supports).

**Exit:** a 20-seed random-program sweep passes on PicoRV32, and the report
lists any RV32I mnemonic the sweep never retired.

## Phase 4 · Wireless log analysis

### 4.1 · Declarative log parser · `routine` · `feature/log-profiles`
- [ ] A `textlog` integration whose YAML log profile maps line regexes to
  event types and attributes, with a timestamp format, a time zone, and
  per-source clock metadata (`confidence_metadata`). Unmatched lines are
  diagnostics, not crashes.
- [ ] Multiple sources in one experiment (`inputs:` with a profile per input),
  each event tagged with its source; contract tests for a two-source fixture.

**Exit:** a two-source synthetic fixture normalizes with zero diagnostics
and every event links to its raw line.

### 4.2 · 5G core and RAN logs · `routine` · `feature/5g-log-analysis`
- [ ] Log profiles for Open5GS AMF/SMF and an OCUDU (srsRAN-derived) gNB.
  Fixtures must be real captures with source and version recorded, or
  clearly labeled synthetic fixtures written from the documented format.
- [ ] Join RAN and core events on NGAP UE id pairs; fall back to a recorded
  time window, marking those joins as lower confidence.
- [ ] Contracts: registration completes, PDU session setup completes, RRC
  state transitions are legal; faults: a dropped, a delayed, and a reordered
  message in the fixture, each caught by its target contract.

**Exit:** RTL and wireless run on the same unmodified core, and fixture
results are labeled as fixtures.
