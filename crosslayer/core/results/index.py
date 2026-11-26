"""A SQLite index over run bundles.

Bundles stay the source of truth; the index copies what is needed to answer
questions across runs without walking directories: one row per run, one per
contract result, and one per top localization hypothesis. Rows are read from
``manifest.json``, ``contracts.json``, ``timings.json`` and
``localization.json`` exactly as the run wrote them. Nothing here reads fault
labels to decide anything; labels and ground truth are stored as recorded.

Runs are keyed by ``run_id``. Adding a bundle whose run id is already indexed
changes nothing. ``PRAGMA user_version`` holds the schema version; a database
written by another version is refused rather than migrated silently.
"""

from __future__ import annotations

import datetime as dt
import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from crosslayer.core.artifacts.bundle import MANIFEST, load_json, load_manifest
from crosslayer.core.errors import ConfigError

DEFAULT_DB = Path("runs") / "index.sqlite"
SCHEMA_VERSION = 1
TOP_HYPOTHESES = 3

_SCHEMA = """
CREATE TABLE runs (
    run_id TEXT PRIMARY KEY,
    bundle_path TEXT NOT NULL,
    experiment TEXT NOT NULL,
    adapter TEXT,
    mode TEXT,
    config_hash TEXT NOT NULL,
    seed INTEGER,
    verdict TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT,
    event_count INTEGER,
    fingerprint TEXT,
    localization_status TEXT,
    faults TEXT NOT NULL,
    ground_truth TEXT,
    timings TEXT
);
CREATE INDEX runs_experiment ON runs (experiment, started_at);
CREATE TABLE contract_results (
    run_id TEXT NOT NULL REFERENCES runs (run_id),
    contract_id TEXT NOT NULL,
    version INTEGER,
    owner TEXT,
    severity TEXT,
    verdict TEXT NOT NULL,
    first_violation_ns INTEGER,
    summary TEXT,
    PRIMARY KEY (run_id, contract_id)
);
CREATE INDEX contract_results_id ON contract_results (contract_id);
CREATE TABLE hypotheses (
    run_id TEXT NOT NULL REFERENCES runs (run_id),
    rank INTEGER NOT NULL,
    subsystem TEXT NOT NULL,
    interface TEXT,
    probability REAL,
    PRIMARY KEY (run_id, rank)
);
"""


@dataclass(frozen=True)
class AddOutcome:
    bundle: Path
    run_id: str
    added: bool


def parse_since(text: str) -> str:
    """Normalize ``YYYY-MM-DD`` or an ISO date-time (UTC if no zone) to the bundles' timestamp format."""
    try:
        moment = dt.datetime.fromisoformat(text.strip().replace("Z", "+00:00"))
    except ValueError:
        raise ConfigError(f"--since {text!r}: expected YYYY-MM-DD or an ISO date-time such as 2026-11-01T12:00") from None
    if moment.tzinfo is not None:
        moment = moment.astimezone(dt.timezone.utc).replace(tzinfo=None)
    return moment.strftime("%Y-%m-%dT%H:%M:%S.") + f"{moment.microsecond // 1000:03d}Z"


def _find_bundles(path: Path) -> list[Path]:
    if not path.exists():
        raise ConfigError("no such file or directory", location=str(path))
    if (path / MANIFEST).is_file():
        return [path]
    if not path.is_dir():
        raise ConfigError(f"not a run bundle or a directory of bundles (no {MANIFEST})", location=str(path))
    from crosslayer.core.diagnosis.planner import in_diagnosis

    # Follow-up runs belong to their diagnosis, not to the experiment history.
    return sorted(m.parent for m in path.rglob(MANIFEST) if not in_diagnosis(m.parent))


def _json(value: Any) -> str | None:
    return None if value is None else json.dumps(value, sort_keys=True)


def _optional(bundle: Path, name: str) -> Any:
    try:
        return load_json(bundle, name) if (bundle / name).is_file() else None
    except (OSError, json.JSONDecodeError) as exc:
        raise ConfigError(f"unreadable {name}: {exc}", location=str(bundle / name)) from exc


def _require(manifest: dict[str, Any], key: str, bundle: Path) -> Any:
    if manifest.get(key) in (None, ""):
        raise ConfigError(f"manifest has no {key!r}", location=str(bundle / MANIFEST))
    return manifest[key]


class RunIndex:
    """Open (and with ``create=True``, make) an index database."""

    def __init__(self, path: Path, *, create: bool = False) -> None:
        self.path = Path(path)
        if not create and not self.path.is_file():
            raise ConfigError("no run index (create one with `crosslayer index add PATH...`)", location=str(self.path))
        if create:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self._db = sqlite3.connect(self.path)
            self._db.row_factory = sqlite3.Row
            self._init_schema()
        except sqlite3.DatabaseError as exc:
            raise ConfigError(f"not a usable index database: {exc}", location=str(self.path)) from exc

    def _init_schema(self) -> None:
        version = self._db.execute("PRAGMA user_version").fetchone()[0]
        has_tables = self._db.execute("SELECT count(*) FROM sqlite_master").fetchone()[0] > 0
        if version == SCHEMA_VERSION:
            return
        if version == 0 and not has_tables:
            with self._db:
                self._db.executescript(_SCHEMA)
                self._db.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            return
        raise ConfigError(f"index schema version {version}, this CrossLayer reads version {SCHEMA_VERSION}; "
                          "index the bundles into a new file", location=str(self.path))

    def close(self) -> None:
        self._db.close()

    def __enter__(self) -> "RunIndex":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- writing -------------------------------------------------------

    def add(self, paths: Iterable[Path]) -> list[AddOutcome]:
        """Index every bundle at or under ``paths``. All bundles are read before any row is written."""
        bundles: list[Path] = []
        for path in paths:
            found = _find_bundles(Path(path))
            if not found:
                raise ConfigError(f"no run bundles found (looked for {MANIFEST})", location=str(path))
            bundles.extend(found)
        records = [self._read(bundle) for bundle in dict.fromkeys(b.resolve() for b in bundles)]
        outcomes: list[AddOutcome] = []
        seen: set[str] = set()
        with self._db:
            for record in records:
                run_id = record["run"]["run_id"]
                exists = run_id in seen or self._db.execute(
                    "SELECT 1 FROM runs WHERE run_id = ?", (run_id,)).fetchone() is not None
                if not exists:
                    self._insert(record)
                seen.add(run_id)
                outcomes.append(AddOutcome(record["bundle"], run_id, added=not exists))
        return outcomes

    def _read(self, bundle: Path) -> dict[str, Any]:
        manifest = load_manifest(bundle)
        run_id = _require(manifest, "run_id", bundle)
        experiment = manifest.get("experiment") or {}
        if not experiment.get("name"):
            raise ConfigError("manifest has no experiment name", location=str(bundle / MANIFEST))
        localization = manifest.get("localization") or {}
        events = manifest.get("events") or {}
        timings = _optional(bundle, "timings.json")
        contracts = _optional(bundle, "contracts.json")
        if contracts is None:
            raise ConfigError("bundle has no contracts.json", location=str(bundle))
        results = []
        for entry in contracts.get("contracts", []):
            result = entry.get("result") or {}
            if not result.get("contract_id") or not result.get("verdict"):
                raise ConfigError("a contract result has no contract_id or verdict", location=str(bundle / "contracts.json"))
            results.append((result["contract_id"], result.get("version"), result.get("owner"), result.get("severity"),
                            result["verdict"], result.get("first_violation_ns"), result.get("summary")))
        run = {
            "run_id": run_id, "bundle_path": str(bundle), "experiment": experiment["name"],
            "adapter": (manifest.get("adapter") or {}).get("name"), "mode": manifest.get("mode"),
            "config_hash": _require(manifest, "config_hash", bundle), "seed": experiment.get("seed"),
            "verdict": _require(manifest, "verdict", bundle), "started_at": manifest.get("started_at"),
            "finished_at": manifest.get("finished_at"), "event_count": events.get("count"),
            "fingerprint": events.get("fingerprint"), "localization_status": localization.get("status"),
            "faults": _json(manifest.get("faults") or []), "ground_truth": _json(manifest.get("ground_truth")),
            "timings": _json((timings or {}).get("stages")),
        }
        hypotheses = [(rank, h.get("subsystem"), h.get("interface"), h.get("probability"))
                      for rank, h in enumerate((localization.get("top") or [])[:TOP_HYPOTHESES], start=1)]
        return {"bundle": bundle, "run": run, "results": results, "hypotheses": hypotheses}

    def _insert(self, record: dict[str, Any]) -> None:
        run = record["run"]
        columns = ", ".join(run)
        self._db.execute(f"INSERT INTO runs ({columns}) VALUES ({', '.join('?' for _ in run)})", tuple(run.values()))
        self._db.executemany(
            "INSERT INTO contract_results (run_id, contract_id, version, owner, severity, verdict, "
            "first_violation_ns, summary) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [(run["run_id"], *row) for row in record["results"]])
        self._db.executemany(
            "INSERT INTO hypotheses (run_id, rank, subsystem, interface, probability) VALUES (?, ?, ?, ?, ?)",
            [(run["run_id"], *row) for row in record["hypotheses"]])

    # -- reading -------------------------------------------------------

    def runs(self, *, experiment: str | None = None, verdict: str | None = None, since: str | None = None,
             limit: int | None = None) -> list[dict[str, Any]]:
        """Runs, newest first. ``since`` is a normalized timestamp (see :func:`parse_since`)."""
        where, params = self._filters(experiment=experiment, verdict=verdict, since=since)
        sql = f"SELECT * FROM runs {where} ORDER BY started_at DESC, run_id DESC"
        if limit is not None:
            sql += " LIMIT ?"
            params.append(limit)
        return [self._run_dict(row) for row in self._db.execute(sql, params)]

    def history(self, contract_id: str, *, experiment: str | None = None, limit: int | None = None) -> list[dict[str, Any]]:
        """One contract's results over time, newest first."""
        where, params = self._filters(experiment=experiment, prefix="r.")
        where = (where + " AND " if where else "WHERE ") + "c.contract_id = ?"
        params.append(contract_id)
        sql = ("SELECT r.run_id, r.experiment, r.seed, r.started_at, r.verdict AS run_verdict, r.config_hash, "
               "c.verdict, c.version, c.owner, c.severity, c.first_violation_ns, c.summary "
               f"FROM contract_results c JOIN runs r ON r.run_id = c.run_id {where} "
               "ORDER BY r.started_at DESC, r.run_id DESC")
        if limit is not None:
            sql += " LIMIT ?"
            params.append(limit)
        return [dict(row) for row in self._db.execute(sql, params)]

    def known_contracts(self) -> list[str]:
        return [row[0] for row in self._db.execute("SELECT DISTINCT contract_id FROM contract_results ORDER BY 1")]

    def run(self, run_id: str) -> dict[str, Any] | None:
        row = self._db.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
        return None if row is None else self._run_dict(row)

    def _filters(self, *, experiment: str | None = None, verdict: str | None = None, since: str | None = None,
                 prefix: str = "") -> tuple[str, list[Any]]:
        clauses, params = [], []
        for column, value, op in (("experiment", experiment, "="), ("verdict", verdict, "="), ("started_at", since, ">=")):
            if value is not None:
                clauses.append(f"{prefix}{column} {op} ?")
                params.append(value)
        return ("WHERE " + " AND ".join(clauses) if clauses else ""), params

    def _run_dict(self, row: sqlite3.Row) -> dict[str, Any]:
        doc = dict(row)
        for key in ("faults", "ground_truth", "timings"):
            doc[key] = None if doc[key] is None else json.loads(doc[key])
        doc["contracts"] = [dict(r) for r in self._db.execute(
            "SELECT contract_id, version, owner, severity, verdict, first_violation_ns, summary "
            "FROM contract_results WHERE run_id = ? ORDER BY contract_id", (doc["run_id"],))]
        doc["hypotheses"] = [dict(r) for r in self._db.execute(
            "SELECT rank, subsystem, interface, probability FROM hypotheses WHERE run_id = ? ORDER BY rank",
            (doc["run_id"],))]
        return doc
