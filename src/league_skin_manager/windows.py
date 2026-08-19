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

MUTEX_NAME = rf"Local\{APP_NAME}-singleton"
"""Session-local, not Global.

This is a per-user application, so a machine-wide mutex would stop two
logged-in users each running their own copy, and creating Global objects
can require a privilege this process has no reason to hold.
"""
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"

SHORTCUT_PATH_ENV = "LSMVN_SHORTCUT_PATH"
SHORTCUT_TARGET_ENV = "LSMVN_SHORTCUT_TARGET"
SHORTCUT_WORKDIR_ENV = "LSMVN_SHORTCUT_WORKDIR"
SHORTCUT_NAME_ENV = "LSMVN_SHORTCUT_NAME"

_SHORTCUT_SCRIPT = (
    "$ErrorActionPreference='Stop';"
    "$s=(New-Object -ComObject WScript.Shell).CreateShortcut($env:LSMVN_SHORTCUT_PATH);"
    "$s.TargetPath=$env:LSMVN_SHORTCUT_TARGET;"
    "$s.WorkingDirectory=$env:LSMVN_SHORTCUT_WORKDIR;"
    "$s.Description=$env:LSMVN_SHORTCUT_NAME;"
    "$s.Save()"
)
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


def repair_startup_path(executable: Path) -> bool:
    """Re-point an existing Run entry at the current executable.

    The entry stores an absolute path, so moving a portable executable leaves
    it pointing at nothing: Windows silently fails to launch at login and the
    tray reports the toggle as off, with no explanation. The Start Menu
    shortcut is rewritten on every launch for the same reason; this keeps the
    two consistent.

    Only ever rewrites an entry that already exists. An absent entry means the
    user did not ask for startup, and that stays true.
    """

    if os.name != "nt":
        return False
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            current, _kind = winreg.QueryValueEx(key, APP_NAME)
    except OSError:
        return False
    wanted = startup_command(executable)
    if str(current) == wanted:
        return False
    LOGGER.info("Repointing the Windows startup entry at %s", executable)
    return set_startup_enabled(executable, True)


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
    # Paths are passed through the environment rather than interpolated into
    # the script. A single quote in a path -- "C:\Users\Bob's PC" is an
    # ordinary folder name -- would otherwise close the PowerShell string and
    # have the remainder executed as code.
    environment = dict(os.environ)
    environment[SHORTCUT_PATH_ENV] = str(shortcut)
    environment[SHORTCUT_TARGET_ENV] = str(target)
    environment[SHORTCUT_WORKDIR_ENV] = str(target.parent)
    environment[SHORTCUT_NAME_ENV] = APP_DISPLAY_NAME
    try:
        shortcut.parent.mkdir(parents=True, exist_ok=True)
        run = runner or _run_powershell
        completed = run(_SHORTCUT_SCRIPT, environment)
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


def system_powershell() -> Path:
    """Resolve the OS copy of PowerShell without trusting PATH or environment.

    ltk.py hardens its Authenticode check this way; there is no reason for a
    second, weaker standard elsewhere in the same application.
    """

    if os.name != "nt":
        return Path("powershell.exe")
    buffer = ctypes.create_unicode_buffer(32_768)
    try:
        length = ctypes.windll.kernel32.GetWindowsDirectoryW(buffer, len(buffer))
    except (AttributeError, OSError):
        length = 0
    root = (
        Path(buffer.value)
        if isinstance(length, int) and 0 < length < len(buffer)
        else Path(r"C:\Windows")
    )
    return root / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"


def _run_powershell(
    script: str, environment: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            str(system_powershell()),
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
        env=environment,
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
    "repair_startup_path",
    "running_executable",
    "set_startup_enabled",
    "start_menu_shortcut",
    "startup_command",
    "startup_enabled",
]
