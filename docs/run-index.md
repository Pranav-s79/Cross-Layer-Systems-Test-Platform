# Run index

Bundles stay the source of truth. `crosslayer index` copies what cross-run
questions need into one SQLite file (standard library `sqlite3`), so nothing
has to walk directories.

```bash
crosslayer suite experiments/reference/suite.yaml --out runs
crosslayer index add runs                     # bundles or directories; default runs/index.sqlite
crosslayer index runs --verdict fail --since 2026-11-01
crosslayer history pipeline.rx_accounted --experiment sensor-pipeline-silent-drop
```

| Command | Does |
|---|---|
| `index add PATH... [--db FILE] [--json]` | Indexes each bundle, or every bundle under a directory (found by `manifest.json`). A run id that is already indexed is left unchanged. Every bundle is read before any row is written, so one unreadable bundle adds nothing. Follow-up runs inside `<bundle>.diagnosis/` are skipped. |
| `index runs [--experiment N] [--verdict V] [--since D] [--limit N] [--json]` | Runs, newest first. `--since` takes `YYYY-MM-DD` or an ISO date-time (UTC unless it names a zone). |
| `history CONTRACT_ID [--experiment N] [--limit N] [--json]` | That contract's verdict in each run, newest first, with the first-violation time of failures. An id the index has never seen is a config error (exit 4); a filter that matches nothing is an empty list. |

`index runs` and `history` never create the database; without one they exit 4.

## What is recorded

| Table | One row per | Columns |
|---|---|---|
| `runs` | run | run id, bundle path, experiment, adapter, mode, config hash, seed, verdict, start and finish time, event count and fingerprint, localization status, fault labels and ground truth (JSON, as in the manifest), stage timings (JSON) |
| `contract_results` | run and contract | contract id, version, owner, severity, verdict, first violation (ns), summary |
| `hypotheses` | run and rank | the top three: subsystem, interface, probability |

Values are read from `manifest.json`, `contracts.json`, `timings.json`, and
`localization.json` as the run wrote them. `timings.json` is optional (older
bundles); `contracts.json` and the manifest's run id, experiment name, config
hash, and verdict are required, and a missing one is a config error naming it.

## Versioning

`PRAGMA user_version` holds the schema version (currently 1). A database with
another version, or a SQLite file that is not an index, is refused with the
version found; there are no silent migrations. Index the bundles again into a
new file instead.

## Limits

The index records bundles by path and does not copy them; moving a bundle
leaves a stale `bundle_path`. Rows are never updated or removed, so a bundle
edited after indexing keeps its old row. Regression detection and flaky
contracts come in the next milestone steps.
