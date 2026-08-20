"""Tests for the Win32 overlay surface.

Every call is driven through an injected ``user32``/``kernel32`` double, so
these run anywhere and assert the two things that are easy to get silently
wrong: styling the correct window handle, and treating an unanswerable
foreground probe as "unknown" rather than as "the game is gone".
"""

from __future__ import annotations

from typing import Any

from league_skin_manager.cooldown.overlay import (
    GWL_EXSTYLE,
    OVERLAY_EXSTYLE,
    WS_EX_NOACTIVATE,
    OverlayWindow,
)

CHILD = 5900342
ROOT = 3473658
# What Tk actually leaves on a borderless topmost window, measured 2026-08-20.
TK_ROOT_EXSTYLE = 0x00080088


class FakeUser32:
    """A window table with a parent/child split, as Tk produces."""

    def __init__(self, *, styles: dict[int, int] | None = None, foreground: int = 0) -> None:
        self.styles = styles if styles is not None else {ROOT: TK_ROOT_EXSTYLE, CHILD: 0x4}
        self.foreground = foreground
        self.set_calls: list[tuple[int, int]] = []
        self.pos_calls: list[int] = []
        self.ancestor_calls: list[int] = []
        self.refuse_write = False

    def GetAncestor(self, hwnd: Any, _flags: int) -> int:  # noqa: N802
        handle = _value(hwnd)
        self.ancestor_calls.append(handle)
        return ROOT if handle == CHILD else handle

    def GetWindowLongW(self, hwnd: Any, index: int) -> int:  # noqa: N802
        assert index == GWL_EXSTYLE
        return self.styles.get(_value(hwnd), 0)

    def SetWindowLongW(self, hwnd: Any, index: int, value: int) -> int:  # noqa: N802
        assert index == GWL_EXSTYLE
        handle = _value(hwnd)
        self.set_calls.append((handle, value))
        if not self.refuse_write:
            self.styles[handle] = value
        return 0

    def SetWindowPos(self, hwnd: Any, *_args: Any) -> int:  # noqa: N802
        self.pos_calls.append(_value(hwnd))
        return 1

    def GetForegroundWindow(self) -> int:  # noqa: N802
        return self.foreground

    def GetWindowThreadProcessId(self, _hwnd: Any, pid: Any) -> int:  # noqa: N802
        pid._obj.value = 4242
        return 1


class FakeKernel32:
    def __init__(self, name: str | None = "League of Legends.exe", *, open_fails: bool = False):
        self.name = name
        self.open_fails = open_fails
        self.closed = 0

    def OpenProcess(self, *_args: Any) -> int:  # noqa: N802
        return 0 if self.open_fails else 77

    def QueryFullProcessImageNameW(self, _h: Any, _f: int, buffer: Any, size: Any) -> int:  # noqa: N802
        if self.name is None:
            return 0
        full = f"C:\\Games\\Riot Games\\League of Legends\\Game\\{self.name}"
        buffer.value = full
        size._obj.value = len(full)
        return 1

    def CloseHandle(self, _handle: Any) -> int:  # noqa: N802
        self.closed += 1
        return 1


def _value(handle: Any) -> int:
    return handle if isinstance(handle, int) else int(getattr(handle, "value", 0) or 0)


def make(**kwargs: Any) -> tuple[OverlayWindow, FakeUser32, FakeKernel32]:
    user32 = kwargs.pop("user32", None) or FakeUser32(**kwargs.pop("user32_kwargs", {}))
    kernel32 = kwargs.pop("kernel32", None) or FakeKernel32(**kwargs.pop("kernel32_kwargs", {}))
    return OverlayWindow(user32=user32, kernel32=kernel32), user32, kernel32


# --- the handle that matters ---------------------------------------------


def test_the_top_level_handle_is_resolved_from_the_tk_child() -> None:
    """Tk's winfo_id is a child window; styling it would silently do nothing."""

    overlay, user32, _ = make()
    assert overlay.root_handle(CHILD) == ROOT
    assert user32.ancestor_calls == [CHILD]


def test_styles_are_applied_to_the_root_not_the_child() -> None:
    overlay, user32, _ = make()
    assert overlay.apply(CHILD) is True
    assert [handle for handle, _ in user32.set_calls] == [ROOT]
    assert user32.styles[CHILD] == 0x4, "the child must be left alone"
    assert user32.styles[ROOT] & WS_EX_NOACTIVATE


def test_the_frame_is_refreshed_after_a_style_change() -> None:
    overlay, user32, _ = make()
    overlay.apply(CHILD)
    assert user32.pos_calls == [ROOT]


def test_applying_twice_does_not_rewrite_the_style() -> None:
    overlay, user32, _ = make()
    overlay.apply(CHILD)
    user32.set_calls.clear()
    user32.pos_calls.clear()
    assert overlay.apply(CHILD) is True
    assert user32.set_calls == []
    assert user32.pos_calls == []


def test_a_style_that_does_not_take_is_reported() -> None:
    """Read back, never assume: a refused write must not report success."""

    overlay, user32, _ = make()
    user32.refuse_write = True
    assert overlay.apply(CHILD) is False


def test_tk_already_supplies_topmost_and_toolwindow() -> None:
    """Only NOACTIVATE is this application's to add."""

    assert OVERLAY_EXSTYLE == WS_EX_NOACTIVATE


def test_a_missing_user32_is_survivable() -> None:
    overlay = OverlayWindow(user32=None, kernel32=None)
    assert overlay.available is False
    assert overlay.apply(CHILD) is False
    assert overlay.foreground_process() is None
    assert overlay.is_foreground("League of Legends.exe") is None


# --- foreground ------------------------------------------------------------


def test_the_foreground_process_is_reported_by_image_name() -> None:
    overlay, user32, _ = make()
    user32.foreground = 999
    assert overlay.foreground_process() == "League of Legends.exe"


def test_the_process_handle_is_always_closed() -> None:
    overlay, user32, kernel32 = make()
    user32.foreground = 999
    overlay.foreground_process()
    assert kernel32.closed == 1


def test_the_game_in_front_is_recognised_case_insensitively() -> None:
    overlay, user32, _ = make()
    user32.foreground = 999
    assert overlay.is_foreground("league of legends.exe") is True


def test_another_application_in_front_is_reported() -> None:
    overlay, user32, _ = make(kernel32=FakeKernel32("chrome.exe"))
    user32.foreground = 999
    assert overlay.is_foreground("League of Legends.exe") is False


def test_no_foreground_window_reads_as_unknown() -> None:
    """Unknown must not be mistaken for 'the game is gone'."""

    overlay, user32, _ = make()
    user32.foreground = 0
    assert overlay.is_foreground("League of Legends.exe") is None


def test_a_process_we_may_not_open_reads_as_unknown() -> None:
    overlay, user32, _ = make(kernel32=FakeKernel32(open_fails=True))
    user32.foreground = 999
    assert overlay.is_foreground("League of Legends.exe") is None


def test_an_unreadable_image_name_reads_as_unknown() -> None:
    overlay, user32, _ = make(kernel32=FakeKernel32(None))
    user32.foreground = 999
    assert overlay.is_foreground("League of Legends.exe") is None


def test_a_raising_probe_reads_as_unknown() -> None:
    class Exploding(FakeUser32):
        def GetForegroundWindow(self) -> int:  # noqa: N802
            raise OSError("the window station went away")

    overlay, _user32, _ = make(user32=Exploding())
    assert overlay.is_foreground("League of Legends.exe") is None
