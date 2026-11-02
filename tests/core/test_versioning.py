"""Contract versioning and run-to-run contract change detection."""

from __future__ import annotations

import pytest

from crosslayer.core.contracts import parse_contract
from crosslayer.core.contracts.versioning import diff_contracts, render_changelog
from crosslayer.core.errors import ConfigError

BASE = {"id": "a.b", "title": "t", "owner": "team-a", "source": "s", "destination": "d",
        "rule": {"kind": "count", "select": {"event_type": "x"}, "min": 1}}


def c(**overrides):
    return parse_contract({**BASE, **overrides}, "test")


def test_version_defaults_to_one_and_is_validated():
    assert c().version == 1
    assert c(version=3).version == 3
    for bad in (0, -1, "2", True):
        with pytest.raises(ConfigError, match="version"):
            c(version=bad)


def test_identical_and_default_equivalent_contracts_have_no_changes():
    assert diff_contracts([c()], [c()]) == []
    # omitting a default is the same contract as stating it
    assert diff_contracts([c()], [c(severity="major", on_fail="fail_run", evidence=[])]) == []


def test_semantic_change_without_bump_is_a_problem():
    [change] = diff_contracts([c()], [c(rule={**BASE["rule"], "min": 2})])
    assert change.kind == "changed" and change.fields == ["rule"]
    assert "without a version bump" in change.problem


def test_semantic_change_with_bump_is_fine():
    [change] = diff_contracts([c()], [c(version=2, on_fail="warn")])
    assert change.kind == "changed" and change.fields == ["on_fail"] and change.problem is None


def test_wording_and_owner_changes_need_no_bump():
    [change] = diff_contracts([c()], [c(title="clearer", owner="team-b")])
    assert change.kind == "metadata" and change.fields == ["title", "owner"]
    assert change.problem is None and change.previous_owner == "team-a"


def test_version_going_backwards_is_a_problem():
    [change] = diff_contracts([c(version=3)], [c(version=2)])
    assert "backwards" in change.problem


def test_added_removed_and_bare_version_bump():
    changes = diff_contracts([c(), c(id="old.one")], [c(version=2), c(id="new.one")])
    assert {(x.contract_id, x.kind) for x in changes} == {
        ("a.b", "version"), ("old.one", "removed"), ("new.one", "added")}
    assert not any(x.problem for x in changes)


def test_changelog_groups_by_owner():
    text = render_changelog(diff_contracts([c()], [c(rule={**BASE["rule"], "min": 2}), c(id="z.z", owner="team-z")]))
    assert text.index("## team-a") < text.index("## team-z")
    assert "PROBLEM" in text
