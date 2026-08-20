"""Tests for the cooldown package's public boundary.

The six functions the shell is allowed to call, driven with a fake window and
host so no Tk interpreter is involved. What matters here is which of them
create, which merely show, and which destroy -- getting that wrong is what
crashed a live match.
"""

from __future__ import annotations

from typing import Any

import pytest

from league_skin_manager import cooldown


class FakeWindow:
    def __init__(self, *, visible: bool = True) -> None:
        self.visible = visible
        self.hidden: list[bool] = []
        self.shown = 0
        self.display: list[tuple[float, float]] = []

    @property
    def is_visible(self) -> bool:
        return self.visible

    def hide(self) -> None:
        self.visible = False
        self.hidden.append(True)

    def show(self) -> None:
        self.visible = True
        self.shown += 1

    def set_display(self, *, opacity: float, scale: float) -> None:
        self.display.append((opacity, scale))


class FakeHost:
    def __init__(self, window: FakeWindow, *, running: bool = True) -> None:
        self.window = window
        self.running = running
        self.stopped = 0
        self.shows = 0

    @property
    def is_running(self) -> bool:
        return self.running

    def show(self) -> bool:
        self.shows += 1
        self.window.show()
        return True

    def stop(self, _timeout: float = 5.0) -> bool:
        self.stopped += 1
        self.running = False
        return True


@pytest.fixture
def session(monkeypatch: pytest.MonkeyPatch) -> Any:
    """Install a fake session and always tear it down."""

    window = FakeWindow()
    host = FakeHost(window)
    monkeypatch.setattr(cooldown, "_window", window, raising=False)
    monkeypatch.setattr(cooldown, "_host", host, raising=False)
    yield {"window": window, "host": host}
    monkeypatch.setattr(cooldown, "_window", None, raising=False)
    monkeypatch.setattr(cooldown, "_host", None, raising=False)


# --- what each function actually does -------------------------------------


def test_opening_an_already_visible_board_does_nothing(session: Any) -> None:
    """No route may produce a second board."""

    assert cooldown.open_panel() is True
    assert session["host"].shows == 0, "nothing may be shown or built again"
    assert session["window"].shown == 0


def test_opening_a_hidden_board_shows_it_without_rebuilding(session: Any) -> None:
    session["window"].visible = False

    assert cooldown.open_panel() is True

    assert session["host"].shows == 1
    assert session["host"].stopped == 0, "showing must never tear anything down"


def test_closing_hides_and_keeps_the_session(session: Any) -> None:
    assert cooldown.close_panel() is True

    assert session["window"].hidden == [True]
    assert session["host"].stopped == 0, "the match is not over"
    assert cooldown.is_open() is True


def test_releasing_ends_the_session(session: Any) -> None:
    assert cooldown.release_panel() is True

    assert session["host"].stopped == 1
    assert cooldown.is_open() is False


def test_releasing_twice_is_harmless(session: Any) -> None:
    cooldown.release_panel()
    assert cooldown.release_panel() is True


def test_closing_without_a_session_is_harmless() -> None:
    assert cooldown.close_panel() is True


# --- existing versus on screen --------------------------------------------


def test_a_hidden_board_still_counts_as_open(session: Any) -> None:
    """The distinction the shell needs: hidden is not gone."""

    cooldown.close_panel()

    assert cooldown.is_open() is True
    assert cooldown.is_visible() is False


def test_a_visible_board_is_both(session: Any) -> None:
    assert cooldown.is_open() is True
    assert cooldown.is_visible() is True


def test_nothing_is_visible_after_release(session: Any) -> None:
    cooldown.release_panel()
    assert cooldown.is_visible() is False


def test_a_dead_host_reads_as_closed(session: Any) -> None:
    session["host"].running = False
    assert cooldown.is_open() is False
    assert cooldown.is_visible() is False


# --- failures never reach the tray ----------------------------------------


def test_a_raising_hide_is_reported_not_raised(session: Any) -> None:
    def explode() -> None:
        raise OSError("the window went away")

    session["window"].hide = explode  # type: ignore[method-assign]

    assert cooldown.close_panel() is False


def test_display_settings_reach_a_live_window(session: Any) -> None:
    assert cooldown.apply_display(0.35, 0.7) is True
    assert session["window"].display == [(0.35, 0.7)]


def test_display_settings_are_a_no_op_without_a_session() -> None:
    assert cooldown.apply_display(0.35, 0.7) is False
