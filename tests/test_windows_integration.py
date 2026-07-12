from __future__ import annotations

import ctypes
import logging
from pathlib import Path
from typing import Any

from league_skin_manager.windows_integration import (
    ERROR_ALREADY_EXISTS,
    LEGACY_MUTEX_NAME,
    MUTEX_NAME,
    ProcessLauncher,
    SingleInstanceMutex,
    StartupRegistration,
)


class Function:
    def __init__(self, result: int) -> None:
        self.result = result
        self.calls: list[tuple[Any, ...]] = []
        self.argtypes: object = None
        self.restype: object = None

    def __call__(self, *args: Any) -> int:
        self.calls.append(args)
        return self.result


class Kernel32:
    def __init__(self, handle: int = 123) -> None:
        self.CreateMutexW = Function(handle)
        self.CloseHandle = Function(1)


def test_mutex_acquires_and_releases_native_handle(monkeypatch: Any) -> None:
    kernel32 = Kernel32()
    monkeypatch.setattr(ctypes, "set_last_error", lambda _value: None)
    monkeypatch.setattr(ctypes, "get_last_error", lambda: 0)
    mutex = SingleInstanceMutex(kernel32=kernel32)
    assert mutex.acquire()
    assert mutex.handle == 123
    mutex.release()
    assert mutex.handle is None
    assert [call[2] for call in kernel32.CreateMutexW.calls] == [
        LEGACY_MUTEX_NAME,
        MUTEX_NAME,
    ]
    assert len(kernel32.CloseHandle.calls) == 2


def test_mutex_rejects_existing_instance_and_closes_duplicate(monkeypatch: Any) -> None:
    kernel32 = Kernel32()
    monkeypatch.setattr(ctypes, "set_last_error", lambda _value: None)
    monkeypatch.setattr(ctypes, "get_last_error", lambda: ERROR_ALREADY_EXISTS)
    mutex = SingleInstanceMutex(kernel32=kernel32)
    assert not mutex.acquire()
    assert mutex.handle is None
    assert len(kernel32.CloseHandle.calls) == 1


def test_mutex_rolls_back_legacy_handle_when_v2_instance_exists(monkeypatch: Any) -> None:
    kernel32 = Kernel32()
    errors = iter((0, ERROR_ALREADY_EXISTS))
    monkeypatch.setattr(ctypes, "set_last_error", lambda _value: None)
    monkeypatch.setattr(ctypes, "get_last_error", lambda: next(errors))

    mutex = SingleInstanceMutex(kernel32=kernel32)

    assert not mutex.acquire()
    assert mutex.handle is None
    assert len(kernel32.CreateMutexW.calls) == 2
    assert len(kernel32.CloseHandle.calls) == 2


def test_mutex_without_native_backend_is_a_safe_noop() -> None:
    mutex = SingleInstanceMutex(kernel32=None)
    mutex.kernel32 = None
    assert mutex.acquire()
    mutex.release()


def test_startup_command_quotes_absolute_executable(tmp_path: Path) -> None:
    executable = tmp_path / "folder with spaces" / "app.exe"
    assert StartupRegistration._command(executable) == f'"{executable.resolve()}"'


class Process:
    def __init__(self, name: str | None = None, error: Exception | None = None) -> None:
        self.info = {"name": name}
        self.error = error


def test_process_running_lookup_is_case_insensitive(monkeypatch: Any) -> None:
    monkeypatch.setattr(
        "league_skin_manager.windows_integration.psutil.process_iter",
        lambda _fields: [Process(None), Process("CSLOL-MANAGER.EXE")],
    )
    assert ProcessLauncher.is_running("cslol-manager.exe")
    assert not ProcessLauncher.is_running("different.exe")


def test_launcher_rejects_missing_file_and_starts_existing(
    monkeypatch: Any, tmp_path: Path
) -> None:
    launcher = ProcessLauncher(logging.getLogger("test"))
    assert not launcher.launch(tmp_path / "missing.exe")

    executable = tmp_path / "manager.exe"
    executable.write_bytes(b"exe")
    calls: list[tuple[list[str], dict[str, object]]] = []
    monkeypatch.setattr(
        "league_skin_manager.windows_integration.subprocess.Popen",
        lambda args, **kwargs: calls.append((args, kwargs)),
    )
    assert launcher.launch(executable)
    assert calls[0][0] == [str(executable)]
    assert calls[0][1]["cwd"] == str(tmp_path)
