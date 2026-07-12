"""Safe, per-user uninstaller for LeagueSkinManagerVN.

The application installs under ``%APPDATA%`` and registers startup under
``HKEY_CURRENT_USER``.  Uninstallation therefore never requests elevation.
All destructive work is gated on both target-path validation and process checks.
"""

from __future__ import annotations

import ctypes
import os
import shutil
from collections.abc import Callable, Collection
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any

import psutil

from league_skin_manager.config import APP_NAME, MANAGER_PROCESS_NAME

SERVICE_PROCESS_NAME = f"{APP_NAME}.exe"
BLOCKING_PROCESSES = (SERVICE_PROCESS_NAME, MANAGER_PROCESS_NAME)


class RemovalState(str, Enum):
    REMOVED = "removed"
    NOT_FOUND = "not_found"
    FAILED = "failed"
    SKIPPED = "skipped"


class UninstallStatus(str, Enum):
    SUCCESS = "success"
    PARTIAL = "partial"
    ABORTED = "aborted"


@dataclass(frozen=True, slots=True)
class UninstallResult:
    status: UninstallStatus
    startup: RemovalState
    app_data: RemovalState
    message: str
    blocking_process: str | None = None
    errors: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return self.status is UninstallStatus.SUCCESS


ProcessFinder = Callable[[Collection[str]], str | None]
StartupRemover = Callable[[], RemovalState]
TreeRemover = Callable[[Path], RemovalState]
Confirmer = Callable[[str, str], bool]
Notifier = Callable[[str, str, bool], None]


def find_running_process(
    executable_names: Collection[str],
    *,
    process_iter: Callable[..., Any] = psutil.process_iter,
    current_pid: int | None = None,
) -> str | None:
    """Return the first blocking executable name, ignoring inaccessible processes."""

    own_pid = os.getpid() if current_pid is None else current_pid
    expected = {name.casefold() for name in executable_names}
    for process in process_iter(["pid", "name"]):
        try:
            pid = process.info.get("pid")
            name = process.info.get("name")
            if pid == own_pid or not isinstance(name, str):
                continue
            if name.casefold() in expected:
                return name
        except (psutil.Error, OSError):
            continue
    return None


def remove_user_startup_registration() -> RemovalState:
    """Remove only the current user's startup value; never touches HKLM."""

    if os.name != "nt":
        return RemovalState.NOT_FOUND

    import winreg

    key_path = r"Software\Microsoft\Windows\CurrentVersion\Run"
    try:
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            key_path,
            0,
            winreg.KEY_QUERY_VALUE | winreg.KEY_SET_VALUE,
        ) as key:
            try:
                winreg.DeleteValue(key, APP_NAME)
            except FileNotFoundError:
                return RemovalState.NOT_FOUND
    except FileNotFoundError:
        return RemovalState.NOT_FOUND
    return RemovalState.REMOVED


def remove_app_data_tree(path: Path) -> RemovalState:
    if not path.exists():
        return RemovalState.NOT_FOUND
    shutil.rmtree(path)
    if path.exists():
        raise OSError(f"Application data still exists after removal: {path}")
    return RemovalState.REMOVED


def _validated_data_dir(appdata_root: Path, data_dir: Path) -> Path:
    """Allow deletion of exactly ``<APPDATA>/<APP_NAME>`` and no other path."""

    root = Path(os.path.abspath(appdata_root)).resolve()
    lexical_target = Path(os.path.abspath(data_dir))
    if lexical_target.parent != Path(os.path.abspath(appdata_root)):
        raise ValueError("Application data target is outside APPDATA")
    if lexical_target.name != APP_NAME:
        raise ValueError(f"Application data target must be named {APP_NAME}")
    if lexical_target.is_symlink():
        raise ValueError("Application data target cannot be a symbolic link")
    resolved_target = lexical_target.resolve()
    if resolved_target.parent != root or resolved_target.name != APP_NAME:
        raise ValueError("Resolved application data target is outside APPDATA")
    return lexical_target


class Uninstaller:
    """Orchestrate process-gated, per-user cleanup with an explicit result."""

    def __init__(
        self,
        *,
        appdata_root: Path,
        data_dir: Path,
        process_finder: ProcessFinder = find_running_process,
        startup_remover: StartupRemover = remove_user_startup_registration,
        tree_remover: TreeRemover = remove_app_data_tree,
    ) -> None:
        self.data_dir = _validated_data_dir(appdata_root, data_dir)
        self.process_finder = process_finder
        self.startup_remover = startup_remover
        self.tree_remover = tree_remover

    def run(self) -> UninstallResult:
        blocking_process = self.process_finder(BLOCKING_PROCESSES)
        if blocking_process is not None:
            return UninstallResult(
                status=UninstallStatus.ABORTED,
                startup=RemovalState.SKIPPED,
                app_data=RemovalState.SKIPPED,
                blocking_process=blocking_process,
                message=f"Close {blocking_process} before uninstalling.",
            )

        errors: list[str] = []
        try:
            startup_state = self.startup_remover()
        except (OSError, RuntimeError) as exc:
            startup_state = RemovalState.FAILED
            errors.append(f"startup registration: {exc}")

        try:
            app_data_state = self.tree_remover(self.data_dir)
        except OSError as exc:
            app_data_state = RemovalState.FAILED
            errors.append(f"application data: {exc}")

        if errors:
            return UninstallResult(
                status=UninstallStatus.PARTIAL,
                startup=startup_state,
                app_data=app_data_state,
                errors=tuple(errors),
                message="Uninstall incomplete: " + "; ".join(errors),
            )

        startup_text = (
            "startup registration removed"
            if startup_state is RemovalState.REMOVED
            else "startup registration was already absent"
        )
        data_text = (
            "application data removed"
            if app_data_state is RemovalState.REMOVED
            else "application data was already absent"
        )
        return UninstallResult(
            status=UninstallStatus.SUCCESS,
            startup=startup_state,
            app_data=app_data_state,
            message=f"Uninstall complete: {startup_text}; {data_text}.",
        )


def confirm_uninstall(title: str, message: str) -> bool:
    if os.name != "nt":
        return False
    yes = 6
    yes_no = 0x00000004
    warning = 0x00000030
    result = ctypes.windll.user32.MessageBoxW(None, message, title, yes_no | warning)
    return int(result) == yes


def show_result(title: str, message: str, error: bool) -> None:
    if os.name != "nt":
        print(f"{title}: {message}")
        return
    ok = 0x00000000
    icon = 0x00000010 if error else 0x00000040
    ctypes.windll.user32.MessageBoxW(None, message, title, ok | icon)


def main(
    *,
    appdata: str | Path | None = None,
    confirmer: Confirmer = confirm_uninstall,
    notifier: Notifier = show_result,
    process_finder: ProcessFinder = find_running_process,
    startup_remover: StartupRemover = remove_user_startup_registration,
    tree_remover: TreeRemover = remove_app_data_tree,
) -> int:
    """Run the interactive entrypoint without elevation or callback exits."""

    raw_appdata = appdata if appdata is not None else os.environ.get("APPDATA")
    if not raw_appdata:
        notifier("Uninstall failed", "APPDATA is unavailable; nothing was removed.", True)
        return 1

    if not confirmer(
        f"Uninstall {APP_NAME}",
        "Remove startup registration and all LeagueSkinManagerVN application data?",
    ):
        notifier("Uninstall cancelled", "Nothing was removed.", False)
        return 0

    appdata_root = Path(raw_appdata)
    try:
        result = Uninstaller(
            appdata_root=appdata_root,
            data_dir=appdata_root / APP_NAME,
            process_finder=process_finder,
            startup_remover=startup_remover,
            tree_remover=tree_remover,
        ).run()
    except ValueError as exc:
        notifier("Uninstall failed", str(exc), True)
        return 1

    title = {
        UninstallStatus.SUCCESS: "Uninstall complete",
        UninstallStatus.PARTIAL: "Uninstall incomplete",
        UninstallStatus.ABORTED: "Uninstall aborted",
    }[result.status]
    notifier(title, result.message, result.status is not UninstallStatus.SUCCESS)
    return 0 if result.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "BLOCKING_PROCESSES",
    "RemovalState",
    "UninstallResult",
    "UninstallStatus",
    "Uninstaller",
    "find_running_process",
    "main",
    "remove_app_data_tree",
    "remove_user_startup_registration",
]
