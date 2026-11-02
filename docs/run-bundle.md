# Run bundle format (version 1)

Every run writes one directory, `<output>/<run_id>/`, where
`run_id = <UTC start, YYYYMMDDTHHMMSSZ>-<first 8 hex of config hash>[-n]`.

| File | Contents |
|---|---|
| `manifest.json` | Verdict, mode, timings, config hash, experiment, adapter, execution record, injected faults (with ground-truth targets), event count and fingerprint, contract tallies, warnings, errors, and a `files` index with SHA-256 for every other file. Written last. |
| `configuration.yaml` | Resolved experiment: **every evaluated contract inlined** (adapter defaults included), absolute paths, `include_adapter_contracts: false`. Replaying it checks exactly the same contracts. |
| `versions.json` | CrossLayer version and git revision (with dirty flag), Python and OS, adapter description (system version, subsystems, interfaces, external dependencies), capabilities. |
| `raw_artifacts/` | Untouched output from the system under test. Evidence references point here (`raw_artifacts/sim.log#L315`). |
| `normalized_events.jsonl` | One canonical-JSON event per line, time-ordered. This is exactly what the contracts saw. |
| `normalization.json` | Adapter stats and parse diagnostics (raw lines that could not be normalized). |
| `contracts.json` | Each contract's origin, original definition, and full result: verdict, rule verdict, summary, stats, missing evidence, and findings with evidence. |
| `violations.json` | Only the FAIL results, same structure. |
| `report.md` | Human-readable summary: contract table, owners needing attention, violations ordered by first observation, inconclusive contracts. |
| `junit.xml` | One testcase per contract. FAIL maps to `failure`, ERROR to `error`, INCONCLUSIVE and SKIPPED to `skipped` with the verdict in the message. |
| `replay.sh` | Re-runs `configuration.yaml`. Its header lists expected verdict, event fingerprint, and the external dependencies that are not bundled. |
| `work/` | Scratch space for build outputs. Not indexed. |

## Hashes and fingerprints

* **Config hash**: SHA-256 of the canonical JSON of the effective experiment,
  with absolute paths removed and input files replaced by their content
  hashes. Same experiment on two machines gives the same hash. Changing a
  seed, a parameter, a fault, or any contract changes it.
* **Event fingerprint**: SHA-256 over the canonical normalized events with
  `run_id` removed. Two runs of a deterministic system with the same config
  should have equal fingerprints. `crosslayer compare` reports this.
* **File index**: `crosslayer verify <bundle>` recomputes every file hash
  listed in the manifest and reports missing or modified files.

## Planned additions

`dependency_graph.json` and `diagnosis.json` (roadmap Phase 7), and an HTML
report with cross-layer timelines (Phase 13).
