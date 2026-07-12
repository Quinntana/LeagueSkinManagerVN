from __future__ import annotations

import logging

from league_skin_manager.process_monitor import LeagueProcessMonitor


class Lookup:
    def __init__(self, values: list[int | None | Exception]) -> None:
        self.values = values

    def find_pid(self, _name: str) -> int | None:
        value = self.values.pop(0)
        if isinstance(value, Exception):
            raise value
        return value


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
