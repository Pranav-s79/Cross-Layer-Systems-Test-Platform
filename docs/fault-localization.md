# Fault localization

When a run fails, every bundle ranks **where the fault most likely started**:
which subsystem, through which interface, with the evidence for and against
each candidate and an honest statement of how sure it is. It is a ranking
for a human to act on, not a proof. The localizer never reads fault labels;
they are used only to score it.

```sh
crosslayer localize runs/<bundle>                 # ranked hypotheses of one run
crosslayer localize runs --score                  # accuracy over every labeled run below runs/
crosslayer localize runs --score --min-top3 1.0   # exit 1 if accuracy regresses (CI does this)
```

## Method (`boundary-evidence-v1`)

The system graph has the adapter's subsystems as nodes and its interfaces
plus every evaluated contract boundary (`source -> destination`) as edges.
The structure is the one for the run's configuration
(`SystemAdapter.describe_for`, which defaults to `describe()`): a
multi-cycle RV32I core has no `hazard` subsystem, so it is never blamed. A
contract SKIPPED by its precondition adds no edge (its boundary does not
exist here), and only the structure's subsystems are ranked.

| Step | Rule |
|---|---|
| Weigh each failing contract | `severity` (critical 1.0, major 0.7, minor 0.4) x `0.5^r`, where `r` counts the distinct first-violation times before its own. Later failures are often consequences. |
| Blame its boundary | The source gets the full weight, the destination 0.6. |
| Reach silent components | A subsystem `k` edges upstream of the source gets `0.5^k / 2`, so a component that logs nothing can still be blamed. |
| Counter-evidence | Each passing contract multiplies the score of its source by 0.85 and of its destination by 0.9, down to 0.25. |
| Interface | The interface carrying data in the failing contract's direction (the opposite direction only if none exists), weighted the same way. |
| Probability | Scores normalized to sum to 1. |
| Status | **ambiguous** if the top two are direct endpoints of exactly the same failing contracts (only priors separate them) or their margin is below 0.25; else **confident**; **no_failures** if nothing failed. The reason is stated. |

Output: `localization.json` in every bundle, a top-three summary in the
manifest (`localization`), a section in `report.md`, a "Locate the fault" step in
`report.html` (ranked hypotheses; each evidence and counter-evidence contract
links to its row in the contracts step), and the top three in `crosslayer show`
(`localization` in `--json`). Bundles without `localization.json` render the
report with five steps and `show` omits the section.

## Ground truth

Injected faults carry their true location (`target_subsystem`,
`target_interface`). A fault that lives in the inputs (a log fixture) has no
injected fault, so the experiment states it:

```yaml
ground_truth: {subsystem: amf, note: "the AMF never logs Registration complete"}
```

It must name a declared subsystem (and interface, if given), is recorded in
the manifest, and is not part of the config hash: it labels a run, it does
not change it.

## Measured accuracy

All 76 labeled fault runs: 36 fault experiments (reference, RV32I on
PicoRV32, Quark, and the pipelined core, textlog, 5G) and the 40 seeds of
the pipeline's hazard-mutant sweeps, run with `crosslayer suite -j auto` on
Windows, Python 3.11, and scored with `crosslayer localize --score`:

| Metric | Result |
|---|---|
| Top-1 subsystem | 69/76 (91%): 29/36 fault experiments, 40/40 sweep seeds |
| Top-3 subsystem | 76/76 (100%) |
| Top-1 subsystem and interface (labels with an interface) | 49/69 |
| Confident calls that were right | 4/4 (the other 72 runs are labeled ambiguous) |

Before milestone 3.7 (26 experiments, no pipelined core): top-1 19/26, top-3
26/26, interface 10/19, confident 1/1.

Every miss, and why:

| Experiment | Truth (rank) | Why |
|---|---|---|
| sensor `duplicate_completion`, `slow_consumer` | `proc` (2) | The consumer is at fault, but the failing contracts' boundaries make it the destination; the source prior wins. |
| 5G `delayed_pdu_session` | `ngap` (2) | Same pattern: the gNB completes late, on a single boundary. |
| textlog `slow_reply` | `server` (2) | Same pattern. |
| RV32I `jal_imm_bit11` (PicoRV32) | `decoder` (2) | The bus error from fetching at the wrong target is logged a few cycles *before* the JAL retires, so `no_bus_errors` (source `core`) looks earliest. On Quark and the pipeline it ranks first. |
| RV32I `regfile_wrong_rd` via commit log | `regfile` (3) | A commit log has no operand reads, so the first visible symptom is a wrong store, two components downstream of the cause. With RVFI the same fault ranks first. |
| pipeline `no_load_use_stall` | `hazard` (2) | The first symptom is a store of the bad value to the output port. The port sees it in the memory stage, one cycle before the store retires and its stale operand is reported, so `program_output` (source `lsu`) looks earliest. |

Four of the seven misses are a faulty *destination* on a single boundary:
the evidence cannot tell a bad producer from a bad consumer. Two are
observation latency: the same instruction's effects are recorded at
different points (bus, retirement), and the earliest record is a symptom.
Separating both kinds takes a follow-up experiment:
[guided experiments](guided-experiments.md) (Phase 8) fix four of the
seven, and the other three cannot be re-run or have no separating action.

## RV32I contracts

Milestone 3.7 added a pipelined core and four hazard mutants. With the
contracts as they were, all four ranked `hazard` second or worse: a stale
forwarded operand made the consumer's result wrong, so
`lockstep_writeback` (`alu -> regfile`) failed at the same instant as the
operand check and the ALU outscored the bypass. The fix makes each check
local, comparing a unit's output with what the reference semantics give
for the inputs it actually received
([RTL verification](rtl-verification.md#local-and-end-to-end-checks)).
Each step was measured on the 31 RV32I and reference fault experiments
(the 21 above plus the pipeline's 10):

| Change | Top-1 | Top-3 | Decision |
|---|---|---|---|
| Pipelined core, hazard contracts (`forwarded_operands`, `redirect_followed`), contracts otherwise unchanged | 20/31 | 29/31 | the hazard mutants rank 2-4 |
| Results and data accesses checked against the operands read; operands compared only for the same instruction | 19/31 | 31/31 | not enough: `regfile_wrong_rd` dropped to 2 on both multi-cycle cores |
| Control flow split into a local next-PC check (`lockstep_control_flow`) and an end-to-end path check (`reference_path`, source `core`); structure per configuration | 26/31 | 31/31 | kept: a branch steered off the path by a bad register no longer blames the ALU |
| Loaded values moved to `load_data` (`lsu -> regfile`) | 26/31; hazard sweeps 33/40 -> 40/40 | 31/31 | kept: a load that reads memory an earlier bad store changed no longer blames the ALU |

No experiment's rank got worse. Quark's `jal_imm_bit11` went from 2 to 1 and
PicoRV32's from 3 to 2.

## What was tried and rejected

Each alternative was measured on the 21 injected faults and kept only if it
helped without breaking other cases.

| Change | Effect | Decision |
|---|---|---|
| Match interfaces in the contract's direction | interface top-1 6 -> 10 of 21 runs | kept (it was a bug) |
| Add RV32I's real data paths (`regfile -> lsu`, `lsu -> regfile`, `fetch -> decoder`, `decoder -> alu`) to its description | top-3 19 -> 21 of 21 | kept (the graph was missing paths) |
| Blame the endpoint whose events show the violation | top-1 16 -> 12 | rejected: the component that *observes* a violation is usually the consumer, not the cause |
| Treat violations within 1% of the trace span as simultaneous | top-1 16 -> 15, interface 10 -> 8 | rejected: it merges causally ordered failures microseconds apart |
| Ambiguous whenever the top two share any failing contracts | confident calls 3/4 right | replaced by the direct-endpoint rule (1/1 right) |

## Limits

* A ranking from contract outcomes and graph structure; it does not inspect
  signal values or replay the run.
* The graph is only as good as the adapter's description: a missing data
  path hides the true cause (see RV32I above).
* 36 fault experiments (plus 40 sweep seeds of four of them) are a small
  set; the weights are documented defaults, not fitted. Phase 9 is where
  weights are learned and tested on held-out data.
