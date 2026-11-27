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

`index runs`, `history`, and `regressions` never create the database; without one they exit 4.

## Regressions

```bash
crosslayer regressions --db runs/index.sqlite [--experiment N] [--json]
```

Runs of one experiment and seed form a series ordered by start time (a seed
sweep is compared per seed). For each series the latest run is compared with the
earlier ones; with one run there is nothing to compare. Every finding names the
first run where it appeared: the oldest run of the unbroken streak that ends at
the latest run. The baseline is the run just before that streak, so a failure
that has lasted five runs is still reported until a run passes.

| Finding | Reported when | Class |
|---|---|---|
| `verdict` | the run verdict differs from the last different one | regression if it became `fail` or `error` from something else |
| `contract` | a contract's verdict differs likewise, or a contract new to the series is `fail`/`error` | same rule |
| `first_violation` | a contract fails in the latest and previous run and its first-violation time moved by more than `--violation-tolerance` (default 0.1, relative) | change |
| `timing` | a stage took more than `--timing-tolerance` (default 0.5) over the median of earlier runs **and** at least `--min-seconds` (default 0.05) more | change |

Exit code 1 only when a regression exists; changes (recoveries, moves between
`fail` and `error`, timing, first-violation shifts) are listed but exit 0.
Timings are wall-clock seconds and vary between machines, so they never fail a
run. A series whose config hash differs from the previous run's is marked
`[config changed]`: a difference may come from the configuration, not the code.
The index records no threshold attributes of contract expressions, so
first-violation time is the attribute compared.

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
edited after indexing keeps its old row. Flaky contracts (same config hash,
different verdicts) come in the next milestone step.
