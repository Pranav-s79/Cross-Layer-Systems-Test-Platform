"""Lint experiments and contract files with the adapter's knowledge applied."""

from __future__ import annotations

from pathlib import Path

from crosslayer.core.adapters.registry import load_adapter
from crosslayer.core.contracts.lint import LintFinding, lint_contracts
from crosslayer.core.contracts.loader import ensure_unique_ids, load_contracts_file
from crosslayer.core.orchestrator.experiment import ExperimentConfig


def lint_experiment(experiment: ExperimentConfig) -> list[LintFinding]:
    """Lint every contract the experiment would evaluate, against its adapter."""
    adapter = load_adapter(experiment.adapter)
    contracts = experiment.load_contracts()
    if experiment.include_adapter_contracts:
        contracts += adapter.contracts()
    ensure_unique_ids(contracts)
    return lint_contracts(
        contracts,
        catalog=adapter.event_catalog(),
        subsystems=adapter.describe().subsystems,
        timeout_s=experiment.timeout_s,
    )


def lint_contract_file(path: Path, adapter_ref: str | None = None) -> list[LintFinding]:
    """Lint a contract file, optionally against an adapter's catalog and subsystems."""
    contracts = load_contracts_file(path)
    if adapter_ref is None:
        return lint_contracts(contracts)
    adapter = load_adapter(adapter_ref)
    return lint_contracts(
        contracts,
        catalog=adapter.event_catalog(),
        subsystems=adapter.describe().subsystems,
    )
