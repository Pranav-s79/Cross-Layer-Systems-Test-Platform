# Guided follow-up experiments

[Fault localization](fault-localization.md) ranks where a failure most
likely started from one run's evidence. Some failures cannot be told apart
that way: when a consumer is at fault but the failing contract's boundary
names the producer as its source, or when the earliest symptom is observed
downstream of its cause. A person would run another experiment, such as
swapping in a known-good part or slowing things down. `crosslayer diagnose`
does that: it chooses the follow-up run that best separates the current
suspects, runs it, updates the ranking, and repeats until one suspect is
clear or nothing left would change the answer.

```sh
crosslayer diagnose runs/<bundle>                 # diagnose one failing run
crosslayer diagnose runs/<bundle> --budget 2      # at most two follow-up runs
crosslayer diagnose runs                          # every failing labeled run below runs/, with a summary
crosslayer diagnose runs --all                    # run every action instead of choosing (for comparison)
crosslayer diagnose runs --min-top1 0.9           # exit 1 if accuracy after diagnosis is below 0.9
```

The diagnosis is written next to the bundle, in `<bundle>.diagnosis/`:
`diagnosis.json` (the trail), `report.md`, and the follow-up runs under
`runs/`. The original bundle is never changed, so `crosslayer verify` still
passes; the trail records the bundle's run id and manifest SHA-256.
`crosslayer localize --score` skips follow-up runs.

## Diagnostic actions

An adapter declares the follow-ups that make sense for one configuration
(`SystemAdapter.diagnostic_actions(parameters, base_dir)`, none by default).
Each is a set of parameter overrides plus an outcome model, which says what
each suspect predicts:

| Model | The variant... | Outcome | Likelihood |
|---|---|---|---|
| `clears` | takes away what a fault in some subsystems needs to show: that part swapped for a reference implementation, or the condition that exercises it removed | does the failure reproduce? (a contract fails) | `clears[s]` = P(no failure \| fault in `s`); a `subsystem/interface` key applies when the localization blamed that interface |
| `observes` | records more, for example operand values | the variant's own top suspect | right with probability `observes`, otherwise any other suspect |

A clearing action can also list contracts it changes by construction
(`ignores`, such as a CPI budget when the variant adds instructions). They
do not count toward the outcome, and the action does not apply when every
original failure is one of them.

| Adapter | Action | When offered | Model |
|---|---|---|---|
| sensor pipeline | `reference_sensor_rx`, `reference_ring_buffer`, `reference_proc` | always (each component not already swapped) | clears that component (1.0) |
| rv32i | `space_instructions` | pipelined cores | NOPs after every instruction (`nop_spacing: stages - 1`), so nothing is forwarded or stalled; clears `hazard/bypass` (1.0), not `hazard/pipeline_control` (taken branches still flush), `hazard` 0.75 if no interface; ignores `rv32i.cpi_budget` |
| rv32i | `observe_operands` | traces without operand values | re-run traced as `rvfi` (or another format with operands); observes 0.8, the measured top-1 rate with operands rounded down |

The sensor pipeline's reference components are its simulator's own models
without their fault hooks (`sim.py --reference proc`), standing in for
swapping in a known-good board or module. `nop_spacing` is an assembler
option (`asm.assemble(source, spacing=N)`): every instruction word is
followed by N NOPs, labels account for them, and a literal `jalr rd, imm(rs)`
right after `auipc rs, 0` has its PC-relative offset scaled. The spaced
program is a different program, and the reference model runs it too.

## Method (`expected-information-gain-v1`)

1. The prior is the localization's probability over subsystems.
2. For each action not yet run, compute the expected information gain: the
   entropy of the current distribution minus the expected entropy after
   seeing the outcome, using the action's likelihoods (clipped to
   [0.02, 0.98], so one surprising outcome lowers a suspect but never rules
   it out).
3. Run the action with the highest gain (ties go to declaration order), as a
   variant of the bundle's own `configuration.yaml`, and update the
   distribution by Bayes' rule. An ERROR run says nothing and changes
   nothing.
4. Stop when a suspect reaches the threshold (0.9), the budget is spent, or
   no remaining action is expected to gain 0.01 bits.

`--all` skips step 2 and runs every action in order. It is the baseline the
planner is measured against.

## Measured

Every failing labeled run of the six suites (76 runs, 55 of them with at
least one applicable action), run on Windows with Python 3.11:

| | Top-1 subsystem | Follow-up runs |
|---|---|---|
| Localization alone | 69/76 | 0 |
| Diagnosis, planner | **73/76** | **46** |
| Diagnosis, every action | 73/76 | 65 |

The planner reaches the same accuracy as running every action with 29% fewer
experiments. All 38 of its confident calls were right. By suite:

| Runs | Before | After | Follow-up runs (planner / every action) |
|---|---|---|---|
| Sensor pipeline (5 faults) | 3/5 | 5/5 | 7 / 15 |
| RV32I pipeline fault experiments (10) | 9/10 | 10/10 | 8 / 9 |
| RV32I pipeline hazard sweeps (40 seeds) | 40/40 | 40/40 | 30 / 40 |
| RV32I commit-log `regfile_wrong_rd` | rank 3 | 1 | 1 / 1 |

How the [Phase 7 misses](fault-localization.md#measured-accuracy) fare:

| Run | Localization | After diagnosis |
|---|---|---|
| sensor `duplicate_completion` | `sensor_rx` 0.48, `proc` 0.34 | `proc` 0.99 after 2 runs: the reference RX still fails, the reference consumer passes |
| sensor `slow_consumer` | `sensor_rx` 0.40, `proc` 0.31 | `proc` confident |
| RV32I `regfile_wrong_rd` via commit log | `lsu` 0.29, `regfile` 0.16 | `regfile` 0.84 after 1 run: traced with operands, the run names the register file |
| pipeline `no_load_use_stall` | `lsu` 0.31, `hazard` 0.16 | `hazard` confident after 1 run: spaced out, the failure is gone |
| 5G `delayed_pdu_session`, textlog `slow_reply` | wrong (rank 2) | unchanged: analyzed logs cannot be re-run |
| PicoRV32 `jal_imm_bit11` | wrong (rank 2) | unchanged: no action separates the decoder from the rest of the core |

Two measured mistakes shaped the method. A single "spacing clears hazard
faults 75% of the time" sent the 11 branch-flush runs to `fetch` (one to `lsu`), because a
flush fault still fails when spaced out. Keying the prediction on the
interface the localization blamed fixed that, and the planner now skips the
action there. Spacing also "cleared" the pipeline's slow-memory fault by
changing its CPI profile; `ignores` fixed that.

## Limits

* An action's likelihoods are declared by its adapter, not learned. Phase 9
  is where they would be fitted from labeled runs.
* Actions are tried at most once each, because the runs are deterministic. A
  flaky system would need repeats and a noise model per action.
* Analyzed runs (logs given as `inputs`) cannot be re-run, so they get no
  follow-ups.
