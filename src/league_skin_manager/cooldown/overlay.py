"""Win32 surface for the cooldown overlay.

Everything in this package that talks to ``user32`` lives here, so the panel
stays about layout and this stays testable with a double.

Three jobs, all measured against a real Borderless League client on
2026-08-20:

* Make a Tk window behave like an overlay -- borderless, always on top, and
  non-activating, so clicking a slot does not take keyboard focus off the game.
* Report which process owns the foreground window, so the board can hide when
  the player alt-tabs away.
* Declare DPI awareness, so a position saved on a scaled display is restored
  where the user put it.

The subtlety that costs an afternoon: ``Tk.winfo_id()`` returns a *child*
window whose extended style is ``0x4``. The style that matters belongs to
``GetAncestor(hwnd, GA_ROOT)``. Applying flags to the child silently does
nothing at all.
"""

from __future__ import annotations

import ctypes
import logging
import ntpath
import os
from ctypes import wintypes
from typing import Any

LOGGER = logging.getLogger(__name__)

GWL_EXSTYLE = -20
GA_ROOT = 2

WS_EX_TOPMOST = 0x00000008
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_NOACTIVATE = 0x08000000

# Tk already sets TOPMOST and TOOLWINDOW when told to be topmost and
# borderless. NOACTIVATE is the one this application has to add.
OVERLAY_EXSTYLE = WS_EX_NOACTIVATE

SWP_NOSIZE = 0x0001
SWP_NOMOVE = 0x0002
SWP_NOZORDER = 0x0004
SWP_NOACTIVATE = 0x0010
SWP_FRAMECHANGED = 0x0020
STYLE_REFRESH = SWP_NOSIZE | SWP_NOMOVE | SWP_NOZORDER | SWP_NOACTIVATE | SWP_FRAMECHANGED

PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
MAX_PROCESS_PATH = 32768
DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2 = -4


class _Auto:
    """Sentinel: load the real library. ``None`` means deliberately absent."""


AUTO: Any = _Auto()


def _load(name: str) -> Any | None:
    if os.name != "nt":
        return None
    try:
        return ctypes.WinDLL(name, use_last_error=True)
    except OSError:  # pragma: no cover - a Windows install without user32
        return None


class OverlayWindow:
    """Applies overlay window styles and reads the foreground process."""

    def __init__(
        self,
        *,
        user32: Any = AUTO,
        kernel32: Any = AUTO,
        logger: logging.Logger = LOGGER,
    ) -> None:
        self._user32 = _load("user32") if isinstance(user32, _Auto) else user32
        self._kernel32 = _load("kernel32") if isinstance(kernel32, _Auto) else kernel32
        self._logger = logger

    @property
    def available(self) -> bool:
        return self._user32 is not None

    # -- window styling ---------------------------------------------------

    def root_handle(self, handle: int) -> int:
        """Resolve the top-level window that owns a Tk child handle."""

        if self._user32 is None:
            return handle
        try:
            root = int(self._user32.GetAncestor(wintypes.HWND(handle), GA_ROOT))
        except Exception:  # noqa: BLE001 - fall back to what we were given
            self._logger.debug("GetAncestor failed for %s", handle, exc_info=True)
            return handle
        return root or handle

    def apply(self, handle: int) -> bool:
        """Add the overlay styles to a window, then read them back.

        Returns whether the styles are present afterwards. A window that cannot
        be styled still works -- it simply steals focus when clicked -- so the
        caller logs this rather than failing.
        """

        if self._user32 is None:
            return False
        root = self.root_handle(handle)
        try:
            current = (
                int(self._user32.GetWindowLongW(wintypes.HWND(root), GWL_EXSTYLE)) & 0xFFFFFFFF
            )
            if current & OVERLAY_EXSTYLE != OVERLAY_EXSTYLE:
                self._user32.SetWindowLongW(
                    wintypes.HWND(root), GWL_EXSTYLE, current | OVERLAY_EXSTYLE
                )
                self._user32.SetWindowPos(
                    wintypes.HWND(root), wintypes.HWND(0), 0, 0, 0, 0, STYLE_REFRESH
                )
            verified = (
                int(self._user32.GetWindowLongW(wintypes.HWND(root), GWL_EXSTYLE)) & 0xFFFFFFFF
            )
        except Exception:  # noqa: BLE001 - styling is best effort
            self._logger.debug("Could not apply overlay styles", exc_info=True)
            return False
        applied = verified & OVERLAY_EXSTYLE == OVERLAY_EXSTYLE
        if not applied:
            self._logger.info("The cooldown overlay could not be made non-activating")
        return applied

    # -- foreground -------------------------------------------------------

    def foreground_process(self) -> str | None:
        """Return the image name of the foreground window's process."""

        if self._user32 is None or self._kernel32 is None:
            return None
        try:
            handle = self._user32.GetForegroundWindow()
            if not handle:
                return None
            pid = wintypes.DWORD()
            self._user32.GetWindowThreadProcessId(wintypes.HWND(handle), ctypes.byref(pid))
            process = self._kernel32.OpenProcess(
                PROCESS_QUERY_LIMITED_INFORMATION, False, pid.value
            )
            if not process:
                return None
            try:
                buffer = ctypes.create_unicode_buffer(MAX_PROCESS_PATH)
                size = wintypes.DWORD(len(buffer))
                if not self._kernel32.QueryFullProcessImageNameW(
                    process, 0, buffer, ctypes.byref(size)
                ):
                    return None
                return ntpath.basename(buffer.value[: size.value])
            finally:
                self._kernel32.CloseHandle(process)
        except Exception:  # noqa: BLE001 - a failed probe must read as "unknown"
            self._logger.debug("Could not read the foreground process", exc_info=True)
            return None

    def is_foreground(self, process_name: str) -> bool | None:
        """Whether ``process_name`` owns the foreground window.

        ``None`` means the question could not be answered, which callers treat
        as "leave the board as it is" rather than as a negative.
        """

        current = self.foreground_process()
        if current is None:
            return None
        return current.casefold() == process_name.casefold()


def enable_dpi_awareness() -> bool:
    """Ask Windows for physical pixels.

    Without this a position saved by a scaled process is restored in a
    different coordinate space, and the error grows with distance from the
    origin.
    """

    user32 = _load("user32")
    if user32 is None:
        return False
    try:
        setter = user32.SetProcessDpiAwarenessContext
        setter.argtypes = [wintypes.HANDLE]
        setter.restype = wintypes.BOOL
        return bool(setter(wintypes.HANDLE(DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2)))
    except (AttributeError, OSError):
        # Pre-1703 Windows has no such entry point and is not a target.
        return False


__all__ = [
    "GA_ROOT",
    "GWL_EXSTYLE",
    "OVERLAY_EXSTYLE",
    "WS_EX_NOACTIVATE",
    "WS_EX_TOOLWINDOW",
    "WS_EX_TOPMOST",
    "OverlayWindow",
    "enable_dpi_awareness",
]
