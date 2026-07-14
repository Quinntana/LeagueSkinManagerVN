"""Cooperative League Client process monitoring."""

from __future__ import annotations

import ctypes
import logging
import os
from collections.abc import Callable
from ctypes import wintypes
from threading import Event
from typing import Any, Protocol

import psutil


class ProcessLookup(Protocol):
    def find_pid(self, executable_name: str) -> int | None: ...


class PsutilProcessLookup:
    def find_pid(self, executable_name: str) -> int | None:
        expected = executable_name.casefold()
        for process in psutil.process_iter(["pid", "name"]):
            try:
                name = process.info.get("name")
                if isinstance(name, str) and name.casefold() == expected:
                    pid = process.info.get("pid")
                    return int(pid) if pid is not None else None
            except (psutil.Error, OSError, ValueError):
                continue
        return None


TH32CS_SNAPPROCESS = 0x00000002
ERROR_NO_MORE_FILES = 18
MAX_PATH = 260


class _PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [
        ("dwSize", wintypes.DWORD),
        ("cntUsage", wintypes.DWORD),
        ("th32ProcessID", wintypes.DWORD),
        ("th32DefaultHeapID", ctypes.c_size_t),
        ("th32ModuleID", wintypes.DWORD),
        ("cntThreads", wintypes.DWORD),
        ("th32ParentProcessID", wintypes.DWORD),
        ("pcPriClassBase", wintypes.LONG),
        ("dwFlags", wintypes.DWORD),
        ("szExeFile", wintypes.WCHAR * MAX_PATH),
    ]


class WindowsProcessLookup:
    """Find a process using the cheap native Toolhelp snapshot API."""

    def __init__(
        self,
        kernel32: Any | None = None,
        fallback: ProcessLookup | None = None,
    ) -> None:
        if kernel32 is None and os.name == "nt":
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self._kernel32 = kernel32
        self._fallback = fallback or PsutilProcessLookup()

    def find_pid(self, executable_name: str) -> int | None:
        kernel32 = self._kernel32
        if kernel32 is None:
            return self._fallback.find_pid(executable_name)
        try:
            return self._find_pid_native(kernel32, executable_name)
        except OSError:
            return self._fallback.find_pid(executable_name)

    @staticmethod
    def _find_pid_native(kernel32: Any, executable_name: str) -> int | None:
        create_snapshot = kernel32.CreateToolhelp32Snapshot
        process_first = kernel32.Process32FirstW
        process_next = kernel32.Process32NextW
        close_handle = kernel32.CloseHandle
        create_snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
        create_snapshot.restype = wintypes.HANDLE
        process_first.argtypes = [wintypes.HANDLE, ctypes.POINTER(_PROCESSENTRY32W)]
        process_first.restype = wintypes.BOOL
        process_next.argtypes = [wintypes.HANDLE, ctypes.POINTER(_PROCESSENTRY32W)]
        process_next.restype = wintypes.BOOL
        close_handle.argtypes = [wintypes.HANDLE]
        close_handle.restype = wintypes.BOOL

        snapshot = create_snapshot(TH32CS_SNAPPROCESS, 0)
        invalid_handle = ctypes.c_void_p(-1).value
        if snapshot in (None, invalid_handle):
            error = _get_last_error()
            raise OSError(error, "Could not create a Windows process snapshot")
        expected = executable_name.casefold()
        entry = _PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(entry)
        try:
            _set_last_error(0)
            available = bool(process_first(snapshot, ctypes.byref(entry)))
            if not available:
                error = _get_last_error()
                if error == ERROR_NO_MORE_FILES:
                    return None
                raise OSError(error, "Could not read the Windows process snapshot")
            while available:
                if entry.szExeFile.casefold() == expected:
                    return int(entry.th32ProcessID)
                _set_last_error(0)
                available = bool(process_next(snapshot, ctypes.byref(entry)))
                if not available:
                    error = _get_last_error()
                    if error != ERROR_NO_MORE_FILES:
                        raise OSError(error, "Could not continue the Windows process snapshot")
        finally:
            close_handle(snapshot)
        return None


def _get_last_error() -> int:
    getter = getattr(ctypes, "get_last_error", None)
    return int(getter()) if getter is not None else 0


def _set_last_error(value: int) -> None:
    setter = getattr(ctypes, "set_last_error", None)
    if setter is not None:
        setter(value)


class LeagueProcessMonitor:
    def __init__(
        self,
        lookup: ProcessLookup,
        executable_name: str,
        poll_seconds: float,
        logger: logging.Logger,
    ) -> None:
        if poll_seconds <= 0:
            raise ValueError("poll_seconds must be positive")
        self.lookup = lookup
        self.executable_name = executable_name
        self.poll_seconds = poll_seconds
        self.logger = logger

    def run(self, stop_event: Event, changed: Callable[[int | None], None]) -> None:
        previous: int | None | object = object()
        while not stop_event.is_set():
            try:
                current = self.lookup.find_pid(self.executable_name)
                if current != previous:
                    changed(current)
                    previous = current
            except Exception:
                self.logger.exception("League process polling failed")
            stop_event.wait(self.poll_seconds)
