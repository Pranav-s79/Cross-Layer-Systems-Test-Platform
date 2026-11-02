from __future__ import annotations

import sys

from crosslayer.core.orchestrator.process import run_process


def test_captures_output_and_exit_code(tmp_path):
    result = run_process(
        [sys.executable, "-c", "import sys; print('hello'); sys.stderr.write('oops'); sys.exit(3)"],
        cwd=tmp_path,
        timeout_s=30,
        stdout_path=tmp_path / "out.log",
        stderr_path=tmp_path / "err.log",
    )
    assert result.exit_code == 3 and not result.timed_out and result.error is None
    assert (tmp_path / "out.log").read_text().strip() == "hello"
    assert (tmp_path / "err.log").read_text() == "oops"


def test_timeout_kills_process(tmp_path):
    result = run_process(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        cwd=tmp_path,
        timeout_s=1,
        stdout_path=tmp_path / "out.log",
        stderr_path=tmp_path / "err.log",
    )
    assert result.timed_out and result.exit_code is None
    assert result.duration_s < 30


def test_env_overrides_reach_child(tmp_path):
    run_process(
        [sys.executable, "-c", "import os; print(os.environ['CROSSLAYER_PROBE'])"],
        cwd=tmp_path,
        timeout_s=30,
        stdout_path=tmp_path / "out.log",
        stderr_path=tmp_path / "err.log",
        env_overrides={"CROSSLAYER_PROBE": "42"},
    )
    assert (tmp_path / "out.log").read_text().strip() == "42"


def test_missing_executable_reports_error(tmp_path):
    result = run_process(
        ["definitely-not-a-real-binary-xyz"],
        cwd=tmp_path,
        timeout_s=5,
        stdout_path=tmp_path / "out.log",
        stderr_path=tmp_path / "err.log",
    )
    assert result.error is not None and result.exit_code is None
