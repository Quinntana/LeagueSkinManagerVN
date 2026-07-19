from __future__ import annotations

import ctypes
import logging
from pathlib import Path
from typing import Any

import pytest

import league_skin_manager.windows_integration as windows_integration
from league_skin_manager.windows_integration import (
    ACTIVATION_EVENT_NAME,
    ERROR_ALREADY_EXISTS,
    LEGACY_MUTEX_NAME,
    MUTEX_NAME,
    WAIT_OBJECT_0,
    WAIT_TIMEOUT,
    InstanceActivationEvent,
    ProcessLauncher,
    SingleInstanceMutex,
    StartupRegistration,
    copy_text_to_clipboard,
    open_path,
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


class ActivationKernel32:
    def __init__(self) -> None:
        self.CreateEventW = Function(456)
        self.SetEvent = Function(1)
        self.WaitForSingleObject = Function(WAIT_OBJECT_0)
        self.CloseHandle = Function(1)


class ClipboardBackend:
    def __init__(
        self, open_results: tuple[bool, ...] = (True,), *, fail_on: str | None = None
    ) -> None:
        self.open_results = iter(open_results)
        self.fail_on = fail_on
        self.allocated: list[str] = []
        self.open_calls = 0
        self.empty_calls = 0
        self.set_calls: list[int] = []
        self.close_calls = 0
        self.freed: list[int] = []

    def allocate_unicode(self, text: str) -> int:
        self.allocated.append(text)
        if self.fail_on == "allocate":
            raise OSError("allocation failed")
        return 789

    def open(self) -> bool:
        self.open_calls += 1
        if self.fail_on == "open":
            raise OSError("open failed")
        return next(self.open_results, False)

    def empty(self) -> None:
        self.empty_calls += 1
        if self.fail_on == "empty":
            raise OSError("empty failed")

    def set_unicode(self, handle: int) -> None:
        self.set_calls.append(handle)
        if self.fail_on == "set":
            raise OSError("set failed")

    def close(self) -> None:
        self.close_calls += 1
        if self.fail_on == "close":
            raise OSError("close failed")

    def free(self, handle: int) -> None:
        self.freed.append(handle)


class ClipboardUser32:
    def __init__(self) -> None:
        self.OpenClipboard = Function(1)
        self.EmptyClipboard = Function(1)
        self.SetClipboardData = Function(789)
        self.CloseClipboard = Function(1)


class ClipboardKernel32:
    def __init__(self) -> None:
        self.GlobalAlloc = Function(456)
        self.GlobalLock = Function(1234)
        self.GlobalUnlock = Function(1)
        self.GlobalFree = Function(0)


def native_clipboard_backend(
    monkeypatch: Any,
) -> tuple[Any, ClipboardUser32, ClipboardKernel32, list[tuple[Any, ...]]]:
    user32 = ClipboardUser32()
    kernel32 = ClipboardKernel32()
    copied: list[tuple[Any, ...]] = []
    libraries = {"user32": user32, "kernel32": kernel32}
    monkeypatch.setattr(
        ctypes,
        "WinDLL",
        lambda name, **_kwargs: libraries[name],
    )
    monkeypatch.setattr(ctypes, "set_last_error", lambda _value: None)
    monkeypatch.setattr(ctypes, "get_last_error", lambda: 0)
    monkeypatch.setattr(ctypes, "memmove", lambda *args: copied.append(args))
    return windows_integration._Win32ClipboardBackend(), user32, kernel32, copied


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


def test_activation_event_retains_signals_waits_and_closes() -> None:
    kernel32 = ActivationKernel32()
    activation = InstanceActivationEvent(kernel32=kernel32)

    assert activation.create()
    assert activation.signal()
    assert activation.wait(250)
    kernel32.WaitForSingleObject.result = WAIT_TIMEOUT
    assert not activation.wait(0)
    activation.close()
    activation.close()

    assert len(kernel32.CreateEventW.calls) == 1
    assert kernel32.CreateEventW.calls[0][3] == ACTIVATION_EVENT_NAME
    assert len(kernel32.SetEvent.calls) == 1
    assert len(kernel32.WaitForSingleObject.calls) == 2
    assert len(kernel32.CloseHandle.calls) == 1


def test_activation_event_without_native_backend_is_a_safe_noop() -> None:
    activation = InstanceActivationEvent(kernel32=None)
    activation.kernel32 = None

    assert not activation.create()
    assert not activation.signal()
    assert not activation.wait(0)
    activation.close()


def test_activation_event_surfaces_native_failures(monkeypatch: Any) -> None:
    monkeypatch.setattr(ctypes, "get_last_error", lambda: 5)

    create_failure = ActivationKernel32()
    create_failure.CreateEventW.result = 0
    with pytest.raises(OSError, match="create"):
        InstanceActivationEvent(kernel32=create_failure).create()

    signal_failure = ActivationKernel32()
    signal_failure.SetEvent.result = 0
    activation = InstanceActivationEvent(kernel32=signal_failure)
    with pytest.raises(OSError, match="signal"):
        activation.signal()
    activation.close()

    wait_failure = ActivationKernel32()
    wait_failure.WaitForSingleObject.result = 0xFFFFFFFF
    activation = InstanceActivationEvent(kernel32=wait_failure)
    with pytest.raises(ValueError, match="negative"):
        activation.wait(-1)
    with pytest.raises(OSError, match="wait"):
        activation.wait(1)
    activation.close()


def test_activation_event_context_manager_closes_handle() -> None:
    kernel32 = ActivationKernel32()

    with InstanceActivationEvent(kernel32=kernel32) as activation:
        assert activation.handle == 456

    assert activation.handle is None
    assert len(kernel32.CloseHandle.calls) == 1


def test_clipboard_transfers_handle_ownership_after_success() -> None:
    backend = ClipboardBackend()

    copy_text_to_clipboard("C:/Ứng dụng/cslol-manager", backend=backend)

    assert backend.allocated == ["C:/Ứng dụng/cslol-manager"]
    assert backend.open_calls == 1
    assert backend.empty_calls == 1
    assert backend.set_calls == [789]
    assert backend.close_calls == 1
    assert backend.freed == []


def test_clipboard_retries_busy_backend_before_transfer(monkeypatch: Any) -> None:
    backend = ClipboardBackend((False, False, True))
    delays: list[float] = []
    monkeypatch.setattr(
        "league_skin_manager.windows_integration.time.sleep",
        delays.append,
    )

    copy_text_to_clipboard(
        "manager path",
        backend=backend,
        attempts=4,
        retry_delay_seconds=0.125,
    )

    assert backend.open_calls == 3
    assert delays == [0.125, 0.125]
    assert backend.close_calls == 1
    assert backend.freed == []


def test_clipboard_busy_failure_releases_untransferred_handle() -> None:
    backend = ClipboardBackend((False, False, False))

    with pytest.raises(OSError, match="clipboard is busy"):
        copy_text_to_clipboard(
            "manager path",
            backend=backend,
            attempts=3,
            retry_delay_seconds=0,
        )

    assert backend.open_calls == 3
    assert backend.empty_calls == 0
    assert backend.close_calls == 0
    assert backend.freed == [789]


@pytest.mark.parametrize("failure", ("open", "empty", "set"))
def test_clipboard_errors_release_handle_before_ownership_transfer(failure: str) -> None:
    backend = ClipboardBackend(fail_on=failure)

    with pytest.raises(OSError, match=f"{failure} failed"):
        copy_text_to_clipboard("manager path", backend=backend)

    assert backend.close_calls == (0 if failure == "open" else 1)
    assert backend.freed == [789]


@pytest.mark.parametrize(
    ("text", "attempts", "retry_delay_seconds", "error", "message"),
    (
        (object(), 1, 0.0, TypeError, "must be a string"),
        ("bad\x00text", 1, 0.0, ValueError, "NUL"),
        ("text", 0, 0.0, ValueError, "attempts must be positive"),
        ("text", 1, -0.1, ValueError, "cannot be negative"),
    ),
)
def test_clipboard_rejects_invalid_input_before_allocating(
    text: Any,
    attempts: int,
    retry_delay_seconds: float,
    error: type[Exception],
    message: str,
) -> None:
    backend = ClipboardBackend()

    with pytest.raises(error, match=message):
        copy_text_to_clipboard(
            text,
            backend=backend,
            attempts=attempts,
            retry_delay_seconds=retry_delay_seconds,
        )

    assert backend.allocated == []


def test_native_clipboard_backend_allocates_copies_and_calls_win32(monkeypatch: Any) -> None:
    backend, user32, kernel32, copied = native_clipboard_backend(monkeypatch)
    payload = "Ứ".encode("utf-16-le") + b"\x00\x00"

    handle = backend.allocate_unicode("Ứ")

    assert handle == 456
    assert kernel32.GlobalAlloc.calls == [(windows_integration.GMEM_MOVEABLE, len(payload))]
    assert kernel32.GlobalLock.calls[0][0].value == 456
    assert copied == [(1234, payload, len(payload))]
    assert kernel32.GlobalUnlock.calls[0][0].value == 456

    assert backend.open()
    backend.empty()
    backend.set_unicode(handle)
    backend.close()
    backend.free(handle)

    assert user32.OpenClipboard.calls == [(None,)]
    assert user32.EmptyClipboard.calls == [()]
    assert user32.SetClipboardData.calls[0][0] == windows_integration.CF_UNICODETEXT
    assert user32.SetClipboardData.calls[0][1].value == 456
    assert user32.CloseClipboard.calls == [()]
    assert kernel32.GlobalFree.calls[0][0].value == 456


def test_native_clipboard_backend_releases_failed_lock(monkeypatch: Any) -> None:
    backend, _user32, kernel32, _copied = native_clipboard_backend(monkeypatch)
    kernel32.GlobalLock.result = 0
    monkeypatch.setattr(ctypes, "get_last_error", lambda: 5)

    with pytest.raises(OSError):
        backend.allocate_unicode("text")

    assert kernel32.GlobalFree.calls[0][0].value == 456


def test_native_clipboard_backend_surfaces_allocation_failure(monkeypatch: Any) -> None:
    backend, _user32, kernel32, _copied = native_clipboard_backend(monkeypatch)
    kernel32.GlobalAlloc.result = 0
    monkeypatch.setattr(ctypes, "get_last_error", lambda: 5)

    with pytest.raises(OSError):
        backend.allocate_unicode("text")

    assert kernel32.GlobalLock.calls == []
    assert kernel32.GlobalFree.calls == []


def test_native_clipboard_backend_releases_failed_unlock(monkeypatch: Any) -> None:
    backend, _user32, kernel32, _copied = native_clipboard_backend(monkeypatch)
    kernel32.GlobalUnlock.result = 0
    monkeypatch.setattr(ctypes, "get_last_error", lambda: 5)

    with pytest.raises(OSError):
        backend.allocate_unicode("text")

    assert kernel32.GlobalFree.calls[0][0].value == 456


def test_native_clipboard_backend_surfaces_empty_and_set_failures(monkeypatch: Any) -> None:
    backend, user32, _kernel32, _copied = native_clipboard_backend(monkeypatch)
    user32.OpenClipboard.result = 0
    assert not backend.open()

    user32.EmptyClipboard.result = 0
    with pytest.raises(OSError, match="empty the clipboard"):
        backend.empty()

    user32.SetClipboardData.result = 0
    with pytest.raises(OSError, match="place text on the clipboard"):
        backend.set_unicode(456)


def test_startup_command_quotes_absolute_executable(tmp_path: Path) -> None:
    executable = tmp_path / "folder with spaces" / "app.exe"
    assert StartupRegistration._command(executable) == f'"{executable.resolve()}" --background'
    assert StartupRegistration._command(executable, ()) == f'"{executable.resolve()}"'


def test_open_path_uses_windows_shell(monkeypatch: Any, tmp_path: Path) -> None:
    opened: list[str] = []
    monkeypatch.setattr("league_skin_manager.windows_integration.os.name", "nt")
    monkeypatch.setattr(
        "league_skin_manager.windows_integration.os.startfile",
        lambda value: opened.append(value),
    )

    open_path(tmp_path)

    assert opened == [str(tmp_path.resolve())]


class Process:
    def __init__(self, name: str | None = None, error: Exception | None = None) -> None:
        self.info = {"name": name}
        self.error = error


def test_process_running_lookup_is_case_insensitive(monkeypatch: Any) -> None:
    scans: list[tuple[str, ...]] = []

    def processes(fields: list[str]) -> list[Process]:
        scans.append(tuple(fields))
        return [Process(None), Process("LTK-MANAGER.EXE")]

    monkeypatch.setattr(
        "league_skin_manager.windows_integration.psutil.process_iter",
        processes,
    )
    assert ProcessLauncher.is_running("ltk-manager.exe")
    assert not ProcessLauncher.is_running("different.exe")
    assert ProcessLauncher.is_any_running(("cslol-manager.exe", "LTK-MANAGER.exe"))
    assert not ProcessLauncher.is_any_running(())
    assert scans == [("name",), ("name",), ("name",)]


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


def test_running_under_matches_exact_owned_process_paths(monkeypatch: Any, tmp_path: Path) -> None:
    class OwnedProcess:
        def __init__(self, executable: Path | None) -> None:
            self.info = {"exe": str(executable) if executable is not None else None}

    manager_dir = tmp_path / "manager"
    owned = manager_dir / "tools" / "mod-tools.exe"
    unrelated = tmp_path / "elsewhere" / "mod-tools.exe"
    monkeypatch.setattr(
        "league_skin_manager.windows_integration.psutil.process_iter",
        lambda _fields: [OwnedProcess(unrelated), OwnedProcess(owned), OwnedProcess(None)],
    )

    assert ProcessLauncher.is_running_under(manager_dir)
    assert not ProcessLauncher.is_running_under(tmp_path / "different")


def test_running_under_blocks_known_owned_names_when_executable_is_inaccessible(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    class Process:
        def __init__(self, name: str | None, executable: str | None) -> None:
            self.info = {"name": name, "exe": executable}

    processes = [
        Process("unrelated.exe", None),
        Process("MOD-TOOLS.EXE", None),
    ]
    monkeypatch.setattr(
        "league_skin_manager.windows_integration.psutil.process_iter",
        lambda _fields: processes,
    )

    assert ProcessLauncher.is_running_under(tmp_path / "manager")


def test_running_under_does_not_block_same_known_name_at_accessible_unowned_path(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    class Process:
        info = {"name": "mod-tools.exe", "exe": str(tmp_path / "other" / "mod-tools.exe")}

    monkeypatch.setattr(
        "league_skin_manager.windows_integration.psutil.process_iter",
        lambda _fields: [Process()],
    )

    assert not ProcessLauncher.is_running_under(tmp_path / "manager")
