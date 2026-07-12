"""Narrow Windows integration boundary for instance/startup/process handling."""

from __future__ import annotations

import ctypes
import logging
import os
import subprocess
import sys
from contextlib import suppress
from ctypes import wintypes
from pathlib import Path
from typing import Any

import psutil

from .config import APP_NAME

ERROR_ALREADY_EXISTS = 183
LEGACY_MUTEX_NAME = "LeagueSkinManagerVN_Mutex_v1"
MUTEX_NAME = "Local\\LeagueSkinManagerVN_Mutex_v2"
MUTEX_NAMES = (LEGACY_MUTEX_NAME, MUTEX_NAME)


class SingleInstanceMutex:
    def __init__(self, name: str | None = None, kernel32: Any | None = None) -> None:
        self.names = (name,) if name is not None else MUTEX_NAMES
        self.kernel32 = kernel32
        self.handle: int | None = None
        self._handles: list[int] = []
        if self.kernel32 is None and os.name == "nt":
            self.kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    def acquire(self) -> bool:
        if self.kernel32 is None:
            return True
        if self._handles:
            return True
        create_mutex = self.kernel32.CreateMutexW
        create_mutex.argtypes = [wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR]
        create_mutex.restype = wintypes.HANDLE
        for name in self.names:
            ctypes.set_last_error(0)
            handle = create_mutex(None, False, name)
            if not handle:
                self.release()
                return False
            native_handle = int(handle)
            self._handles.append(native_handle)
            self.handle = self._handles[0]
            if ctypes.get_last_error() == ERROR_ALREADY_EXISTS:
                self.release()
                return False
        return True

    def release(self) -> None:
        handles = tuple(reversed(self._handles))
        self._handles.clear()
        if not handles or self.kernel32 is None:
            self.handle = None
            return
        self.handle = None
        for handle in handles:
            with suppress(Exception):
                self.kernel32.CloseHandle(wintypes.HANDLE(handle))

    def __enter__(self) -> SingleInstanceMutex:
        if not self.acquire():
            raise RuntimeError("LeagueSkinManagerVN is already running")
        return self

    def __exit__(self, *_args: object) -> None:
        self.release()


class StartupRegistration:
    KEY_PATH = r"Software\Microsoft\Windows\CurrentVersion\Run"

    def __init__(self, app_name: str = APP_NAME) -> None:
        self.app_name = app_name

    @staticmethod
    def _command(executable: Path) -> str:
        return f'"{executable.resolve()}"'

    def is_enabled(self, executable: Path) -> bool:
        if os.name != "nt":
            return False
        import winreg

        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, self.KEY_PATH) as key:
                value, _kind = winreg.QueryValueEx(key, self.app_name)
            return str(value) == self._command(executable)
        except (FileNotFoundError, OSError):
            return False

    def set_enabled(self, executable: Path, enabled: bool) -> None:
        if os.name != "nt":
            return
        import winreg

        access = winreg.KEY_QUERY_VALUE | winreg.KEY_SET_VALUE
        with winreg.CreateKeyEx(
            winreg.HKEY_CURRENT_USER,
            self.KEY_PATH,
            0,
            access,
        ) as key:
            if enabled:
                winreg.SetValueEx(
                    key,
                    self.app_name,
                    0,
                    winreg.REG_SZ,
                    self._command(executable),
                )
            else:
                with suppress(FileNotFoundError):
                    winreg.DeleteValue(key, self.app_name)


class ProcessLauncher:
    def __init__(self, logger: logging.Logger) -> None:
        self.logger = logger

    @staticmethod
    def is_running(executable_name: str) -> bool:
        expected = executable_name.casefold()
        for process in psutil.process_iter(["name"]):
            try:
                name = process.info.get("name")
                if isinstance(name, str) and name.casefold() == expected:
                    return True
            except (psutil.Error, OSError):
                continue
        return False

    def launch(self, executable: Path) -> bool:
        if not executable.is_file():
            self.logger.warning("Executable is unavailable: %s", executable)
            return False
        try:
            creation_flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
            subprocess.Popen(
                [str(executable)],
                cwd=str(executable.parent),
                close_fds=True,
                creationflags=creation_flags,
            )
            self.logger.info("Launched %s", executable)
            return True
        except OSError:
            self.logger.exception("Could not launch %s", executable)
            return False


def running_executable() -> Path:
    return Path(sys.executable).resolve()
