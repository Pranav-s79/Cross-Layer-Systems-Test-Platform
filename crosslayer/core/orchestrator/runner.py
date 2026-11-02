"""The experiment runner: one call takes an experiment to a sealed run bundle.

Pipeline::

    resolve adapter -> validate (capabilities, faults, contracts)
      -> create bundle, snapshot configuration + versions
      -> build -> execute -> collect -> normalize      (adapter)
      -> order events -> evaluate contracts           (core)
      -> verdict -> report, junit, replay -> manifest (core)

Configuration problems raise ``ConfigError`` *before* a bundle is created.
Failures while running are recorded in the bundle and give verdict ERROR, so
there is always evidence to inspect.
"""

from __future__ import annotations

import traceback
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from crosslayer.core.adapters.base import (
    REQUIRED_CAPABILITIES,
    Artifact,
    Capability,
    ExecutionResult,
    ExecutionStatus,
    RunContext,
    SystemAdapter,
)
from crosslayer.core.adapters.registry import load_adapter
from crosslayer.core.artifacts.bundle import BUNDLE_FORMAT_VERSION, MANIFEST, RunBundle, sha256_file
from crosslayer.core.artifacts.environment import capture_environment
from crosslayer.core.artifacts.junit import render_junit
from crosslayer.core.artifacts.report import render_markdown
from crosslayer.core.contracts.evaluator import aggregate_verdict, evaluate_contracts, warnings
from crosslayer.core.contracts.loader import ensure_unique_ids
from crosslayer.core.contracts.model import BoundaryContract, ContractResult
from crosslayer.core.errors import AdapterError, ConfigError
from crosslayer.core.orchestrator.experiment import ExperimentConfig
from crosslayer.core.telemetry.events import NormalizedEvent
from crosslayer.core.telemetry.store import functional_fingerprint, order_events, write_events_jsonl
from crosslayer.core.verdict import Verdict


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(ts: datetime) -> str:
    return ts.isoformat(timespec="milliseconds").replace("+00:00", "Z")


@dataclass
class RunResult:
    run_id: str
    bundle_dir: Path
    verdict: Verdict
    results: list[ContractResult]
    manifest: dict[str, Any]


@dataclass
class _Plan:
    adapter: SystemAdapter
    capabilities: frozenset[Capability]
    contracts: list[BoundaryContract]
    adapter_contracts: list[BoundaryContract]
    mode: str
    fault_labels: list[dict[str, Any]]


class ExperimentRunner:
    def __init__(
        self,
        output_root: Path,
        *,
        adapter_loader: Callable[[str], SystemAdapter] = load_adapter,
        clock: Callable[[], datetime] = _utc_now,
    ) -> None:
        self.output_root = output_root
        self._load_adapter = adapter_loader
        self._clock = clock

    # -- validation ----------------------------------------------------------

    def plan(self, experiment: ExperimentConfig) -> _Plan:
        """Validate everything that can be checked without running anything."""
        adapter = self._load_adapter(experiment.adapter)
        caps = adapter.capabilities()
        missing = REQUIRED_CAPABILITIES - caps
        if missing:
            raise ConfigError(f"adapter {experiment.adapter!r} lacks required capabilities {sorted(missing)}")
        adapter.validate_parameters(experiment.parameters)

        specs = {f.name: f for f in adapter.faults()}
        if experiment.faults and Capability.FAULT_INJECTION not in caps:
            raise ConfigError(f"adapter {experiment.adapter!r} does not support fault injection")
        unknown = [f.name for f in experiment.faults if f.name not in specs]
        if unknown:
            raise ConfigError(f"unknown faults {unknown}; adapter supports {sorted(specs)}")
        for fault in experiment.faults:
            bad = set(fault.params) - set(specs[fault.name].parameters)
            if bad:
                raise ConfigError(
                    f"fault {fault.name!r} has no parameters {sorted(bad)} "
                    f"(supported: {sorted(specs[fault.name].parameters)})"
                )
        adapter.validate_faults(experiment.faults)
        fault_labels = [
            {**f.to_dict(), "target_subsystem": specs[f.name].target_subsystem,
             "target_interface": specs[f.name].target_interface}
            for f in experiment.faults
        ]

        if experiment.inputs:
            mode = "analyze"
        elif Capability.EXECUTE in caps:
            mode = "execute"
        else:
            raise ConfigError(
                f"adapter {experiment.adapter!r} cannot execute; give 'inputs' to analyze existing artifacts"
            )
        if mode == "analyze" and experiment.faults:
            raise ConfigError("faults cannot be injected when analyzing existing artifacts")

        adapter_contracts = adapter.contracts() if experiment.include_adapter_contracts else []
        contracts = experiment.load_contracts() + adapter_contracts
        ensure_unique_ids(contracts)
        if not contracts:
            raise ConfigError("no contracts to evaluate; add contracts or enable include_adapter_contracts")
        subsystems = set(adapter.describe().subsystems)
        for contract in contracts:
            undeclared = {contract.source, contract.destination} - subsystems
            if undeclared:
                raise ConfigError(
                    f"contract {contract.id!r} references subsystems {sorted(undeclared)} "
                    f"that adapter {experiment.adapter!r} does not declare ({sorted(subsystems)})"
                )
        return _Plan(adapter, caps, contracts, adapter_contracts, mode, fault_labels)

    # -- execution -----------------------------------------------------------

    def _new_run_id(self, started: datetime, config_hash: str) -> str:
        base = f"{started:%Y%m%dT%H%M%SZ}-{config_hash[:8]}"
        run_id, n = base, 1
        while (self.output_root / run_id).exists():
            run_id = f"{base}-{n}"
            n += 1
        return run_id

    @staticmethod
    def _check_artifacts(bundle: RunBundle, artifacts: list[Artifact]) -> None:
        raw = bundle.raw_dir.resolve()
        for artifact in artifacts:
            path = artifact.path.resolve()
            if not path.is_file():
                raise AdapterError(f"artifact {artifact.name!r} does not exist at {path}")
            if raw not in path.parents:
                raise AdapterError(f"artifact {artifact.name!r} is outside raw_artifacts/: {path}")

    @staticmethod
    def _check_events(run_id: str, events: list[NormalizedEvent]) -> None:
        wrong = {e.run_id for e in events if e.run_id != run_id}
        if wrong:
            raise AdapterError(f"events carry foreign run ids {sorted(wrong)[:3]}")
        dupes = [eid for eid, n in Counter(e.event_id for e in events).items() if n > 1]
        if dupes:
            raise AdapterError(f"duplicate event ids, e.g. {dupes[:3]}")

    def run(self, experiment: ExperimentConfig) -> RunResult:
        plan = self.plan(experiment)
        adapter = plan.adapter
        description = adapter.describe()
        config_hash = experiment.config_hash(plan.adapter_contracts)

        self.output_root.mkdir(parents=True, exist_ok=True)
        started = self._clock()
        run_id = self._new_run_id(started, config_hash)
        bundle = RunBundle.create(self.output_root, run_id)

        ctx = RunContext(
            run_id=run_id,
            seed=experiment.seed,
            parameters=dict(experiment.parameters),
            faults=experiment.faults,
            raw_dir=bundle.raw_dir,
            work_dir=bundle.work_dir,
            base_dir=experiment.base_dir,
            timeout_s=experiment.timeout_s,
            inputs=experiment.inputs,
        )

        bundle.write_yaml("configuration.yaml", experiment.resolved_document(plan.adapter_contracts))
        bundle.write_json(
            "versions.json",
            {
                "environment": capture_environment(),
                "adapter": description.to_dict(),
                "capabilities": sorted(plan.capabilities),
            },
        )

        errors: list[dict[str, str]] = []
        execution: ExecutionResult | None = None
        build_details: dict[str, Any] | None = None
        events: list[NormalizedEvent] = []
        diagnostics: list[dict[str, Any]] = []
        normalization_stats: dict[str, Any] = {}
        have_events = False
        stage = "build"
        try:
            if plan.mode == "execute" and Capability.BUILD in plan.capabilities:
                build = adapter.build(ctx)
                build_details = build.details
                if not build.ok:
                    raise AdapterError(f"build failed: {build.details}")
            stage = "execute"
            if plan.mode == "execute":
                execution = adapter.execute(ctx)
                if execution.status is not ExecutionStatus.COMPLETED:
                    errors.append({"stage": "execute", "message": f"execution {execution.status}"})
            stage = "collect"
            artifacts = adapter.collect(ctx, execution)
            self._check_artifacts(bundle, artifacts)
            stage = "normalize"
            normalization = adapter.normalize(ctx, artifacts)
            events = order_events(normalization.events)
            self._check_events(run_id, events)
            diagnostics = [d.to_dict() for d in normalization.diagnostics]
            normalization_stats = normalization.stats
            have_events = True
        except Exception as exc:  # noqa: BLE001 - record any adapter failure in the bundle
            errors.append(
                {
                    "stage": stage,
                    "message": f"{type(exc).__name__}: {exc}",
                    "traceback": traceback.format_exc(),
                }
            )
            events = []

        write_events_jsonl(bundle.path("normalized_events.jsonl"), events)
        bundle.write_json("normalization.json", {"stats": normalization_stats, "diagnostics": diagnostics})

        # Without a normalized event stream every contract would be judged on
        # nothing, which produces misleading failures. Skip evaluation instead.
        results = evaluate_contracts(plan.contracts, events) if have_events else []
        verdict = Verdict.ERROR if errors else aggregate_verdict(results)

        bundle.write_json(
            "contracts.json",
            {
                "contracts": [
                    {"origin": r.contract.origin, "definition": dict(r.contract.definition), "result": r.to_dict()}
                    for r in results
                ]
            },
        )
        bundle.write_json(
            "violations.json",
            {"violations": [r.to_dict() for r in results if r.verdict is Verdict.FAIL]},
        )

        finished = self._clock()
        counts = Counter(str(r.verdict) for r in results)
        manifest: dict[str, Any] = {
            "bundle_format_version": BUNDLE_FORMAT_VERSION,
            "run_id": run_id,
            "verdict": str(verdict),
            "mode": plan.mode,
            "started_at": _iso(started),
            "finished_at": _iso(finished),
            "config_hash": config_hash,
            "experiment": {
                "name": experiment.name,
                "description": experiment.description,
                "seed": experiment.seed,
                "source": str(experiment.source_path) if experiment.source_path else None,
            },
            "adapter": {
                "name": description.name,
                "adapter_version": description.adapter_version,
                "system": description.system,
                "system_version": description.system_version,
            },
            "build": build_details,
            "execution": execution.to_dict() if execution else None,
            "inputs": [{"path": str(p), "sha256": sha256_file(p)} for p in experiment.inputs],
            "faults": plan.fault_labels,
            "events": {
                "count": len(events),
                "fingerprint": functional_fingerprint(events),
                "parse_diagnostics": len(diagnostics),
                "by_type": dict(sorted(Counter(e.event_type for e in events).items())),
            },
            "contracts": {"total": len(results), **{v.value: counts.get(v.value, 0) for v in Verdict}},
            "warnings": [r.contract.id for r in warnings(results)],
            "errors": errors,
        }

        bundle.write_text("report.md", render_markdown(manifest, results))
        bundle.write_text("junit.xml", render_junit(manifest, results))
        bundle.write_text("replay.sh", self._replay_script(manifest, description.external_dependencies))
        manifest["files"] = bundle.file_index()
        bundle.write_json(MANIFEST, manifest)

        return RunResult(run_id=run_id, bundle_dir=bundle.root, verdict=verdict, results=results, manifest=manifest)

    @staticmethod
    def _replay_script(manifest: dict[str, Any], external: tuple[str, ...]) -> str:
        adapter = manifest["adapter"]
        lines = [
            "#!/usr/bin/env sh",
            f"# Replay CrossLayer run {manifest['run_id']}",
            f"# experiment: {manifest['experiment']['name']}  seed: {manifest['experiment']['seed']}",
            f"# config hash: {manifest['config_hash']}",
            f"# expected verdict: {manifest['verdict']}",
            f"# expected event fingerprint: {manifest['events']['fingerprint']}",
            "#",
            "# Not bundled; must be present to replay:",
            "#   - crosslayer (see versions.json for version and git revision)",
            f"#   - adapter {adapter['name']} {adapter['adapter_version']}"
            f" for {adapter['system']} {adapter.get('system_version') or ''}".rstrip(),
            *[f"#   - {dep}" for dep in external],
        ]
        for item in manifest.get("inputs") or []:
            lines.append(f"#   - input {item['path']} (sha256 {item['sha256']})")
        lines += [
            "set -eu",
            'HERE="$(cd "$(dirname "$0")" && pwd)"',
            'PYTHON="${PYTHON:-python}"',
            f'exec "$PYTHON" -m crosslayer run "$HERE/configuration.yaml" --out "${{1:-$HERE/../replays}}"',
            "",
        ]
        return "\n".join(lines)
