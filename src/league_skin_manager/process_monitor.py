"""Cooperative League Client process monitoring."""

from __future__ import annotations

import logging
from collections.abc import Callable
from threading import Event
from typing import Protocol

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
