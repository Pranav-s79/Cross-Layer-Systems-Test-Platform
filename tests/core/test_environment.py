"""Direct tests for environment capture."""

from __future__ import annotations

import platform
import subprocess
from pathlib import Path

import pytest

import crosslayer
from crosslayer.core.artifacts import environment
from crosslayer.core.artifacts.environment import capture_environment, git_info


def test_capture_fields():
    env = capture_environment()
    assert set(env) == {"crosslayer_version", "crosslayer_git", "python", "platform"}
    assert env["crosslayer_version"] == crosslayer.__version__
    assert env["python"] == {
        "version": platform.python_version(),
        "implementation": platform.python_implementation(),
    }
    assert env["platform"] == {
        "system": platform.system(),
        "release": platform.release(),
        "machine": platform.machine(),
    }


def test_capture_is_stable_between_calls():
    assert capture_environment() == capture_environment()


def test_git_info_outside_repository_is_none(tmp_path: Path):
    assert git_info(tmp_path) is None


def test_git_info_requires_repository_root(tmp_path: Path):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    sub = tmp_path / "sub"
    sub.mkdir()
    assert git_info(sub) is None


def test_git_info_without_commits_is_none(tmp_path: Path):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    assert git_info(tmp_path) is None


def test_git_info_reports_revision_and_dirty_flag(tmp_path: Path):
    def run(*args: str) -> None:
        subprocess.run(["git", *args], cwd=tmp_path, check=True, capture_output=True)

    run("init", "-q")
    run("config", "user.name", "t")
    run("config", "user.email", "t@example.com")
    (tmp_path / "a.txt").write_text("a")
    run("add", "a.txt")
    run("commit", "-q", "-m", "init")
    clean = git_info(tmp_path)
    assert clean is not None
    assert len(clean["revision"]) == 40
    assert clean["dirty"] is False
    (tmp_path / "a.txt").write_text("b")
    dirty = git_info(tmp_path)
    assert dirty is not None
    assert dirty["revision"] == clean["revision"]
    assert dirty["dirty"] is True


def test_git_missing_binary_is_none(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    def boom(*a, **k):
        raise FileNotFoundError("git")

    monkeypatch.setattr(environment.subprocess, "run", boom)
    assert git_info(tmp_path) is None
