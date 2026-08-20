"""Tests for the board's session lifetime.

These drive the real package boundary, in a subprocess, because the defect they
exist for does not raise: creating a second Tk interpreter in one process makes
Tcl call ``abort()``. There is no exception to assert on -- only a process that
is suddenly gone -- so the assertion has to be on the exit status.

That is exactly how it presented in live play: the application vanished and the
log simply stopped.
"""

from __future__ import annotations

import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src"

CYCLE_SCRIPT = """
import sys, time, tempfile, pathlib
sys.path.insert(0, {src!r})
from league_skin_manager import cooldown

cache = pathlib.Path(tempfile.mkdtemp())
for _ in range({cycles}):
    cooldown.open_panel(cache_dir=cache)
    time.sleep(0.4)
    cooldown.close_panel()
    time.sleep(0.2)
    cooldown.open_panel(cache_dir=cache)
    time.sleep(0.2)
print("VISIBLE", cooldown.is_visible())
print("OPEN", cooldown.is_open())
cooldown.release_panel()
print("AFTER_RELEASE", cooldown.is_open())
print("SURVIVED")
"""


def run_cycles(cycles: int = 3, timeout: float = 90.0) -> subprocess.CompletedProcess[str]:
    script = CYCLE_SCRIPT.format(src=str(SRC), cycles=cycles)
    return subprocess.run(
        [sys.executable, "-c", textwrap.dedent(script)],
        capture_output=True,
        text=True,
        timeout=timeout,
    )


@pytest.mark.slow
def test_hiding_and_showing_repeatedly_does_not_kill_the_process() -> None:
    """The live crash: reopening built a second interpreter and Tcl aborted.

    Under the session design nothing is rebuilt, so there is never a second
    interpreter to abort over.
    """

    result = run_cycles()

    assert "Tcl_AsyncDelete" not in (result.stdout + result.stderr), (
        "Tcl aborted the process; the interpreter is being torn down or "
        "rebuilt across threads again"
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "SURVIVED" in result.stdout


@pytest.mark.slow
def test_a_session_survives_hiding_and_is_gone_after_release() -> None:
    result = run_cycles(cycles=1)

    assert result.returncode == 0, result.stdout + result.stderr
    assert "OPEN True" in result.stdout, "hiding must not end the session"
    assert "AFTER_RELEASE False" in result.stdout, "releasing must end it"
