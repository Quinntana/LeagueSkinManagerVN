"""Tests for the League game-process watcher."""

from __future__ import annotations

import time
from typing import Any

import pytest

from league_skin_manager.config import LEAGUE_GAME_PROCESS_NAME
from league_skin_manager.process_watch import GameWatcher


class FakeLookup:
    """A process table under test control."""

    def __init__(self, running: bool = False) -> None:
        self.running = running
        self.queries: list[str] = []
        self.error: Exception | None = None

    def is_running(self, name: str) -> bool:
        self.queries.append(name)
        if self.error is not None:
            raise self.error
        return self.running


def make_watcher(**kwargs: Any) -> tuple[GameWatcher, FakeLookup, list[bool]]:
    lookup = FakeLookup()
    seen: list[bool] = []
    watcher = GameWatcher(seen.append, lookup=lookup, **kwargs)
    return watcher, lookup, seen


def test_it_watches_the_game_not_the_client() -> None:
    watcher, lookup, _ = make_watcher()
    watcher.poll_once()
    assert lookup.queries == [LEAGUE_GAME_PROCESS_NAME]
    assert lookup.queries[0] == "League of Legends.exe"


def test_starting_the_game_notifies_once() -> None:
    watcher, lookup, seen = make_watcher()
    lookup.running = True
    watcher.poll_once()
    watcher.poll_once()
    assert seen == [True], "steady state must not re-notify"


def test_ending_the_match_notifies() -> None:
    watcher, lookup, seen = make_watcher()
    lookup.running = True
    watcher.poll_once()
    lookup.running = False
    watcher.poll_once()
    assert seen == [True, False]


def test_no_notification_when_nothing_changes() -> None:
    watcher, _lookup, seen = make_watcher()
    watcher.poll_once()
    watcher.poll_once()
    assert seen == []


def test_match_active_tracks_the_process() -> None:
    watcher, lookup, _ = make_watcher()
    assert watcher.match_active is False
    lookup.running = True
    watcher.poll_once()
    assert watcher.match_active is True


def test_a_polling_failure_keeps_the_previous_state() -> None:
    watcher, lookup, seen = make_watcher()
    lookup.running = True
    watcher.poll_once()
    lookup.error = OSError("process table unavailable")
    assert watcher.poll_once() is True
    assert seen == [True], "a transient failure must not look like the game closing"


def test_a_raising_observer_does_not_stop_the_watch() -> None:
    lookup = FakeLookup()

    def explode(_running: bool) -> None:
        raise RuntimeError("observer is broken")

    watcher = GameWatcher(explode, lookup=lookup)
    lookup.running = True
    watcher.poll_once()  # must not raise
    lookup.running = False
    watcher.poll_once()


def test_the_thread_starts_and_stops() -> None:
    watcher, lookup, seen = make_watcher(poll_seconds=0.01)
    assert watcher.start() is True
    lookup.running = True

    deadline = time.time() + 3.0
    while time.time() < deadline and not seen:
        time.sleep(0.01)

    assert seen == [True]
    assert watcher.stop(timeout=3.0) is True


def test_starting_twice_is_refused() -> None:
    watcher, _lookup, _ = make_watcher(poll_seconds=0.01)
    watcher.start()
    try:
        assert watcher.start() is False
    finally:
        watcher.stop(timeout=3.0)


def test_stopping_without_starting_is_harmless() -> None:
    watcher, _lookup, _ = make_watcher()
    assert watcher.stop() is True


@pytest.mark.parametrize("poll", [0, -1])
def test_an_invalid_interval_is_rejected(poll: float) -> None:
    with pytest.raises(ValueError, match="poll_seconds must be positive"):
        GameWatcher(lambda _r: None, poll_seconds=poll)
