"""Contract versioning: what changed between two sets of contracts, per owner.

A contract's ``version`` must increase whenever what it *checks* changes:
its rule, boundary, precondition, evidence, severity, or failure policy.
Wording (title, description, tags) and ownership may change freely. This
keeps verdict histories honest: a contract that started failing because it
was tightened can be told apart from a system that regressed.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable

from crosslayer.core.contracts.model import BoundaryContract
from crosslayer.core.telemetry.store import canonical_json

SEMANTIC_FIELDS = ("source", "destination", "rule", "precondition", "evidence", "severity", "on_fail")
METADATA_FIELDS = ("title", "description", "tags")
_DEFAULTS: dict[str, Any] = {"severity": "major", "on_fail": "fail_run", "precondition": None,
                             "evidence": [], "description": "", "tags": []}


def _normalized(contract: BoundaryContract, name: str) -> str:
    """Field value with defaults applied, so `severity: major` == omitted."""
    value = contract.definition.get(name, _DEFAULTS.get(name))
    if name == "evidence" and isinstance(value, dict):
        value = [value]  # a single selector is shorthand for a one-item list
    if name == "description" and value is None:
        value = ""
    return canonical_json(value)


def changed_fields(old: BoundaryContract, new: BoundaryContract, names: Iterable[str]) -> list[str]:
    return [n for n in names if _normalized(old, n) != _normalized(new, n)]


@dataclass
class ContractChange:
    contract_id: str
    owner: str
    kind: str
    """``added``, ``removed``, ``changed`` (semantic), ``metadata``, or ``version``."""
    old_version: int | None = None
    new_version: int | None = None
    fields: list[str] = field(default_factory=list)
    previous_owner: str | None = None
    problem: str | None = None

    def describe(self) -> str:
        if self.kind == "added":
            text = f"added (v{self.new_version})"
        elif self.kind == "removed":
            text = f"removed (was v{self.old_version})"
        elif self.kind == "version":
            text = f"version v{self.old_version} -> v{self.new_version} with no semantic change"
        else:
            text = f"{self.kind}: {', '.join(self.fields)} (v{self.old_version} -> v{self.new_version})"
        if self.previous_owner:
            text += f"; owner {self.previous_owner} -> {self.owner}"
        return text


def diff_contracts(old: Iterable[BoundaryContract], new: Iterable[BoundaryContract]) -> list[ContractChange]:
    before = {c.id: c for c in old}
    after = {c.id: c for c in new}
    changes: list[ContractChange] = []

    for cid in sorted(before.keys() | after.keys()):
        o, n = before.get(cid), after.get(cid)
        if o is None:
            if n is not None:
                changes.append(ContractChange(cid, n.owner, "added", new_version=n.version))
            continue
        if n is None:
            changes.append(ContractChange(cid, o.owner, "removed", old_version=o.version))
            continue

        semantic = changed_fields(o, n, SEMANTIC_FIELDS)
        metadata = changed_fields(o, n, METADATA_FIELDS)
        owner_moved = o.owner if o.owner != n.owner else None
        change = ContractChange(cid, n.owner, "", o.version, n.version, previous_owner=owner_moved)

        if n.version < o.version:
            change.problem = f"version went backwards (v{o.version} -> v{n.version})"
        if semantic:
            change.kind, change.fields = "changed", semantic
            if n.version <= o.version and change.problem is None:
                change.problem = f"{', '.join(semantic)} changed without a version bump (still v{n.version})"
        elif metadata or owner_moved:
            change.kind, change.fields = "metadata", metadata + (["owner"] if owner_moved else [])
        elif n.version != o.version:
            change.kind = "version"
        else:
            continue
        changes.append(change)
    return changes


def render_changelog(changes: list[ContractChange]) -> str:
    """Markdown changelog grouped by (current) owner."""
    if not changes:
        return "No contract changes.\n"
    by_owner: dict[str, list[ContractChange]] = {}
    for c in changes:
        by_owner.setdefault(c.owner, []).append(c)
    lines = ["# Contract changes\n"]
    for owner in sorted(by_owner):
        lines.append(f"## {owner}\n")
        for c in by_owner[owner]:
            flag = f" **PROBLEM: {c.problem}**" if c.problem else ""
            lines.append(f"- `{c.contract_id}`: {c.describe()}{flag}")
        lines.append("")
    return "\n".join(lines)
