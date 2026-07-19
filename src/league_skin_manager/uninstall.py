"""Safe, per-user uninstaller for LeagueSkinManagerVN.

The installed one-file executable first relaunches itself from a temporary
directory. The relocated process can then remove the validated program tree
synchronously while holding both installation and application mutexes.
"""

from __future__ import annotations

import base64
import ctypes
import os
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Callable, Collection
from contextlib import suppress
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Protocol

import psutil

from league_skin_manager.config import APP_NAME, MANAGER_PROCESS_NAMES
from league_skin_manager.installation import (
    INSTALL_OPERATION_MUTEX_NAME,
    AppsAndFeaturesRegistration,
    InstallationError,
    InstallLayout,
    _is_reparse_point,
)
from league_skin_manager.windows_integration import SingleInstanceMutex

SERVICE_PROCESS_NAME = f"{APP_NAME}.exe"
BLOCKING_PROCESSES = (SERVICE_PROCESS_NAME, *MANAGER_PROCESS_NAMES)

_RELOCATED_ENV = "LSMVN_UNINSTALL_RELOCATED"
_RELOCATED_DIR_ENV = "LSMVN_UNINSTALL_TEMP_DIR"
_RELOCATED_WAIT_PID_ENV = "LSMVN_UNINSTALL_WAIT_PID"
_TEMP_CLEANUP_DIR_ENV = "LSMVN_TEMP_CLEANUP_DIR"
_TEMP_CLEANUP_ROOT_ENV = "LSMVN_TEMP_CLEANUP_ROOT"
_TEMP_CLEANUP_PID_ENV = "LSMVN_TEMP_CLEANUP_PID"
_TEMP_PREFIX = f"{APP_NAME}-uninstall-"


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
    registration: RemovalState
    install_files: RemovalState
    message: str
    blocking_process: str | None = None
    errors: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return self.status is UninstallStatus.SUCCESS


ProcessFinder = Callable[[Collection[str]], str | None]
StartupRemover = Callable[[], RemovalState]
TreeRemover = Callable[[Path], RemovalState]
RegistrationRemover = Callable[[], RemovalState]
InstallCleanup = Callable[[], RemovalState]
Confirmer = Callable[[str, str], bool]
Notifier = Callable[[str, str, bool], None]


class Mutex(Protocol):
    def acquire(self) -> bool: ...

    def release(self) -> None: ...


def find_running_process(
    executable_names: Collection[str],
    *,
    process_iter: Callable[..., Any] = psutil.process_iter,
    current_pid: int | None = None,
    blocked_roots: Collection[Path] = (),
) -> str | None:
    """Return the first blocker by known name or executable path under owned roots."""

    own_pid = os.getpid() if current_pid is None else current_pid
    expected = {name.casefold() for name in executable_names}
    roots = tuple(Path(root).resolve() for root in blocked_roots)
    for process in process_iter(["pid", "name", "exe"]):
        try:
            pid = process.info.get("pid")
            name = process.info.get("name")
            if pid == own_pid:
                continue
            if isinstance(name, str) and name.casefold() in expected:
                return name
            executable = process.info.get("exe")
            if isinstance(executable, str) and executable:
                resolved = Path(executable).resolve()
                if any(resolved == root or resolved.is_relative_to(root) for root in roots):
                    return name if isinstance(name, str) and name else str(resolved)
        except (psutil.Error, OSError, RuntimeError, ValueError):
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


def remove_install_tree(layout: InstallLayout) -> RemovalState:
    """Synchronously remove only the validated per-user program directory."""

    install_dir = layout.validated_install_dir()
    if not install_dir.exists():
        return RemovalState.NOT_FOUND
    if not install_dir.is_dir() or _is_reparse_point(install_dir):
        raise InstallationError("Install directory is not a normal directory")
    shutil.rmtree(install_dir)
    if install_dir.exists():
        raise OSError(f"Installed files still exist after removal: {install_dir}")
    return RemovalState.REMOVED


def remove_apps_registration() -> RemovalState:
    removed = AppsAndFeaturesRegistration().unregister()
    return RemovalState.REMOVED if removed else RemovalState.NOT_FOUND


def _validated_data_dir(appdata_root: Path, data_dir: Path) -> Path:
    """Allow deletion of exactly ``<APPDATA>/<APP_NAME>`` and no other path."""

    lexical_root = Path(os.path.abspath(appdata_root))
    root = lexical_root.resolve()
    lexical_target = Path(os.path.abspath(data_dir))
    if lexical_target.parent != lexical_root:
        raise ValueError("Application data target is outside APPDATA")
    if lexical_target.name != APP_NAME:
        raise ValueError(f"Application data target must be named {APP_NAME}")
    if _is_reparse_point(lexical_target):
        raise ValueError("Application data target cannot be a reparse point")
    resolved_target = lexical_target.resolve()
    if resolved_target.parent != root or resolved_target.name != APP_NAME:
        raise ValueError("Resolved application data target is outside APPDATA")
    return lexical_target


def _aborted_result(message: str, blocking_process: str | None = None) -> UninstallResult:
    return UninstallResult(
        status=UninstallStatus.ABORTED,
        startup=RemovalState.SKIPPED,
        app_data=RemovalState.SKIPPED,
        registration=RemovalState.SKIPPED,
        install_files=RemovalState.SKIPPED,
        blocking_process=blocking_process,
        message=message,
    )


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
        registration_remover: RegistrationRemover = lambda: RemovalState.NOT_FOUND,
        install_cleanup: InstallCleanup = lambda: RemovalState.NOT_FOUND,
        operation_mutex: Mutex | None = None,
        mutex: Mutex | None = None,
    ) -> None:
        self.data_dir = _validated_data_dir(appdata_root, data_dir)
        self.process_finder = process_finder
        self.startup_remover = startup_remover
        self.tree_remover = tree_remover
        self.registration_remover = registration_remover
        self.install_cleanup = install_cleanup
        self.operation_mutex = operation_mutex
        self.mutex = mutex

    def run(self) -> UninstallResult:
        operation_acquired = False
        app_acquired = False
        try:
            if self.operation_mutex is not None:
                operation_acquired = self.operation_mutex.acquire()
                if not operation_acquired:
                    return _aborted_result(
                        "Another League Skin Manager setup or uninstall is already active."
                    )
            if self.mutex is not None:
                app_acquired = self.mutex.acquire()
                if not app_acquired:
                    return _aborted_result(
                        f"Close {SERVICE_PROCESS_NAME} before uninstalling.",
                        SERVICE_PROCESS_NAME,
                    )

            blocking_process = self.process_finder(BLOCKING_PROCESSES)
            if blocking_process is not None:
                return _aborted_result(
                    f"Close {blocking_process} before uninstalling.", blocking_process
                )

            errors: list[str] = []
            try:
                startup_state = self.startup_remover()
                if startup_state is RemovalState.FAILED:
                    errors.append("startup registration: removal failed")
            except (OSError, RuntimeError) as exc:
                startup_state = RemovalState.FAILED
                errors.append(f"startup registration: {exc}")

            try:
                app_data_state = self.tree_remover(self.data_dir)
                if app_data_state is RemovalState.FAILED:
                    errors.append("application data: removal failed")
            except OSError as exc:
                app_data_state = RemovalState.FAILED
                errors.append(f"application data: {exc}")

            if errors:
                return UninstallResult(
                    status=UninstallStatus.PARTIAL,
                    startup=startup_state,
                    app_data=app_data_state,
                    registration=RemovalState.SKIPPED,
                    install_files=RemovalState.SKIPPED,
                    errors=tuple(errors),
                    message="Uninstall incomplete: " + "; ".join(errors),
                )

            try:
                install_state = self.install_cleanup()
                if install_state is RemovalState.FAILED:
                    errors.append("installed files: cleanup failed")
            except (OSError, RuntimeError) as exc:
                install_state = RemovalState.FAILED
                errors.append(f"installed files: {exc}")
            if errors:
                return UninstallResult(
                    status=UninstallStatus.PARTIAL,
                    startup=startup_state,
                    app_data=app_data_state,
                    registration=RemovalState.SKIPPED,
                    install_files=install_state,
                    errors=tuple(errors),
                    message="Uninstall incomplete: " + "; ".join(errors),
                )

            try:
                registration_state = self.registration_remover()
                if registration_state is RemovalState.FAILED:
                    errors.append("Apps & Features registration: removal failed")
            except (OSError, RuntimeError) as exc:
                registration_state = RemovalState.FAILED
                errors.append(f"Apps & Features registration: {exc}")
            if errors:
                return UninstallResult(
                    status=UninstallStatus.PARTIAL,
                    startup=startup_state,
                    app_data=app_data_state,
                    registration=registration_state,
                    install_files=install_state,
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
            install_text = (
                "installed files removed"
                if install_state is RemovalState.REMOVED
                else "installed files were already absent"
            )
            registration_text = (
                "Apps & Features registration removed"
                if registration_state is RemovalState.REMOVED
                else "Apps & Features registration was already absent"
            )
            return UninstallResult(
                status=UninstallStatus.SUCCESS,
                startup=startup_state,
                app_data=app_data_state,
                registration=registration_state,
                install_files=install_state,
                message=(
                    f"Uninstall complete: {startup_text}; {data_text}; {install_text}; "
                    f"{registration_text}."
                ),
            )
        finally:
            if app_acquired and self.mutex is not None:
                self.mutex.release()
            if operation_acquired and self.operation_mutex is not None:
                self.operation_mutex.release()


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
    local_appdata: str | Path | None = None,
    confirmer: Confirmer = confirm_uninstall,
    notifier: Notifier = show_result,
    process_finder: ProcessFinder = find_running_process,
    startup_remover: StartupRemover = remove_user_startup_registration,
    tree_remover: TreeRemover = remove_app_data_tree,
    registration_remover: RegistrationRemover = remove_apps_registration,
    install_cleanup: InstallCleanup | None = None,
    operation_mutex: Mutex | None = None,
    mutex: Mutex | None = None,
) -> int:
    """Run the interactive entrypoint without elevation or callback exits."""

    raw_appdata = appdata if appdata is not None else os.environ.get("APPDATA")
    if not raw_appdata:
        notifier("Uninstall failed", "APPDATA is unavailable; nothing was removed.", True)
        return 1

    if not confirmer(
        f"Uninstall {APP_NAME}",
        "Remove LeagueSkinManagerVN, downloaded skins, cache, logs, CSLOL profiles, "
        "migration history, and all other LeagueSkinManagerVN application data? "
        "The separately installed official LTK Manager and its library will be kept.",
    ):
        notifier("Uninstall cancelled", "Nothing was removed.", False)
        return 0

    appdata_root = Path(raw_appdata)
    try:
        layout = InstallLayout.discover(local_appdata)
        data_dir = appdata_root / APP_NAME
        selected_finder = process_finder
        if process_finder is find_running_process:

            def find_owned_process(names: Collection[str]) -> str | None:
                return find_running_process(
                    names,
                    blocked_roots=(data_dir, layout.install_dir),
                )

            selected_finder = find_owned_process
        selected_install_cleanup = (
            install_cleanup if install_cleanup is not None else lambda: remove_install_tree(layout)
        )
        selected_operation_mutex = (
            operation_mutex
            if operation_mutex is not None
            else SingleInstanceMutex(name=INSTALL_OPERATION_MUTEX_NAME)
        )
        selected_app_mutex = mutex if mutex is not None else SingleInstanceMutex()
        result = Uninstaller(
            appdata_root=appdata_root,
            data_dir=data_dir,
            process_finder=selected_finder,
            startup_remover=startup_remover,
            tree_remover=tree_remover,
            registration_remover=registration_remover,
            install_cleanup=selected_install_cleanup,
            operation_mutex=selected_operation_mutex,
            mutex=selected_app_mutex,
        ).run()
    except (ValueError, InstallationError) as exc:
        notifier("Uninstall failed", str(exc), True)
        return 1

    title = {
        UninstallStatus.SUCCESS: "Uninstall complete",
        UninstallStatus.PARTIAL: "Uninstall incomplete",
        UninstallStatus.ABORTED: "Uninstall aborted",
    }[result.status]
    notifier(title, result.message, result.status is not UninstallStatus.SUCCESS)
    return 0 if result.ok else 1


def _detached_creation_flags() -> int:
    if os.name != "nt":
        return 0
    return (
        subprocess.CREATE_NO_WINDOW
        | subprocess.DETACHED_PROCESS
        | subprocess.CREATE_NEW_PROCESS_GROUP
    )


def launch_installed_uninstaller_after_exit(
    layout: InstallLayout,
    *,
    wait_pid: int | None = None,
    popen: Callable[..., Any] = subprocess.Popen,
) -> int:
    """Start the installed uninstaller after this application process exits.

    The child receives only a validated, exact uninstaller path.  Waiting for
    the current application PID lets the normal uninstaller acquire the app
    mutex instead of racing shutdown when invoked from the tray.
    """

    install_dir = layout.validated_install_dir()
    uninstaller = validated_installed_uninstaller(layout)
    selected_pid = os.getpid() if wait_pid is None else wait_pid
    if selected_pid <= 0:
        raise ValueError("Invalid application process identifier")

    environment = os.environ.copy()
    environment[_RELOCATED_WAIT_PID_ENV] = str(selected_pid)
    environment["PYINSTALLER_RESET_ENVIRONMENT"] = "1"
    process = popen(
        [str(uninstaller)],
        cwd=str(install_dir),
        close_fds=True,
        creationflags=_detached_creation_flags(),
        env=environment,
    )
    pid = getattr(process, "pid", None)
    return int(pid) if isinstance(pid, int) and not isinstance(pid, bool) else 0


def validated_installed_uninstaller(layout: InstallLayout) -> Path:
    """Return the exact normal-file uninstaller from the validated install layout."""

    install_dir = layout.validated_install_dir()
    uninstaller = Path(os.path.abspath(layout.uninstaller))
    if (
        uninstaller.parent != install_dir
        or not os.path.lexists(uninstaller)
        or not uninstaller.is_file()
        or _is_reparse_point(uninstaller)
        or uninstaller.resolve() != uninstaller
    ):
        raise InstallationError("The installed uninstaller is missing or unsafe")
    return uninstaller


def launch_relocated_uninstaller(
    layout: InstallLayout,
    *,
    executable: Path | None = None,
    parent_pid: int | None = None,
    temp_root: Path | None = None,
    popen: Callable[..., Any] = subprocess.Popen,
) -> Path:
    """Copy the installed one-file executable to TEMP and launch it detached."""

    install_dir = layout.validated_install_dir()
    source = (executable or Path(sys.executable)).resolve()
    if source != layout.uninstaller.resolve() or source.parent != install_dir:
        raise InstallationError("Only the installed uninstaller can be relocated")
    root = (temp_root or Path(tempfile.gettempdir())).resolve()
    root.mkdir(parents=True, exist_ok=True)
    temp_dir = Path(tempfile.mkdtemp(prefix=_TEMP_PREFIX, dir=root))
    relocated = temp_dir / layout.uninstaller.name
    try:
        shutil.copy2(source, relocated)
        environment = os.environ.copy()
        environment[_RELOCATED_ENV] = "1"
        environment[_RELOCATED_DIR_ENV] = str(temp_dir)
        environment["PYINSTALLER_RESET_ENVIRONMENT"] = "1"
        environment[_RELOCATED_WAIT_PID_ENV] = str(
            os.getppid() if parent_pid is None else parent_pid
        )
        popen(
            [str(relocated)],
            cwd=str(temp_dir),
            close_fds=True,
            creationflags=_detached_creation_flags(),
            env=environment,
        )
    except Exception:
        shutil.rmtree(temp_dir, ignore_errors=True)
        raise
    return temp_dir


def wait_for_process_exit(
    pid: int,
    *,
    timeout_seconds: float = 30.0,
    process_factory: Callable[[int], Any] = psutil.Process,
) -> None:
    """Wait for the original one-file bootloader parent before deleting its EXE."""

    if pid <= 0 or pid == os.getpid():
        raise ValueError("Invalid parent process identifier")
    try:
        process = process_factory(pid)
        process.wait(timeout=timeout_seconds)
    except psutil.NoSuchProcess:
        return
    except psutil.TimeoutExpired as exc:
        raise RuntimeError("Timed out waiting for the installed uninstaller to exit") from exc


def _validated_temp_copy_dir(path: Path) -> Path:
    root = Path(tempfile.gettempdir()).resolve()
    lexical = Path(os.path.abspath(path))
    if lexical.parent != root or not lexical.name.startswith(_TEMP_PREFIX):
        raise ValueError("Temporary uninstaller directory is outside TEMP")
    if _is_reparse_point(lexical):
        raise ValueError("Temporary uninstaller directory cannot be a reparse point")
    resolved = lexical.resolve()
    if resolved.parent != root or not resolved.name.startswith(_TEMP_PREFIX):
        raise ValueError("Resolved temporary uninstaller directory is outside TEMP")
    return lexical


def cleanup_relocated_copy(
    temp_dir: Path,
    *,
    parent_pid: int | None = None,
    popen: Callable[..., Any] = subprocess.Popen,
) -> None:
    """Best-effort removal of the relocated one-file executable after it exits."""

    target = _validated_temp_copy_dir(temp_dir)
    try:
        shutil.rmtree(target)
        return
    except OSError:
        pass
    system_root = os.environ.get("SYSTEMROOT")
    if os.name != "nt" or not system_root:
        return
    powershell = Path(system_root) / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
    if not powershell.is_file():
        return
    script = f"""
$ErrorActionPreference = 'Stop'
$target = [IO.Path]::GetFullPath($env:{_TEMP_CLEANUP_DIR_ENV})
$root = [IO.Path]::GetFullPath($env:{_TEMP_CLEANUP_ROOT_ENV})
if (-not [StringComparer]::OrdinalIgnoreCase.Equals(
    [IO.Path]::GetDirectoryName($target).TrimEnd('\\'), $root.TrimEnd('\\')
)) {{ exit 2 }}
if (-not [IO.Path]::GetFileName($target).StartsWith('{_TEMP_PREFIX}')) {{ exit 2 }}
Wait-Process -Id ([int]$env:{_TEMP_CLEANUP_PID_ENV}) -ErrorAction SilentlyContinue
for ($attempt = 0; $attempt -lt 20; $attempt++) {{
    try {{ Remove-Item -LiteralPath $target -Recurse -Force -ErrorAction Stop }} catch {{}}
    if (-not (Test-Path -LiteralPath $target)) {{ exit 0 }}
    Start-Sleep -Milliseconds 250
}}
exit 1
""".strip()
    environment = os.environ.copy()
    environment[_TEMP_CLEANUP_DIR_ENV] = str(target)
    environment[_TEMP_CLEANUP_ROOT_ENV] = str(target.parent)
    environment[_TEMP_CLEANUP_PID_ENV] = str(os.getppid() if parent_pid is None else parent_pid)
    encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    try:
        popen(
            [
                str(powershell),
                "-NoLogo",
                "-NoProfile",
                "-NonInteractive",
                "-WindowStyle",
                "Hidden",
                "-EncodedCommand",
                encoded,
            ],
            close_fds=True,
            creationflags=_detached_creation_flags(),
            env=environment,
        )
    except OSError:
        return


def run_uninstall_entrypoint() -> int:
    """Relocate a frozen installed uninstaller before running interactive cleanup."""

    temp_dir_raw = os.environ.get(_RELOCATED_DIR_ENV)
    try:
        wait_pid_raw = os.environ.get(_RELOCATED_WAIT_PID_ENV)
        if wait_pid_raw:
            wait_for_process_exit(int(wait_pid_raw))

        layout = InstallLayout.discover()
        running = Path(sys.executable).resolve()
        relocated = os.environ.get(_RELOCATED_ENV) == "1"
        if (
            getattr(sys, "frozen", False)
            and not relocated
            and running == layout.uninstaller.resolve()
        ):
            launch_relocated_uninstaller(layout, executable=running)
            return 0
        return main()
    except (InstallationError, OSError, RuntimeError, ValueError) as exc:
        show_result("Uninstall failed", str(exc), True)
        return 1
    finally:
        if temp_dir_raw:
            with suppress(OSError, ValueError):
                cleanup_relocated_copy(Path(temp_dir_raw))


if __name__ == "__main__":
    raise SystemExit(run_uninstall_entrypoint())


__all__ = [
    "BLOCKING_PROCESSES",
    "RemovalState",
    "UninstallResult",
    "UninstallStatus",
    "Uninstaller",
    "cleanup_relocated_copy",
    "find_running_process",
    "launch_installed_uninstaller_after_exit",
    "launch_relocated_uninstaller",
    "main",
    "remove_app_data_tree",
    "remove_apps_registration",
    "remove_install_tree",
    "remove_user_startup_registration",
    "run_uninstall_entrypoint",
    "validated_installed_uninstaller",
    "wait_for_process_exit",
]
