from __future__ import annotations

import time
from collections.abc import Callable
from threading import Event

from league_skin_manager.controller import AppState
from league_skin_manager.desktop_host import DesktopHost


class FakeDesktop:
    def __init__(self, *, run_error: Exception | None = None) -> None:
        self.run_error = run_error
        self.run_started = Event()
        self.release_run = Event()
        self.run_calls: list[bool] = []
        self.show_calls = 0
        self.stop_calls = 0
        self.migration_calls = 0
        self.status_updates: list[tuple[AppState, str]] = []
        self.ltk_updates: list[tuple[str, bool]] = []

    def run(self, *, show_on_start: bool = True) -> None:
        self.run_calls.append(show_on_start)
        self.run_started.set()
        if self.run_error is not None:
            raise self.run_error
        self.release_run.wait(5.0)

    def show(self) -> None:
        self.show_calls += 1

    def stop(self) -> None:
        self.stop_calls += 1
        self.release_run.set()

    def request_ltk_migration(self) -> None:
        self.migration_calls += 1

    def update_status(self, state: AppState, detail: str) -> None:
        self.status_updates.append((state, detail))

    def update_ltk_status(self, detail: str, *, migration_active: bool = False) -> None:
        self.ltk_updates.append((detail, migration_active))


def wait_until(predicate: Callable[[], bool], timeout_seconds: float = 1.0) -> bool:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


def test_status_updates_do_not_construct_the_optional_desktop() -> None:
    factory_calls: list[str] = []

    def factory() -> FakeDesktop:
        factory_calls.append("factory")
        return FakeDesktop()

    host = DesktopHost(factory)

    host.update_status(AppState.READY, "Ready - 1,920 skins")
    host.update_ltk_status("ready", migration_active=True)

    assert factory_calls == []
    assert not host.is_running


def test_show_constructs_one_desktop_and_reuses_it() -> None:
    desktop = FakeDesktop()
    factory_calls: list[str] = []

    def factory() -> FakeDesktop:
        factory_calls.append("factory")
        return desktop

    host = DesktopHost(factory)
    try:
        assert host.show()
        assert desktop.run_started.wait(1.0)

        assert host.show()

        assert factory_calls == ["factory"]
        assert desktop.run_calls == [True]
        assert desktop.show_calls == 1
        assert host.is_running
    finally:
        assert host.stop(timeout_seconds=1.0)


def test_first_migration_request_starts_hidden_desktop_and_dispatches() -> None:
    desktop = FakeDesktop()
    host = DesktopHost(lambda: desktop)
    try:
        assert host.request_ltk_migration()
        assert desktop.run_started.wait(1.0)

        assert desktop.run_calls == [False]
        assert desktop.migration_calls == 1
        assert desktop.show_calls == 0
    finally:
        assert host.stop(timeout_seconds=1.0)


def test_cached_statuses_are_replayed_before_the_desktop_runs() -> None:
    desktop = FakeDesktop()
    host = DesktopHost(lambda: desktop)
    host.update_status(AppState.OFFLINE_READY, "Ready offline - cached catalog")
    host.update_ltk_status("porting 7/12", migration_active=True)
    try:
        assert host.show()
        assert desktop.run_started.wait(1.0)

        assert desktop.status_updates == [
            (AppState.OFFLINE_READY, "Ready offline - cached catalog")
        ]
        assert desktop.ltk_updates == [("porting 7/12", True)]

        host.update_status(AppState.READY, "Ready - current")
        host.update_ltk_status("ready", migration_active=False)
        assert desktop.status_updates[-1] == (AppState.READY, "Ready - current")
        assert desktop.ltk_updates[-1] == ("ready", False)
    finally:
        assert host.stop(timeout_seconds=1.0)


def test_stop_requests_ui_shutdown_joins_thread_and_prevents_restart() -> None:
    desktop = FakeDesktop()
    host = DesktopHost(lambda: desktop)
    assert host.show()
    assert desktop.run_started.wait(1.0)

    assert host.stop(timeout_seconds=1.0)

    assert desktop.stop_calls == 1
    assert not host.is_running
    assert not host.show()


def test_factory_failure_is_reported_and_a_later_request_can_retry() -> None:
    desktop = FakeDesktop()
    attempts = 0
    failures: list[tuple[str, str]] = []

    def factory() -> FakeDesktop:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("Tk is unavailable")
        return desktop

    host = DesktopHost(
        factory,
        failure_sink=lambda title, message: failures.append((title, message)),
    )

    assert not host.show()
    assert attempts == 1
    assert failures == [
        (
            "LeagueSkinManagerVN window",
            "Could not open the optional skin library: Tk is unavailable",
        )
    ]

    try:
        assert host.show()
        assert desktop.run_started.wait(1.0)
        assert attempts == 2
    finally:
        assert host.stop(timeout_seconds=1.0)


def test_failed_initial_status_replay_releases_partial_desktop() -> None:
    class FailingDesktop(FakeDesktop):
        def update_status(self, state: AppState, detail: str) -> None:
            raise RuntimeError("status queue unavailable")

    desktop = FailingDesktop()
    failures: list[tuple[str, str]] = []
    host = DesktopHost(
        lambda: desktop,
        failure_sink=lambda title, message: failures.append((title, message)),
    )

    assert not host.show()
    assert desktop.stop_calls == 1
    assert failures == [
        (
            "LeagueSkinManagerVN window",
            "Could not open the optional skin library: status queue unavailable",
        )
    ]


def test_run_failure_is_reported_and_next_request_creates_a_fresh_desktop() -> None:
    first = FakeDesktop(run_error=RuntimeError("render loop crashed"))
    replacement = FakeDesktop()
    desktops = iter((first, replacement))
    failures: list[tuple[str, str]] = []
    failure_seen = Event()

    def report_failure(title: str, message: str) -> None:
        failures.append((title, message))
        failure_seen.set()

    host = DesktopHost(lambda: next(desktops), failure_sink=report_failure)

    assert host.show()
    assert first.run_started.wait(1.0)
    assert failure_seen.wait(1.0)
    assert wait_until(lambda: not host.is_running)
    assert failures == [
        (
            "LeagueSkinManagerVN window",
            "The optional skin library closed unexpectedly: render loop crashed",
        )
    ]

    try:
        assert host.show()
        assert replacement.run_started.wait(1.0)
        assert replacement.run_calls == [True]
    finally:
        assert host.stop(timeout_seconds=1.0)
