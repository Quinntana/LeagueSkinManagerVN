from __future__ import annotations

import ctypes
import logging
import os
import sys
from collections.abc import Callable
from typing import Any

import pytest

import league_skin_manager.process_monitor as process_monitor
from league_skin_manager.process_monitor import LeagueProcessMonitor, WindowsProcessLookup


class Lookup:
    def __init__(self, values: list[int | None | Exception]) -> None:
        self.values = values
        self.calls: list[str] = []

    def find_pid(self, name: str) -> int | None:
        self.calls.append(name)
        value = self.values.pop(0)
        if isinstance(value, Exception):
            raise value
        return value


class NativeFunction:
    def __init__(self, callback: Callable[..., int]) -> None:
        self.callback = callback
        self.argtypes: object = None
        self.restype: object = None

    def __call__(self, *args: Any) -> int:
        return self.callback(*args)


class FakeToolhelpBackend:
    def __init__(
        self,
        entries: list[tuple[int, str]],
        *,
        first_error: int | None = None,
        next_error: int | None = None,
    ) -> None:
        self.entries = entries
        self.first_error = first_error
        self.next_error = next_error
        self.last_error = 0
        self.index = 0
        self.closed: list[int] = []
        self.CreateToolhelp32Snapshot = NativeFunction(lambda *_args: 77)
        self.Process32FirstW = NativeFunction(self._first)
        self.Process32NextW = NativeFunction(self._next)
        self.CloseHandle = NativeFunction(self._close)

    def _write_entry(self, pointer: object) -> None:
        entry = ctypes.cast(
            pointer,
            ctypes.POINTER(process_monitor._PROCESSENTRY32W),
        ).contents
        entry.th32ProcessID, entry.szExeFile = self.entries[self.index]

    def _first(self, _snapshot: object, pointer: object) -> int:
        if self.first_error is not None:
            self.last_error = self.first_error
            return 0
        if not self.entries:
            self.last_error = process_monitor.ERROR_NO_MORE_FILES
            return 0
        self.index = 0
        self._write_entry(pointer)
        return 1

    def _next(self, _snapshot: object, pointer: object) -> int:
        if self.next_error is not None:
            self.last_error = self.next_error
            return 0
        self.index += 1
        if self.index >= len(self.entries):
            self.last_error = process_monitor.ERROR_NO_MORE_FILES
            return 0
        self._write_entry(pointer)
        return 1

    def _close(self, snapshot: int) -> int:
        self.closed.append(snapshot)
        return 1


def native_lookup(
    monkeypatch: Any,
    backend: FakeToolhelpBackend,
    fallback: Lookup,
) -> WindowsProcessLookup:
    monkeypatch.setattr(process_monitor, "_get_last_error", lambda: backend.last_error)
    monkeypatch.setattr(
        process_monitor,
        "_set_last_error",
        lambda value: setattr(backend, "last_error", value),
    )
    return WindowsProcessLookup(kernel32=backend, fallback=fallback)


class StopAfterWaits:
    def __init__(self, waits: int) -> None:
        self.waits = waits
        self.count = 0

    def is_set(self) -> bool:
        return self.count >= self.waits

    def wait(self, _seconds: float) -> bool:
        self.count += 1
        return self.is_set()


def test_monitor_reports_only_process_transitions() -> None:
    lookup = Lookup([None, None, 10, 10, 20, None])
    changed: list[int | None] = []
    monitor = LeagueProcessMonitor(lookup, "LeagueClient.exe", 0.1, logging.getLogger("test"))
    monitor.run(StopAfterWaits(6), changed.append)  # type: ignore[arg-type]
    assert changed == [None, 10, 20, None]


def test_monitor_recovers_from_lookup_error() -> None:
    lookup = Lookup([OSError("transient"), 42])
    changed: list[int | None] = []
    monitor = LeagueProcessMonitor(lookup, "LeagueClient.exe", 0.1, logging.getLogger("test"))
    monitor.run(StopAfterWaits(2), changed.append)  # type: ignore[arg-type]
    assert changed == [42]


def test_windows_lookup_falls_back_without_native_backend() -> None:
    fallback = Lookup([321])
    lookup = WindowsProcessLookup(kernel32=None, fallback=fallback)
    lookup._kernel32 = None

    assert lookup.find_pid("LeagueClient.exe") == 321


def test_native_lookup_matches_case_insensitively_and_closes_snapshot(
    monkeypatch: Any,
) -> None:
    backend = FakeToolhelpBackend([(11, "other.exe"), (22, "LEAGUECLIENT.EXE")])
    fallback = Lookup([AssertionError("fallback should not run")])
    lookup = native_lookup(monkeypatch, backend, fallback)

    assert lookup.find_pid("LeagueClient.exe") == 22
    assert fallback.calls == []
    assert backend.closed == [77]


def test_native_first_error_closes_snapshot_and_uses_fallback(monkeypatch: Any) -> None:
    backend = FakeToolhelpBackend([], first_error=5)
    fallback = Lookup([321])
    lookup = native_lookup(monkeypatch, backend, fallback)

    assert lookup.find_pid("LeagueClient.exe") == 321
    assert fallback.calls == ["LeagueClient.exe"]
    assert backend.closed == [77]


def test_native_next_error_closes_snapshot_and_uses_fallback(monkeypatch: Any) -> None:
    backend = FakeToolhelpBackend([(11, "other.exe")], next_error=5)
    fallback = Lookup([654])
    lookup = native_lookup(monkeypatch, backend, fallback)

    assert lookup.find_pid("LeagueClient.exe") == 654
    assert fallback.calls == ["LeagueClient.exe"]
    assert backend.closed == [77]


def test_native_end_of_snapshot_is_not_treated_as_an_error(monkeypatch: Any) -> None:
    backend = FakeToolhelpBackend([(11, "other.exe")])
    fallback = Lookup([AssertionError("fallback should not run")])
    lookup = native_lookup(monkeypatch, backend, fallback)

    assert lookup.find_pid("LeagueClient.exe") is None
    assert fallback.calls == []
    assert backend.closed == [77]


@pytest.mark.skipif(os.name != "nt", reason="Toolhelp is Windows-only")
def test_native_lookup_finds_a_python_process_on_windows() -> None:
    lookup = WindowsProcessLookup()

    assert lookup.find_pid(sys.executable.rsplit("\\", maxsplit=1)[-1]) is not None
