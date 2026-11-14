# Run bundle format (version 1)

Every run writes `<output>/<run_id>/`, where
`run_id = <UTC start YYYYMMDDTHHMMSSZ>-<first 8 hex of config hash>[-n]`.

| File | Contents |
|---|---|
| `manifest.json` | Verdict, timings, config hash, adapter, faults (with true targets), event count and fingerprint, contract tallies, errors, and SHA-256 of every other file. Written last. |
| `configuration.yaml` | The resolved experiment with **every contract inlined**, so a replay checks exactly the same contracts. |
| `versions.json` | CrossLayer version and git revision, Python, OS, adapter description, capabilities. |
| `raw_artifacts/` | Untouched output of the system. Evidence points here (`raw_artifacts/sim.log#L315`). |
| `normalized_events.jsonl` | One event per line, time-ordered: exactly what the contracts saw. |
| `normalization.json` | Adapter stats and lines that could not be parsed. |
| `contracts.json` | Each contract's definition and full result, with findings and evidence. |
| `violations.json` | FAIL results only. |
| `timings.json` | Seconds per runner stage: plan, build, execute, collect, normalize, evaluate, write (`write` covers events, normalization, contracts, violations). Stages that did not run are 0. Not part of the fingerprint. |
| `report.md` | Summary: contract table, stage timings, owners needing attention, violations in order, inconclusive contracts. |
| `junit.xml` | One test case per contract. FAIL → `failure`, ERROR → `error`, INCONCLUSIVE, SKIPPED, and warn-only FAIL → `skipped`. Each case has `severity`, `boundary` (`source -> destination`) and `version` properties. |
| `replay.sh` | POSIX shell version of `crosslayer replay`; its header lists the expected verdict, fingerprint, and unbundled dependencies. |
| `work/` | Scratch space. Not indexed. |

## Hashes

* **Config hash:** SHA-256 of the effective experiment, without absolute
  paths and with input files replaced by content hashes. The same experiment
  on two machines gives the same hash; any change to seed, parameters,
  faults, or contracts changes it.
* **Event fingerprint:** SHA-256 of the normalized events without `run_id`.
  Same config on a deterministic system gives the same fingerprint
  (`crosslayer compare` checks this).
* **Replay:** `crosslayer replay <bundle> [--out DIR]` re-runs
  `configuration.yaml` on any OS, then compares config hash and event
  fingerprint with the original. Exit 0 if both match, 1 otherwise.
* **Summary:** `crosslayer show <bundle> [--limit N] [--json]` prints the verdict, per-contract table, and first violations from `manifest.json` and `contracts.json`. It runs nothing.
* **File index:** `crosslayer verify <bundle> [--json]` reports missing, modified, or
  unexpected files, each as `missing: PATH`, `modified: PATH` or `unexpected: PATH`.
  `--json` prints `intact` and `problems`. Exit 0 if intact, 1 if any problem, 4 if the
  bundle or its manifest cannot be read.

Planned: `dependency_graph.json` and `diagnosis.json` (Phase 7), and an HTML
report (Phase 13).
