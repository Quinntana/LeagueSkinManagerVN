"""Tests for the Windows integration adapter."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest

from league_skin_manager import porofessor, windows
from league_skin_manager.config import APP_DISPLAY_NAME, APP_NAME, POROFESSOR_DOWNLOAD_URL

# --- single instance ------------------------------------------------------


class FakeKernel32:
    """Mimics CreateMutexW / CloseHandle well enough to exercise the logic."""

    def __init__(self, already_exists: bool = False, handle: int = 4242) -> None:
        self.already_exists = already_exists
        self.handle = handle
        self.closed: list[int] = []
        self.CreateMutexW = self._create
        self.CloseHandle = self._close

    class _Stub:
        argtypes: Any = None
        restype: Any = None

        def __init__(self, call: Any) -> None:
            self._call = call

        def __call__(self, *args: Any) -> Any:
            return self._call(*args)

    def _create(self, *_args: Any) -> int:
        import ctypes

        ctypes.set_last_error(windows.ERROR_ALREADY_EXISTS if self.already_exists else 0)
        return self.handle

    def _close(self, handle: Any) -> bool:
        self.closed.append(int(getattr(handle, "value", handle) or 0))
        return True

    def __getattr__(self, name: str) -> Any:  # pragma: no cover - defensive
        raise AttributeError(name)


def make_kernel(already_exists: bool = False) -> Any:
    kernel = FakeKernel32(already_exists)
    kernel.CreateMutexW = FakeKernel32._Stub(kernel._create)
    kernel.CloseHandle = FakeKernel32._Stub(kernel._close)
    return kernel


def test_the_first_instance_acquires_the_lock() -> None:
    instance = windows.SingleInstance(kernel32=make_kernel(already_exists=False))
    assert instance.acquire() is True
    instance.release()


def test_a_second_instance_is_refused() -> None:
    instance = windows.SingleInstance(kernel32=make_kernel(already_exists=True))
    assert instance.acquire() is False


def test_a_refused_instance_closes_its_handle() -> None:
    kernel = make_kernel(already_exists=True)
    windows.SingleInstance(kernel32=kernel).acquire()
    assert kernel.closed == [4242]


def test_acquiring_twice_is_idempotent() -> None:
    instance = windows.SingleInstance(kernel32=make_kernel())
    assert instance.acquire() is True
    assert instance.acquire() is True
    instance.release()


def test_release_is_safe_without_acquire() -> None:
    windows.SingleInstance(kernel32=make_kernel()).release()


def test_the_context_manager_raises_when_already_running() -> None:
    with (
        pytest.raises(RuntimeError, match="already running"),
        windows.SingleInstance(kernel32=make_kernel(already_exists=True)),
    ):
        pass


# --- startup --------------------------------------------------------------


def test_the_startup_command_is_the_quoted_executable(tmp_path: Path) -> None:
    executable = tmp_path / "LeagueSkinManagerVN.exe"
    command = windows.startup_command(executable)
    assert command.startswith('"') and command.endswith('"')
    assert str(executable) in command


def test_the_startup_command_carries_no_arguments(tmp_path: Path) -> None:
    """The old build passed --background; the tray is the only interface now."""

    command = windows.startup_command(tmp_path / "app.exe")
    assert "--background" not in command
    assert command.count('"') == 2


def test_the_run_key_is_per_user() -> None:
    assert windows.RUN_KEY.startswith("Software\\Microsoft\\Windows")
    assert "CurrentVersion\\Run" in windows.RUN_KEY


# --- Start Menu shortcut --------------------------------------------------


def test_the_shortcut_lives_in_the_start_menu_programs_folder() -> None:
    shortcut = windows.start_menu_shortcut()
    assert shortcut.suffix == ".lnk"
    assert shortcut.parent.name == "Programs"
    assert "Start Menu" in str(shortcut)
    assert shortcut.stem == APP_DISPLAY_NAME


def capture_runner(returncode: int = 0) -> tuple[Any, list[tuple[str, dict[str, str]]]]:
    seen: list[tuple[str, dict[str, str]]] = []

    def runner(script: str, environment: dict[str, str] | None = None) -> Any:
        seen.append((script, environment or {}))
        return subprocess.CompletedProcess([], returncode, stdout="", stderr="denied")

    return runner, seen


def test_the_shortcut_targets_the_executable(tmp_path: Path) -> None:
    runner, seen = capture_runner()
    executable = tmp_path / "LeagueSkinManagerVN.exe"
    executable.write_bytes(b"exe")

    windows.create_start_menu_shortcut(executable, runner=runner)

    assert seen, "no PowerShell invocation was produced"
    script, environment = seen[0]
    assert "WScript.Shell" in script
    assert "$s.Save()" in script
    assert environment[windows.SHORTCUT_TARGET_ENV] == str(executable)
    assert environment[windows.SHORTCUT_WORKDIR_ENV] == str(executable.parent)


def test_paths_never_reach_the_script_body(tmp_path: Path) -> None:
    """A quote in a path would otherwise close the string and run as code."""

    awkward = tmp_path / "Bob's PC"
    awkward.mkdir()
    executable = awkward / "app.exe"
    executable.write_bytes(b"exe")

    runner, seen = capture_runner()
    windows.create_start_menu_shortcut(executable, runner=runner)

    script, environment = seen[0]
    assert str(executable) not in script, "the path must not be interpolated"
    assert "Bob" not in script
    assert environment[windows.SHORTCUT_TARGET_ENV] == str(executable)


def test_the_script_is_a_fixed_constant(tmp_path: Path) -> None:
    runner, seen = capture_runner()
    windows.create_start_menu_shortcut(tmp_path / "a.exe", runner=runner)
    windows.create_start_menu_shortcut(tmp_path / "b.exe", runner=runner)
    assert seen[0][0] == seen[1][0] == windows._SHORTCUT_SCRIPT


def test_a_failed_shortcut_creation_reports_false(tmp_path: Path) -> None:
    runner, _seen = capture_runner(returncode=1)
    assert windows.create_start_menu_shortcut(tmp_path / "app.exe", runner=runner) is False


def test_powershell_is_resolved_from_the_system_directory() -> None:
    """PATH must not decide which PowerShell verifies or writes anything."""

    resolved = windows.system_powershell()
    assert resolved.name == "powershell.exe"
    assert resolved.parent.name == "v1.0"
    assert "System32" in str(resolved)


def test_the_mutex_is_session_local_not_machine_wide() -> None:
    """Global would block a second logged-in user from running their own copy."""

    assert windows.MUTEX_NAME.startswith("Local\\")
    assert not windows.MUTEX_NAME.startswith("Global")


# --- processes ------------------------------------------------------------


class FakeProcess:
    def __init__(self, name: str) -> None:
        self.info = {"name": name}


def test_a_running_process_is_detected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        windows.psutil, "process_iter", lambda _f: [FakeProcess("League of Legends.exe")]
    )
    assert windows.ProcessLookup.is_running("League of Legends.exe") is True


def test_process_matching_ignores_case(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(windows.psutil, "process_iter", lambda _f: [FakeProcess("LTK-MANAGER.EXE")])
    assert windows.ProcessLookup.is_any_running(["ltk-manager.exe"]) is True


def test_an_absent_process_is_not_detected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(windows.psutil, "process_iter", lambda _f: [FakeProcess("explorer.exe")])
    assert windows.ProcessLookup.is_running("League of Legends.exe") is False


def test_an_empty_name_set_matches_nothing() -> None:
    assert windows.ProcessLookup.is_any_running([]) is False
    assert windows.ProcessLookup.is_any_running([""]) is False


def test_a_process_that_vanishes_mid_scan_is_skipped(monkeypatch: pytest.MonkeyPatch) -> None:
    class Vanishing:
        @property
        def info(self) -> dict[str, Any]:
            raise windows.psutil.NoSuchProcess(1)

    monkeypatch.setattr(
        windows.psutil, "process_iter", lambda _f: [Vanishing(), FakeProcess("target.exe")]
    )
    assert windows.ProcessLookup.is_running("target.exe") is True


def test_launching_a_missing_executable_reports_false(tmp_path: Path) -> None:
    assert windows.launch_detached(tmp_path / "absent.exe") is False


# --- porofessor -----------------------------------------------------------


def test_porofessor_only_opens_its_download_page(monkeypatch: pytest.MonkeyPatch) -> None:
    opened: list[str] = []
    monkeypatch.setattr(porofessor, "open_url", lambda url: opened.append(url) or True)
    assert porofessor.open_download_page() is True
    assert opened == [POROFESSOR_DOWNLOAD_URL]


def test_porofessor_exposes_nothing_but_the_page() -> None:
    """No detection, no launch, no version check: nothing that can break."""

    assert porofessor.__all__ == ["open_download_page"]


def test_the_app_name_is_stable() -> None:
    assert APP_NAME == "LeagueSkinManagerVN"
