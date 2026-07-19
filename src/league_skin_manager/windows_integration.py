"""Narrow Windows integration boundary for instance/startup/process handling."""

from __future__ import annotations

import ctypes
import logging
import os
import subprocess
import sys
import time
from collections.abc import Collection
from contextlib import suppress
from ctypes import wintypes
from pathlib import Path
from typing import Any, Protocol

import psutil

from .config import APP_NAME, MANAGER_PROCESS_NAMES

ERROR_ALREADY_EXISTS = 183
LEGACY_MUTEX_NAME = "LeagueSkinManagerVN_Mutex_v1"
MUTEX_NAME = "Local\\LeagueSkinManagerVN_Mutex_v2"
MUTEX_NAMES = (LEGACY_MUTEX_NAME, MUTEX_NAME)
ACTIVATION_EVENT_NAME = "Local\\LeagueSkinManagerVN_Activate_v1"
WAIT_OBJECT_0 = 0x00000000
WAIT_TIMEOUT = 0x00000102
CF_UNICODETEXT = 13
GMEM_MOVEABLE = 0x0002


class ClipboardBackend(Protocol):
    """Minimal ownership-aware boundary around the Win32 clipboard."""

    def allocate_unicode(self, text: str) -> int: ...

    def open(self) -> bool: ...

    def empty(self) -> None: ...

    def set_unicode(self, handle: int) -> None: ...

    def close(self) -> None: ...

    def free(self, handle: int) -> None: ...


class _Win32ClipboardBackend:
    def __init__(self) -> None:
        self._user32 = ctypes.WinDLL("user32", use_last_error=True)
        self._kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

        self._user32.OpenClipboard.argtypes = [wintypes.HWND]
        self._user32.OpenClipboard.restype = wintypes.BOOL
        self._user32.EmptyClipboard.argtypes = []
        self._user32.EmptyClipboard.restype = wintypes.BOOL
        self._user32.SetClipboardData.argtypes = [wintypes.UINT, wintypes.HANDLE]
        self._user32.SetClipboardData.restype = wintypes.HANDLE
        self._user32.CloseClipboard.argtypes = []
        self._user32.CloseClipboard.restype = wintypes.BOOL

        self._kernel32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
        self._kernel32.GlobalAlloc.restype = wintypes.HANDLE
        self._kernel32.GlobalLock.argtypes = [wintypes.HANDLE]
        self._kernel32.GlobalLock.restype = wintypes.LPVOID
        self._kernel32.GlobalUnlock.argtypes = [wintypes.HANDLE]
        self._kernel32.GlobalUnlock.restype = wintypes.BOOL
        self._kernel32.GlobalFree.argtypes = [wintypes.HANDLE]
        self._kernel32.GlobalFree.restype = wintypes.HANDLE

    def allocate_unicode(self, text: str) -> int:
        payload = text.encode("utf-16-le") + b"\x00\x00"
        ctypes.set_last_error(0)
        handle = self._kernel32.GlobalAlloc(GMEM_MOVEABLE, len(payload))
        if not handle:
            raise _clipboard_error("allocate clipboard memory")
        native_handle = int(handle)
        pointer = self._kernel32.GlobalLock(wintypes.HANDLE(native_handle))
        if not pointer:
            self.free(native_handle)
            raise _clipboard_error("lock clipboard memory")
        try:
            ctypes.memmove(pointer, payload, len(payload))
        finally:
            ctypes.set_last_error(0)
            unlocked = self._kernel32.GlobalUnlock(wintypes.HANDLE(native_handle))
            if not unlocked and ctypes.get_last_error():
                self.free(native_handle)
                raise _clipboard_error("unlock clipboard memory")
        return native_handle

    def open(self) -> bool:
        ctypes.set_last_error(0)
        return bool(self._user32.OpenClipboard(None))

    def empty(self) -> None:
        ctypes.set_last_error(0)
        if not self._user32.EmptyClipboard():
            raise _clipboard_error("empty the clipboard")

    def set_unicode(self, handle: int) -> None:
        ctypes.set_last_error(0)
        if not self._user32.SetClipboardData(CF_UNICODETEXT, wintypes.HANDLE(handle)):
            raise _clipboard_error("place text on the clipboard")

    def close(self) -> None:
        self._user32.CloseClipboard()

    def free(self, handle: int) -> None:
        self._kernel32.GlobalFree(wintypes.HANDLE(handle))


def _clipboard_error(action: str) -> OSError:
    error = ctypes.get_last_error()
    if error:
        return ctypes.WinError(error)
    return OSError(f"Could not {action}")


class SingleInstanceMutex:
    def __init__(self, name: str | None = None, kernel32: Any | None = None) -> None:
        self.names = (name,) if name is not None else MUTEX_NAMES
        self.kernel32 = kernel32
        self.handle: int | None = None
        self._handles: list[int] = []
        if self.kernel32 is None and os.name == "nt":
            self.kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    def acquire(self) -> bool:
        if self.kernel32 is None:
            return True
        if self._handles:
            return True
        create_mutex = self.kernel32.CreateMutexW
        create_mutex.argtypes = [wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR]
        create_mutex.restype = wintypes.HANDLE
        for name in self.names:
            ctypes.set_last_error(0)
            handle = create_mutex(None, False, name)
            if not handle:
                self.release()
                return False
            native_handle = int(handle)
            self._handles.append(native_handle)
            self.handle = self._handles[0]
            if ctypes.get_last_error() == ERROR_ALREADY_EXISTS:
                self.release()
                return False
        return True

    def release(self) -> None:
        handles = tuple(reversed(self._handles))
        self._handles.clear()
        if not handles or self.kernel32 is None:
            self.handle = None
            return
        self.handle = None
        for handle in handles:
            with suppress(Exception):
                self.kernel32.CloseHandle(wintypes.HANDLE(handle))

    def __enter__(self) -> SingleInstanceMutex:
        if not self.acquire():
            raise RuntimeError("LeagueSkinManagerVN is already running")
        return self

    def __exit__(self, *_args: object) -> None:
        self.release()


class InstanceActivationEvent:
    """Auto-reset event used to ask the owning instance to show its window."""

    def __init__(self, name: str = ACTIVATION_EVENT_NAME, kernel32: Any | None = None) -> None:
        self.name = name
        self.kernel32 = kernel32
        self.handle: int | None = None
        if self.kernel32 is None and os.name == "nt":
            self.kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    def create(self) -> bool:
        """Create or open the named event and retain its native handle."""

        if self.handle is not None:
            return True
        if self.kernel32 is None:
            return False
        create_event = self.kernel32.CreateEventW
        create_event.argtypes = [
            wintypes.LPVOID,
            wintypes.BOOL,
            wintypes.BOOL,
            wintypes.LPCWSTR,
        ]
        create_event.restype = wintypes.HANDLE
        handle = create_event(None, False, False, self.name)
        if not handle:
            error = ctypes.get_last_error()
            raise OSError(error, "Could not create the application activation event")
        self.handle = int(handle)
        return True

    def signal(self) -> bool:
        """Wake the primary instance, retaining a signal until it begins waiting."""

        if not self.create() or self.kernel32 is None or self.handle is None:
            return False
        set_event = self.kernel32.SetEvent
        set_event.argtypes = [wintypes.HANDLE]
        set_event.restype = wintypes.BOOL
        if not set_event(wintypes.HANDLE(self.handle)):
            error = ctypes.get_last_error()
            raise OSError(error, "Could not signal the application activation event")
        return True

    def wait(self, timeout_milliseconds: int) -> bool:
        """Wait for activation, returning ``False`` when the timeout expires."""

        if timeout_milliseconds < 0:
            raise ValueError("timeout_milliseconds cannot be negative")
        if not self.create() or self.kernel32 is None or self.handle is None:
            return False
        wait_for_single = self.kernel32.WaitForSingleObject
        wait_for_single.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        wait_for_single.restype = wintypes.DWORD
        result = int(
            wait_for_single(
                wintypes.HANDLE(self.handle),
                wintypes.DWORD(timeout_milliseconds),
            )
        )
        if result == WAIT_OBJECT_0:
            return True
        if result == WAIT_TIMEOUT:
            return False
        error = ctypes.get_last_error()
        raise OSError(error, "Could not wait for the application activation event")

    def close(self) -> None:
        """Release the native event handle; safe to call more than once."""

        handle = self.handle
        self.handle = None
        if handle is None or self.kernel32 is None:
            return
        with suppress(Exception):
            self.kernel32.CloseHandle(wintypes.HANDLE(handle))

    def __enter__(self) -> InstanceActivationEvent:
        self.create()
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()


class StartupRegistration:
    KEY_PATH = r"Software\Microsoft\Windows\CurrentVersion\Run"

    def __init__(
        self,
        app_name: str = APP_NAME,
        arguments: tuple[str, ...] = ("--background",),
    ) -> None:
        self.app_name = app_name
        self.arguments = arguments

    @staticmethod
    def _command(executable: Path, arguments: tuple[str, ...] = ("--background",)) -> str:
        suffix = "" if not arguments else " " + " ".join(arguments)
        return f'"{executable.resolve()}"{suffix}'

    def is_enabled(self, executable: Path) -> bool:
        if os.name != "nt":
            return False
        import winreg

        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, self.KEY_PATH) as key:
                value, _kind = winreg.QueryValueEx(key, self.app_name)
            return str(value) == self._command(executable, self.arguments)
        except (FileNotFoundError, OSError):
            return False

    def set_enabled(self, executable: Path, enabled: bool) -> None:
        if os.name != "nt":
            return
        import winreg

        access = winreg.KEY_QUERY_VALUE | winreg.KEY_SET_VALUE
        with winreg.CreateKeyEx(
            winreg.HKEY_CURRENT_USER,
            self.KEY_PATH,
            0,
            access,
        ) as key:
            if enabled:
                winreg.SetValueEx(
                    key,
                    self.app_name,
                    0,
                    winreg.REG_SZ,
                    self._command(executable, self.arguments),
                )
            else:
                with suppress(FileNotFoundError):
                    winreg.DeleteValue(key, self.app_name)


class ProcessLauncher:
    def __init__(self, logger: logging.Logger) -> None:
        self.logger = logger

    @staticmethod
    def is_running(executable_name: str) -> bool:
        return ProcessLauncher.is_any_running((executable_name,))

    @staticmethod
    def is_any_running(executable_names: Collection[str]) -> bool:
        """Match several process names with one process-table snapshot."""

        expected = {name.casefold() for name in executable_names if name}
        if not expected:
            return False
        for process in psutil.process_iter(["name"]):
            try:
                name = process.info.get("name")
                if isinstance(name, str) and name.casefold() in expected:
                    return True
            except (psutil.Error, OSError):
                continue
        return False

    @staticmethod
    def is_running_under(
        directory: Path,
        known_executable_names: Collection[str] = MANAGER_PROCESS_NAMES,
    ) -> bool:
        """Return whether any process executable is inside an owned directory."""

        root = directory.resolve()
        expected_names = {name.casefold() for name in known_executable_names}
        for process in psutil.process_iter(["name", "exe"]):
            try:
                name = process.info.get("name")
                executable = process.info.get("exe")
            except (psutil.Error, OSError):
                continue
            known_name = isinstance(name, str) and name.casefold() in expected_names
            if not isinstance(executable, str) or not executable:
                if known_name:
                    return True
                continue
            try:
                resolved = Path(executable).resolve()
            except (OSError, ValueError):
                if known_name:
                    return True
                continue
            if resolved == root or resolved.is_relative_to(root):
                return True
        return False

    def launch(self, executable: Path) -> bool:
        if not executable.is_file():
            self.logger.warning("Executable is unavailable: %s", executable)
            return False
        try:
            creation_flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
            subprocess.Popen(
                [str(executable)],
                cwd=str(executable.parent),
                close_fds=True,
                creationflags=creation_flags,
            )
            self.logger.info("Launched %s", executable)
            return True
        except OSError:
            self.logger.exception("Could not launch %s", executable)
            return False


def running_executable() -> Path:
    return Path(sys.executable).resolve()


def copy_text_to_clipboard(
    text: str,
    *,
    backend: ClipboardBackend | None = None,
    attempts: int = 5,
    retry_delay_seconds: float = 0.04,
) -> None:
    """Copy Unicode text without creating Tk or a hidden application window."""

    if not isinstance(text, str):
        raise TypeError("clipboard text must be a string")
    if "\x00" in text:
        raise ValueError("clipboard text cannot contain a NUL character")
    if attempts < 1:
        raise ValueError("attempts must be positive")
    if retry_delay_seconds < 0:
        raise ValueError("retry_delay_seconds cannot be negative")
    if backend is None:
        if os.name != "nt":
            raise OSError("The Windows clipboard is unavailable")
        backend = _Win32ClipboardBackend()

    handle = backend.allocate_unicode(text)
    opened = False
    transferred = False
    try:
        for attempt in range(attempts):
            if backend.open():
                opened = True
                break
            if attempt + 1 < attempts and retry_delay_seconds:
                time.sleep(retry_delay_seconds)
        if not opened:
            raise OSError("The clipboard is busy; try the copy action again")

        backend.empty()
        backend.set_unicode(handle)
        transferred = True
    finally:
        if opened:
            backend.close()
        if not transferred:
            backend.free(handle)


def open_path(path: Path) -> None:
    """Open a local file or directory with the user's Windows shell."""

    if os.name != "nt":
        raise OSError("Opening paths is supported only on Windows")
    os.startfile(str(path.resolve()))
