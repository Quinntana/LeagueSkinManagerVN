"""Per-user setup entrypoint for LeagueSkinManagerVN."""

from __future__ import annotations

import ctypes
import logging
import os
import shutil
import subprocess
import sys
from collections.abc import Callable, Collection
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol
from uuid import uuid4

from league_skin_manager.config import APP_DISPLAY_NAME, APP_NAME, UNINSTALL_APP_NAME
from league_skin_manager.installation import (
    INSTALL_OPERATION_MUTEX_NAME,
    AppsAndFeaturesRegistration,
    InstallationError,
    InstallLayout,
    installed_size_kib,
)
from league_skin_manager.uninstall import find_running_process
from league_skin_manager.windows_integration import SingleInstanceMutex

LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class InstallResult:
    install_dir: Path
    executable: Path
    uninstaller: Path


Confirmer = Callable[[str, str], bool]
Notifier = Callable[[str, str, bool], None]
Launcher = Callable[[Path], object]
ProcessFinder = Callable[[Collection[str]], str | None]


class RegistrationWriter(Protocol):
    def register(self, layout: InstallLayout, *, estimated_size_kib: int) -> None: ...


class Mutex(Protocol):
    def acquire(self) -> bool: ...

    def release(self) -> None: ...


def payload_paths(root: Path) -> tuple[Path, Path]:
    payload = root / "payload"
    bundled = (
        payload / f"{APP_NAME}.exe",
        payload / f"{UNINSTALL_APP_NAME}.exe",
    )
    if all(path.is_file() for path in bundled):
        return bundled
    portable = (
        root / f"{APP_NAME}.exe",
        root / f"{UNINSTALL_APP_NAME}.exe",
    )
    if all(path.is_file() for path in portable):
        return portable
    raise InstallationError("The setup payload is incomplete")


def install_payload(
    main_source: Path,
    uninstall_source: Path,
    layout: InstallLayout,
    *,
    registration: RegistrationWriter | None = None,
) -> InstallResult:
    """Atomically replace the fixed per-user program directory."""

    for source in (main_source, uninstall_source):
        if not source.is_file() or source.stat().st_size <= 0:
            raise InstallationError(f"Install payload is missing: {source.name}")
    install_dir = layout.validated_install_dir()
    parent = install_dir.parent
    parent.mkdir(parents=True, exist_ok=True)
    stage = parent / f".{APP_NAME}-install-{uuid4().hex}"
    backup = parent / f".{APP_NAME}-backup-{uuid4().hex}"
    stage.mkdir()
    moved_existing = False
    installed_new = False
    try:
        shutil.copy2(main_source, stage / layout.executable.name)
        shutil.copy2(uninstall_source, stage / layout.uninstaller.name)
        if install_dir.exists():
            if install_dir.is_symlink() or not install_dir.is_dir():
                raise InstallationError("Existing install location is not a normal directory")
            os.replace(install_dir, backup)
            moved_existing = True
        os.replace(stage, install_dir)
        installed_new = True
        registrar = registration or AppsAndFeaturesRegistration()
        registrar.register(
            layout,
            estimated_size_kib=installed_size_kib((layout.executable, layout.uninstaller)),
        )
    except Exception:
        if installed_new and install_dir.exists():
            shutil.rmtree(install_dir, ignore_errors=True)
        if moved_existing and backup.exists():
            os.replace(backup, install_dir)
        raise
    finally:
        shutil.rmtree(stage, ignore_errors=True)
    if backup.exists():
        try:
            shutil.rmtree(backup)
        except OSError:
            LOGGER.warning("Could not remove replaced-install backup %s", backup, exc_info=True)
    return InstallResult(
        install_dir=install_dir,
        executable=layout.executable,
        uninstaller=layout.uninstaller,
    )


def launch_installed(executable: Path) -> None:
    creation_flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    subprocess.Popen(
        [str(executable)],
        cwd=str(executable.parent),
        close_fds=True,
        creationflags=creation_flags,
    )


def confirm_install(title: str, message: str) -> bool:
    if os.name != "nt":
        return False
    yes = 6
    yes_no = 0x00000004
    information = 0x00000040
    default_no = 0x00000100
    result = ctypes.windll.user32.MessageBoxW(
        None,
        message,
        title,
        yes_no | information | default_no,
    )
    return int(result) == yes


def show_result(title: str, message: str, error: bool) -> None:
    if os.name != "nt":
        print(f"{title}: {message}")
        return
    icon = 0x00000010 if error else 0x00000040
    ctypes.windll.user32.MessageBoxW(None, message, title, icon)


def main(
    *,
    local_appdata: str | Path | None = None,
    payload_root: Path | None = None,
    confirmer: Confirmer = confirm_install,
    notifier: Notifier = show_result,
    launcher: Launcher = launch_installed,
    process_finder: ProcessFinder = find_running_process,
    operation_mutex: Mutex | None = None,
    app_mutex: Mutex | None = None,
) -> int:
    if sys.platform != "win32":
        notifier("Setup failed", "LeagueSkinManagerVN supports Windows only.", True)
        return 1
    if not confirmer(
        f"Install {APP_DISPLAY_NAME}",
        "Install League Skin Manager VN for this Windows user?",
    ):
        notifier("Setup cancelled", "Nothing was changed.", False)
        return 0

    selected_operation_mutex = (
        operation_mutex
        if operation_mutex is not None
        else SingleInstanceMutex(name=INSTALL_OPERATION_MUTEX_NAME)
    )
    selected_app_mutex = app_mutex if app_mutex is not None else SingleInstanceMutex()
    operation_acquired = False
    app_acquired = False
    try:
        operation_acquired = selected_operation_mutex.acquire()
        if not operation_acquired:
            notifier(
                "Setup paused",
                "Another League Skin Manager setup or uninstall is already active.",
                True,
            )
            return 1
        app_acquired = selected_app_mutex.acquire()
        if not app_acquired:
            notifier("Setup paused", f"Close {APP_NAME}.exe before installing or updating.", True)
            return 1
        blocker = process_finder((f"{APP_NAME}.exe",))
        if blocker is not None:
            notifier("Setup paused", f"Close {blocker} before installing or updating.", True)
            return 1

        layout = InstallLayout.discover(local_appdata)
        root = payload_root or Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
        main_source, uninstall_source = payload_paths(root)
        result = install_payload(main_source, uninstall_source, layout)

        selected_app_mutex.release()
        app_acquired = False
        launch_warning: str | None = None
        try:
            launch_result = launcher(result.executable)
            if launch_result is False:
                launch_warning = "Windows did not start the installed application."
        except Exception as exc:
            LOGGER.exception("Installation succeeded, but the application could not start")
            launch_warning = str(exc) or type(exc).__name__

        message = "League Skin Manager VN is installed and available in Windows Apps & Features."
        if launch_warning is not None:
            message += f" The application could not be started automatically: {launch_warning}"
        notifier("Setup complete", message, launch_warning is not None)
        return 0
    except (InstallationError, OSError, RuntimeError) as exc:
        LOGGER.exception("Installation failed")
        notifier("Setup failed", str(exc), True)
        return 1
    finally:
        if app_acquired:
            selected_app_mutex.release()
        if operation_acquired:
            selected_operation_mutex.release()


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "InstallResult",
    "confirm_install",
    "install_payload",
    "launch_installed",
    "main",
    "payload_paths",
    "show_result",
]
