# Recorded traces

Normalized event streams copied from real runs of the reference simulator,
used by `../contract_tests.yaml`. Regenerate them when the simulator or the
adapter's normalization changes.

| File | Experiment | Result when recorded |
|---|---|---|
| `baseline_40.jsonl` | seed 7, `packets: 40`, `buffer_capacity: 8`, no faults | all 8 contracts PASS |
| `silent_drop_40.jsonl` | seed 7, `packets: 40`, `buffer_capacity: 1`, fault `silent_drop` | `rx_accounted` FAIL (3 violations, 5 unresolved), `counter_conservation` FAIL |

Recorded 2026-11-02 with simulator `sim.py sha256:f42b9279e08e`, adapter
`sensor_pipeline` 0.1.0. Each file is the `normalized_events.jsonl` of the
run bundle.
