"""Windows integration: single instance, startup, shortcut, processes, shell.

Everything here is an adapter over an OS facility.  Nothing above this module
imports ``ctypes``, ``winreg``, or ``psutil`` directly.

The previous design also carried an inter-process activation event so a second
launch could ask the first to show a notification.  A second launch now simply
exits, which removes roughly a hundred lines of event plumbing and a listener
thread for a toast nobody asked for.
"""

from __future__ import annotations

import ctypes
import logging
import os
import subprocess
import sys
from collections.abc import Collection, Sequence
from contextlib import suppress
from ctypes import wintypes
from pathlib import Path
from typing import Any

import psutil

from .config import APP_DISPLAY_NAME, APP_NAME
from .hashing import is_real_file

LOGGER = logging.getLogger(__name__)

MUTEX_NAME = rf"Global\{APP_NAME}-singleton"
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
ERROR_ALREADY_EXISTS = 183


# --------------------------------------------------------------------------
# Single instance
# --------------------------------------------------------------------------


class SingleInstance:
    """A named mutex ensuring only one copy runs at a time."""

    def __init__(self, name: str = MUTEX_NAME, kernel32: Any | None = None) -> None:
        self.name = name
        self._handle: int | None = None
        self._kernel32 = kernel32
        if self._kernel32 is None and os.name == "nt":
            self._kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    def acquire(self) -> bool:
        """Return whether this process now owns the single-instance lock."""

        if self._kernel32 is None:
            return True
        if self._handle is not None:
            return True
        create = self._kernel32.CreateMutexW
        create.argtypes = [wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR]
        create.restype = wintypes.HANDLE
        ctypes.set_last_error(0)
        handle = create(None, False, self.name)
        if not handle:
            return False
        if ctypes.get_last_error() == ERROR_ALREADY_EXISTS:
            with suppress(Exception):
                self._kernel32.CloseHandle(wintypes.HANDLE(int(handle)))
            return False
        self._handle = int(handle)
        return True

    def release(self) -> None:
        handle, self._handle = self._handle, None
        if handle is not None and self._kernel32 is not None:
            with suppress(Exception):
                self._kernel32.CloseHandle(wintypes.HANDLE(handle))

    def __enter__(self) -> SingleInstance:
        if not self.acquire():
            raise RuntimeError(f"{APP_DISPLAY_NAME} is already running")
        return self

    def __exit__(self, *_exc: object) -> None:
        self.release()


# --------------------------------------------------------------------------
# Start with Windows
# --------------------------------------------------------------------------


def startup_command(executable: Path) -> str:
    return f'"{Path(os.path.abspath(executable))}"'


def startup_enabled(executable: Path) -> bool:
    if os.name != "nt":
        return False
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            value, _kind = winreg.QueryValueEx(key, APP_NAME)
    except OSError:
        return False
    return str(value) == startup_command(executable)


def set_startup_enabled(executable: Path, enabled: bool) -> bool:
    """Add or remove this application's HKCU Run entry."""

    if os.name != "nt":
        return False
    import winreg

    try:
        access = winreg.KEY_QUERY_VALUE | winreg.KEY_SET_VALUE
        with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, access) as key:
            if enabled:
                winreg.SetValueEx(key, APP_NAME, 0, winreg.REG_SZ, startup_command(executable))
            else:
                with suppress(FileNotFoundError):
                    winreg.DeleteValue(key, APP_NAME)
    except OSError:
        LOGGER.warning("Could not update the Windows startup entry", exc_info=True)
        return False
    return True


# --------------------------------------------------------------------------
# Start Menu shortcut (this is what makes the app findable in Windows Search)
# --------------------------------------------------------------------------


def start_menu_shortcut() -> Path:
    appdata = os.environ.get("APPDATA")
    base = Path(appdata) if appdata else Path.home() / "AppData" / "Roaming"
    return base / "Microsoft" / "Windows" / "Start Menu" / "Programs" / f"{APP_DISPLAY_NAME}.lnk"


def create_start_menu_shortcut(executable: Path, *, runner: Any = None) -> bool:
    """Create the Start Menu shortcut, which Windows Search indexes.

    A portable executable is otherwise unfindable by name.  Written through
    WScript.Shell rather than by hand: a .lnk is a structured binary format,
    and PowerShell is already a dependency of installer verification.
    """

    if os.name != "nt":
        return False
    shortcut = start_menu_shortcut()
    target = Path(os.path.abspath(executable))
    script = (
        "$ErrorActionPreference='Stop';"
        "$s=(New-Object -ComObject WScript.Shell).CreateShortcut("
        f"'{shortcut}');"
        f"$s.TargetPath='{target}';"
        f"$s.WorkingDirectory='{target.parent}';"
        f"$s.Description='{APP_DISPLAY_NAME}';"
        "$s.Save()"
    )
    try:
        shortcut.parent.mkdir(parents=True, exist_ok=True)
        run = runner or _run_powershell
        completed = run(script)
    except (OSError, subprocess.SubprocessError):
        LOGGER.warning("Could not create the Start Menu shortcut", exc_info=True)
        return False
    if completed.returncode != 0:
        LOGGER.warning("Start Menu shortcut creation failed: %s", completed.stderr)
        return False
    return True


def remove_start_menu_shortcut() -> bool:
    try:
        start_menu_shortcut().unlink(missing_ok=True)
    except OSError:
        LOGGER.warning("Could not remove the Start Menu shortcut", exc_info=True)
        return False
    return True


def _run_powershell(script: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            "powershell.exe",
            "-NoLogo",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-Command",
            script,
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


# --------------------------------------------------------------------------
# Processes
# --------------------------------------------------------------------------


class ProcessLookup:
    """Answer whether named processes are running."""

    @staticmethod
    def is_any_running(names: Collection[str]) -> bool:
        expected = {name.casefold() for name in names if name}
        if not expected:
            return False
        for process in psutil.process_iter(["name"]):
            try:
                name = process.info.get("name")
            except (psutil.Error, OSError):
                continue
            if isinstance(name, str) and name.casefold() in expected:
                return True
        return False

    @staticmethod
    def is_running(name: str) -> bool:
        return ProcessLookup.is_any_running((name,))


def launch_detached(executable: Path, arguments: Sequence[str] = ()) -> bool:
    """Start a process without a console window; never raises."""

    if not is_real_file(executable):
        LOGGER.warning("Executable is unavailable: %s", executable)
        return False
    try:
        subprocess.Popen(
            [str(executable), *arguments],
            cwd=str(executable.parent),
            close_fds=True,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except OSError:
        LOGGER.exception("Could not launch %s", executable)
        return False
    return True


# --------------------------------------------------------------------------
# Shell
# --------------------------------------------------------------------------


def open_path(path: Path) -> bool:
    """Open a file or folder in Explorer."""

    if os.name != "nt":
        return False
    try:
        os.startfile(str(Path(os.path.abspath(path))))
    except OSError:
        LOGGER.warning("Could not open %s", path, exc_info=True)
        return False
    return True


def open_url(url: str) -> bool:
    """Open a URL in the default browser."""

    import webbrowser

    try:
        return webbrowser.open(url)
    except Exception:  # noqa: BLE001 - a browser failure must never propagate
        LOGGER.warning("Could not open %s", url, exc_info=True)
        return False


def running_executable() -> Path:
    return Path(os.path.abspath(sys.executable))


__all__ = [
    "MUTEX_NAME",
    "RUN_KEY",
    "ProcessLookup",
    "SingleInstance",
    "create_start_menu_shortcut",
    "launch_detached",
    "open_path",
    "open_url",
    "remove_start_menu_shortcut",
    "running_executable",
    "set_startup_enabled",
    "start_menu_shortcut",
    "startup_command",
    "startup_enabled",
]
