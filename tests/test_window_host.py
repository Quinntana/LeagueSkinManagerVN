from __future__ import annotations

import logging
from threading import Event
from typing import Any

import pytest

from league_skin_manager.window_host import WindowHost


class FakeWindow:
    def __init__(self, *, fail_run: bool = False) -> None:
        self.running = Event()
        self.release = Event()
        self.shows = 0
        self.stops = 0
        self.runs = 0
        self._fail_run = fail_run

    def run(self) -> None:
        self.runs += 1
        self.running.set()
        if self._fail_run:
            raise RuntimeError("window crashed")
        self.release.wait(2.0)

    def show(self) -> None:
        self.shows += 1

    def stop(self) -> None:
        self.stops += 1
        self.release.set()


def host(factory: Any, **options: Any) -> tuple[WindowHost, list[tuple[str, str]]]:
    failures: list[tuple[str, str]] = []
    value = WindowHost(
        factory,
        title="Test window",
        failure_sink=lambda title, message: failures.append((title, message)),
        logger=logging.getLogger("test.window_host"),
        **options,
    )
    return value, failures


def test_the_window_is_only_created_on_first_request() -> None:
    created: list[FakeWindow] = []

    def factory() -> FakeWindow:
        window = FakeWindow()
        created.append(window)
        return window

    value, failures = host(factory)

    assert value.is_running is False
    assert created == []

    assert value.show() is True
    assert created[0].running.wait(2.0)
    assert value.is_running is True
    assert created[0].runs == 1

    # A second request raises the existing window instead of building another.
    assert value.show() is True
    assert len(created) == 1
    assert created[0].shows == 1
    assert failures == []
    assert value.stop(2.0) is True
    assert value.is_running is False


def test_a_factory_failure_is_reported_and_contained() -> None:
    def factory() -> FakeWindow:
        raise RuntimeError("no display")

    value, failures = host(factory)

    assert value.show() is False
    assert value.is_running is False
    assert failures[-1][0] == "Test window"
    assert "no display" in failures[-1][1]


def test_a_crash_inside_the_window_is_reported() -> None:
    window = FakeWindow(fail_run=True)
    value, failures = host(lambda: window)

    assert value.show() is True
    assert window.running.wait(2.0)
    assert value.stop(2.0) is True
    assert any("closed unexpectedly" in message for _title, message in failures)


def test_stop_is_safe_before_anything_was_created() -> None:
    value, failures = host(lambda: FakeWindow())

    assert value.stop(1.0) is True
    assert failures == []

    # After stopping, the host refuses to build the window.
    assert value.show() is False


def test_stop_requires_a_positive_timeout() -> None:
    value, _failures = host(lambda: FakeWindow())

    with pytest.raises(ValueError, match="positive"):
        value.stop(0)


def test_a_show_failure_on_an_existing_window_is_reported() -> None:
    class Stubborn(FakeWindow):
        def show(self) -> None:
            raise RuntimeError("cannot raise")

    window = Stubborn()
    value, failures = host(lambda: window)

    assert value.show() is True
    assert window.running.wait(2.0)

    assert value.show() is False
    assert "cannot raise" in failures[-1][1]
    assert value.stop(2.0) is True
