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
plus every contract boundary (`source -> destination`) as edges.

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
manifest (`localization`), and a section in `report.md`.

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

All 26 labeled fault experiments (reference, RV32I on PicoRV32 and Quark,
textlog, 5G), run with `crosslayer suite -j 8` on Windows, Python 3.11, and
scored with `crosslayer localize --score`:

| Metric | Result |
|---|---|
| Top-1 subsystem | 19/26 (73%) |
| Top-3 subsystem | 26/26 (100%) |
| Top-1 subsystem and interface (labels with an interface) | 10/19 |
| Confident calls that were right | 1/1 (the other 25 runs are labeled ambiguous) |

Every miss, and why:

| Experiment | Truth (rank) | Why |
|---|---|---|
| sensor `duplicate_completion`, `slow_consumer` | `proc` (2) | The consumer is at fault, but the failing contracts' boundaries make it the destination; the source prior wins. |
| 5G `delayed_pdu_session` | `ngap` (2) | Same pattern: the gNB completes late, on a single boundary. |
| textlog `slow_reply` | `server` (2) | Same pattern. |
| RV32I `jal_imm_bit11` (both cores) | `decoder` (3 and 2) | A wrong JAL target is explained as well by the ALU; and the bus error from the wrong fetch is logged a few cycles *before* the JAL retires, so a catch-all contract looks earliest. |
| RV32I `regfile_wrong_rd` via commit log | `regfile` (3) | A commit log has no operand reads, so the first visible symptom is a wrong store, two components downstream of the cause. With RVFI the same fault ranks first. |

Four of the seven misses are a faulty *destination* on a single boundary:
the evidence cannot tell a bad producer from a bad consumer. Separating them
takes a follow-up experiment, which is Phase 8.

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
* 26 labeled runs are a small set; the weights are documented defaults, not
  fitted. Phase 9 is where weights are learned and tested on held-out data.
