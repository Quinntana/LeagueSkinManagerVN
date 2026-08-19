"""Application composition root."""

from __future__ import annotations

import ctypes
import logging
import sys
from argparse import ArgumentParser
from collections.abc import Callable
from pathlib import Path
from threading import Event, Thread

from .catalog import CatalogError, load_catalog
from .config import (
    APP_VERSION,
    CSLOL_RELEASES_URL,
    LEAGUE_PROCESS_NAME,
    LTK_PROCESS_NAMES,
    MANAGER_PROCESS_NAME,
    AppPaths,
    RuntimeConfig,
)
from .controller import AppController, AppState
from .cooldown_timer import CooldownTimerStore, CsvCooldownEventSink, SystemClock
from .cooldown_window import CooldownBoard, create_cooldown_window
from .installation import InstallationError, InstallLayout, is_installed_executable
from .logging_setup import configure_logging
from .ltk_cleanup import LtkSkinCleanupService
from .ltk_companion import (
    LtkCompanion,
    LtkInstallLocator,
    LtkReleaseClient,
    PowerShellAuthenticodeVerifier,
)
from .ltk_library import summarize_library
from .ltk_migration import LtkMigrationService, LtkReconcileError
from .ltk_tasks import LtkTaskCoordinator, wait_for_ltk_tasks
from .manager_update import ManagerReleaseClient, ManagerUpdater
from .operation_gate import OperationGate
from .process_monitor import LeagueProcessMonitor, WindowsProcessLookup
from .skin_source import GitHubSkinSource
from .sync_service import ManagedStateError, SkinSyncService
from .tray import TrayApplication
from .uninstall import (
    launch_installed_uninstaller_after_exit,
    validated_installed_uninstaller,
)
from .window_host import WindowBoundary, WindowHost
from .windows_integration import (
    InstanceActivationEvent,
    ProcessLauncher,
    SingleInstanceMutex,
    StartupRegistration,
    open_path,
    running_executable,
)
from .workflow import SynchronizationWorkflow


def _confirm_remove_all_ltk_skins(storage_dir: Path) -> bool:
    """Show one explicit, default-No confirmation for the destructive tray action."""

    if sys.platform != "win32":
        return False
    message = (
        "Permanently remove every skin from LTK Manager?\n\n"
        f"LTK skin storage: {storage_dir}\n\n"
        "This removes every skin package, extracted metadata, WAD report, and generated "
        "profile overlay in that LTK library, including any skin you added yourself.\n\n"
        "LTK Manager itself, its settings, and its logs are kept. A later rebuild restores "
        "the current skin set. Close LTK Manager and its patcher before continuing."
    )
    yes = 6
    yes_no = 0x00000004
    warning = 0x00000030
    default_no = 0x00000100
    result = ctypes.windll.user32.MessageBoxW(
        None,
        message,
        "Remove all LTK skins",
        yes_no | warning | default_no,
    )
    return int(result) == yes


def _launch_preferred_manager(
    *,
    launcher: ProcessLauncher,
    manager_dir: Path,
    manager_executable: Path,
    locate_ltk: Callable[[], object | None],
    ltk_is_running: Callable[[], bool],
    logger: logging.Logger,
) -> bool:
    """Start the preferred skin manager backend for a League session.

    The installed official LTK Manager is preferred; the app-owned CSLOL
    Manager remains the fallback so the workflow keeps working before LTK has
    been installed or if its executable cannot be started.
    """

    installation: object | None
    try:
        if ltk_is_running():
            return True
        installation = locate_ltk()
    except Exception:
        logger.exception("Could not inspect the LTK installation; using CSLOL Manager")
        installation = None
    if installation is not None:
        executable = getattr(installation, "executable", None)
        if isinstance(executable, Path) and launcher.launch(executable):
            return True
        logger.warning("Installed LTK Manager could not be started; using CSLOL Manager")
    if launcher.is_running_under(manager_dir):
        return True
    return launcher.launch(manager_executable)


class _LeagueExitObserver:
    """Forward monitor callbacks and signal after the League client exits."""

    def __init__(
        self,
        inner: LeagueProcessMonitor,
        on_exit: Callable[[], object],
        logger: logging.Logger,
    ) -> None:
        self._inner = inner
        self._on_exit = on_exit
        self._logger = logger

    def run(
        self,
        stop_event: Event,
        changed: Callable[[int | None], None],
    ) -> None:
        def observed(pid: int | None) -> None:
            changed(pid)
            if pid is None and not stop_event.is_set():
                try:
                    self._on_exit()
                except Exception:
                    self._logger.exception("League-exit follow-up work failed")

        self._inner.run(stop_event, observed)


def _wait_for_workers(
    controller: AppController,
    timeout_seconds: float,
    logger: logging.Logger,
) -> None:
    """Retain process-owned resources until every non-daemon worker has stopped."""

    while not controller.shutdown(timeout_seconds):
        logger.warning("Background work is still stopping; retaining app resources and mutex")


def _wait_for_window(
    window_host: WindowHost,
    timeout_seconds: float,
    logger: logging.Logger,
) -> None:
    """Retain process ownership until the optional non-daemon UI thread exits."""

    while not window_host.stop(timeout_seconds):
        logger.warning("Optional window is still stopping; retaining app resources and mutex")


def _listen_for_activation(
    activation_event: InstanceActivationEvent,
    stop_event: Event,
    on_activate: Callable[[], object],
    logger: logging.Logger,
) -> None:
    """Wait for later launches and notify the already-running tray instance."""

    while not stop_event.is_set():
        try:
            activated = activation_event.wait(250)
        except Exception:
            logger.exception("Application activation listener failed")
            return
        if not activated or stop_event.is_set():
            continue
        try:
            on_activate()
        except Exception:
            logger.exception("Could not handle the application activation request")


def _refresh_ltk_library_summary(
    reconciler: LtkMigrationService,
    tray: TrayApplication,
    ltk_is_installed: Callable[[], bool],
    logger: logging.Logger,
    *,
    with_drift: bool = True,
) -> None:
    """Refresh the read-only LTK library summary without changing anything.

    ``with_drift`` is False on the startup path so the tray becomes visible
    without first comparing every package against LTK's storage.
    """

    try:
        installed = ltk_is_installed()
    except Exception:
        logger.exception("Could not inspect the LTK installation state")
        installed = False
    if not installed:
        tray.update_ltk_library(installed=False)
        return
    summary = summarize_library(reconciler.resolve_storage_dir())
    if summary is None:
        tray.update_ltk_library(installed=True)
        return
    if not with_drift:
        tray.update_ltk_library(
            installed=True,
            in_library=summary.in_library,
            enabled=summary.enabled,
        )
        return
    try:
        baseline = reconciler.inspect_baseline()
    except (LtkReconcileError, ManagedStateError) as exc:
        logger.warning("Could not compare LTK with the current skin set: %s", exc)
        tray.update_ltk_library(
            installed=True,
            in_library=summary.in_library,
            enabled=summary.enabled,
        )
        return
    tray.update_ltk_library(
        installed=True,
        in_library=summary.in_library,
        enabled=summary.enabled,
        expected=baseline.expected,
        pending=baseline.missing + baseline.extra,
    )


def run(*, sync_on_start: bool = True) -> int:
    logger = logging.getLogger("league_skin_manager")
    if sys.platform != "win32":
        logger.error("LeagueSkinManagerVN is Windows-only")
        return 1

    activation_candidate = InstanceActivationEvent()
    activation_event: InstanceActivationEvent | None = activation_candidate
    try:
        activation_candidate.create()
    except Exception:
        logger.exception("Could not create the application activation event")
        activation_candidate.close()
        activation_event = None

    mutex = SingleInstanceMutex()
    if not mutex.acquire():
        if activation_event is not None:
            try:
                activation_event.signal()
                logger.info("Notified the active tray instance about this later launch")
            except Exception:
                logger.exception("Could not activate the existing application instance")
            finally:
                activation_event.close()
        return 2

    source: GitHubSkinSource | None = None
    release_client: ManagerReleaseClient | None = None
    ltk_release_client: LtkReleaseClient | None = None
    ltk_companion: LtkCompanion | None = None
    ltk_tasks: LtkTaskCoordinator | None = None
    controller: AppController | None = None
    cooldown_host: WindowHost | None = None
    tray: TrayApplication | None = None
    activation_stop = Event()
    activation_thread: Thread | None = None
    runtime = RuntimeConfig()
    try:
        paths = AppPaths.discover()
        paths.ensure()
        logger = configure_logging(paths.log_dir)
        launcher = ProcessLauncher(logger.getChild("launcher"))
        manager_executable = paths.manager_dir / MANAGER_PROCESS_NAME
        operation_gate = OperationGate()
        source = GitHubSkinSource(
            attempts=runtime.download_attempts,
            logger=logger.getChild("skin_source"),
        )
        sync_service = SkinSyncService(
            paths.installed_dir,
            paths.managed_manifest_file,
            cache_dir=paths.package_cache_dir,
            max_workers=runtime.download_workers,
            manager_is_running=lambda: launcher.is_running_under(paths.manager_dir),
        )
        release_client = ManagerReleaseClient(
            CSLOL_RELEASES_URL,
            logger.getChild("manager_release"),
        )
        manager_updater = ManagerUpdater(
            release_client,
            paths.manager_dir,
            paths.manager_version_file,
            lambda: launcher.is_running_under(paths.manager_dir),
            logger.getChild("manager_update"),
        )
        workflow = SynchronizationWorkflow(
            source=source,
            sync_service=sync_service,
            manager_updater=manager_updater,
            manager_executable=manager_executable,
            installed_dir=paths.installed_dir,
            logger=logger.getChild("sync"),
            manager_is_running=lambda: launcher.is_running_under(paths.manager_dir),
        )
        monitor = LeagueProcessMonitor(
            WindowsProcessLookup(),
            LEAGUE_PROCESS_NAME,
            runtime.process_poll_seconds,
            logger.getChild("process_monitor"),
        )
        startup = StartupRegistration()
        executable = running_executable()
        install_layout: InstallLayout | None
        try:
            install_layout = InstallLayout.discover()
        except (InstallationError, OSError, RuntimeError, ValueError) as exc:
            logger.warning(
                "Installed-layout discovery was unavailable; treating this copy as portable: %s",
                exc,
            )
            install_layout = None
        installed_copy = install_layout is not None and is_installed_executable(
            executable, install_layout
        )
        runtime_label = f"{'Installed' if installed_copy else 'Portable'} v{APP_VERSION}"
        logger.info("Runtime ownership: %s (%s)", runtime_label, executable)

        def ltk_is_running() -> bool:
            return launcher.is_any_running(LTK_PROCESS_NAMES)

        ltk_locator = LtkInstallLocator(excluded_roots=(paths.ltk_cache_dir,))

        def launch_manager() -> bool:
            return _launch_preferred_manager(
                launcher=launcher,
                manager_dir=paths.manager_dir,
                manager_executable=manager_executable,
                locate_ltk=ltk_locator.locate,
                ltk_is_running=ltk_is_running,
                logger=logger.getChild("manager_launch"),
            )

        def ltk_is_installed() -> bool:
            return ltk_locator.locate() is not None

        # Both boundaries are late-bound on purpose: the cleanup service needs
        # the reconciler's storage resolver, and these are only ever invoked
        # from a reconcile long after composition has finished.
        def remove_ltk_mods(mod_ids: tuple[str, ...]) -> object:
            return cleanup.remove_mods(mod_ids)

        def clear_ltk_toggles() -> int:
            return cleanup.clear_enabled_mods()

        reconciler = LtkMigrationService(
            paths.managed_manifest_file,
            paths.package_cache_dir,
            ltk_app_data_dir=paths.ltk_data_dir,
            report_dir=paths.migration_report_dir,
            archive_index_path=paths.ltk_archive_index_file,
            package_index_path=paths.ltk_package_index_file,
            cslol_is_running=lambda: launcher.is_running_under(paths.manager_dir),
            ltk_is_running=ltk_is_running,
            remove_ltk_mods=remove_ltk_mods,
            clear_ltk_toggles=clear_ltk_toggles,
        )
        cleanup = LtkSkinCleanupService(
            reconciler.resolve_storage_dir,
            ltk_is_running=ltk_is_running,
        )

        def startup_enabled() -> bool:
            return startup.is_enabled(executable)

        def set_startup_enabled(enabled: bool) -> bool:
            if enabled and not installed_copy:
                notify_user(
                    "Start with Windows",
                    "Only the installed copy can own Windows startup. "
                    "Run LeagueSkinManagerVNSetup.exe to install this version first.",
                )
                return False
            startup.set_enabled(executable, enabled)
            return True

        def notify_user(title: str, message: str) -> None:
            if tray is not None:
                tray.notify(title, message)

        def open_cslol_skins() -> bool:
            open_path(paths.installed_dir)
            return True

        def open_data() -> bool:
            open_path(paths.data_dir)
            return True

        def open_log() -> bool:
            log_file = paths.log_dir / "LeagueSkinManagerVN.log"
            if not log_file.is_file():
                notify_user(
                    "Diagnostics log",
                    f"The diagnostics log has not been created yet: {log_file}",
                )
                return False
            open_path(log_file)
            return True

        def open_ltk_skins() -> bool:
            storage_dir = reconciler.resolve_storage_dir()
            archives_dir = storage_dir / "archives"
            target = archives_dir if archives_dir.is_dir() else storage_dir
            if not target.is_dir():
                notify_user(
                    "Skins in LTK",
                    "LTK has not created its skin storage yet. Open LTK Manager once first.",
                )
                return False
            open_path(target)
            return True

        def request_ltk_cleanup() -> bool:
            storage_dir = reconciler.resolve_storage_dir()
            if not _confirm_remove_all_ltk_skins(storage_dir):
                return False
            return active_ltk_tasks().request_cleanup()

        def open_cooldowns() -> bool:
            if cooldown_host is None:
                return False
            return cooldown_host.show()

        def active_controller() -> AppController:
            if controller is None:
                raise RuntimeError("Application controller is not initialized")
            return controller

        def active_ltk_tasks() -> LtkTaskCoordinator:
            if ltk_tasks is None:
                raise RuntimeError("LTK companion is not initialized")
            return ltk_tasks

        def start_services() -> bool:
            controller_started = active_controller().start()
            if not active_ltk_tasks().start():
                logger.error("LTK companion background worker could not be started")
            return controller_started

        def shutdown_services() -> bool:
            controller_stopped = active_controller().shutdown(runtime.shutdown_timeout_seconds)
            ltk_stopped = active_ltk_tasks().shutdown(runtime.shutdown_timeout_seconds)
            return ltk_stopped and controller_stopped

        def exit_from_tray() -> bool:
            result = shutdown_services()
            if result and cooldown_host is not None:
                result = cooldown_host.stop(runtime.shutdown_timeout_seconds)
                if not result:
                    notify_user(
                        "LeagueSkinManagerVN",
                        "The cooldown timer window is still closing. "
                        "Close it, then try Exit again.",
                    )
            return result

        def uninstall_from_tray() -> bool:
            if not installed_copy or install_layout is None:
                notify_user(
                    "Uninstall LeagueSkinManagerVN",
                    "This is the portable copy, so it does not own the installed program files. "
                    "Use Windows Apps & Features to remove the installed copy.",
                )
                return False
            try:
                validated_installed_uninstaller(install_layout)
            except InstallationError as exc:
                logger.warning("Tray uninstall is unavailable: %s", exc)
                notify_user(
                    "Uninstall LeagueSkinManagerVN",
                    f"The installed uninstaller is unavailable: {exc}",
                )
                return False
            if not shutdown_services():
                return False
            if cooldown_host is not None and not cooldown_host.stop(
                runtime.shutdown_timeout_seconds
            ):
                notify_user(
                    "Uninstall LeagueSkinManagerVN",
                    "Close the cooldown timer window, then try again.",
                )
                return False
            try:
                launch_installed_uninstaller_after_exit(install_layout)
            except (InstallationError, OSError, ValueError) as exc:
                logger.exception("Could not start the installed uninstaller from the tray")
                notify_user(
                    "Uninstall LeagueSkinManagerVN",
                    "Background services stopped, but the uninstaller could not be started: "
                    f"{exc}. Restart LeagueSkinManagerVN or uninstall it from Windows Settings.",
                )
                return False
            return True

        def create_cooldowns() -> WindowBoundary:
            sink = CsvCooldownEventSink(paths.cooldown_event_file)
            board = CooldownBoard(
                CooldownTimerStore(SystemClock(), sink),
                flush=sink.flush,
                logger=logger.getChild("cooldowns"),
            )
            return create_cooldown_window(board, logger.getChild("cooldown_window"))

        cooldown_host = WindowHost(
            create_cooldowns,
            title="Enemy cooldown timers",
            thread_name="cooldown-timer-ui",
            failure_sink=notify_user,
            logger=logger.getChild("cooldown_host"),
        )
        tray = TrayApplication(
            on_start=start_services,
            on_sync=lambda: active_controller().request_sync(),
            on_open_ltk=lambda: active_ltk_tasks().request_start(),
            on_open_cooldowns=open_cooldowns,
            on_open_ltk_skins=open_ltk_skins,
            on_open_cslol_skins=open_cslol_skins,
            on_open_data=open_data,
            on_open_log=open_log,
            on_rebuild_library=lambda: active_ltk_tasks().request_rebuild(),
            on_remove_ltk_skins=request_ltk_cleanup,
            on_uninstall=uninstall_from_tray,
            on_exit=exit_from_tray,
            startup_enabled=startup_enabled,
            set_startup_enabled=set_startup_enabled,
            runtime_label=runtime_label,
            uninstall_available=installed_copy,
            startup_available=installed_copy or startup_enabled(),
            logger=logger.getChild("tray"),
        )

        ltk_release_client = LtkReleaseClient()
        ltk_companion = LtkCompanion(
            ltk_release_client,
            ltk_locator,
            PowerShellAuthenticodeVerifier(),
            paths.ltk_cache_dir,
        )

        def refresh_catalog_summary() -> None:
            if tray is None:
                return
            try:
                catalog = load_catalog(paths.managed_manifest_file)
            except CatalogError as exc:
                logger.warning("Could not read the local tray catalog summary: %s", exc)
                return
            tray.update_library(len(catalog.skins), catalog.patch)

        def refresh_ltk_library_summary(*, with_drift: bool = True) -> None:
            if tray is None:
                return
            _refresh_ltk_library_summary(
                reconciler,
                tray,
                ltk_is_installed,
                logger.getChild("ltk_library"),
                with_drift=with_drift,
            )

        def update_ltk_status(detail: str, rebuild_active: bool) -> None:
            if tray is not None:
                tray.update_ltk_status(detail, rebuild_active=rebuild_active)

        def notify_from_ltk(title: str, message: str) -> None:
            if tray is not None:
                tray.notify(title, message)

        ltk_tasks = LtkTaskCoordinator(
            companion=ltk_companion,
            reconciler=reconciler,
            cleanup=cleanup,
            operation_gate=operation_gate,
            ltk_is_running=ltk_is_running,
            ltk_is_installed=ltk_is_installed,
            resume_cslol_launches=lambda: active_controller().resume_pending_manager_launches(),
            notify_sink=notify_from_ltk,
            status_sink=update_ltk_status,
            library_state_changed_sink=refresh_ltk_library_summary,
            logger=logger.getChild("ltk_tasks"),
        )

        def update_status(state: AppState, detail: str) -> None:
            if tray is not None:
                tray.update_status(state, detail)
            if state in (AppState.READY, AppState.OFFLINE_READY):
                refresh_catalog_summary()
                refresh_ltk_library_summary()
            if state is AppState.READY and ltk_tasks is not None:
                # A successful sync is the event that can change the skin set,
                # so it is also when LTK's library may have drifted from it.
                ltk_tasks.request_rebuild(automatic=True)

        controller = AppController(
            sync=workflow,
            launcher=launch_manager,
            monitor=_LeagueExitObserver(
                monitor,
                # A deferred rebuild retries once the game session ends and its
                # manager processes are expected to wind down.
                lambda: active_ltk_tasks().retry_deferred_rebuild(),
                logger.getChild("league_exit"),
            ),
            status_sink=update_status,
            notify_sink=tray.notify,
            sync_on_start=sync_on_start,
            shutdown_timeout_seconds=runtime.shutdown_timeout_seconds,
            operation_gate=operation_gate,
            manager_label="Skin manager",
            logger=logger.getChild("controller"),
        )

        refresh_catalog_summary()
        # Drift is deliberately not computed here: the tray must become visible
        # before anything inspects LTK's storage. The first rebuild publishes it.
        refresh_ltk_library_summary(with_drift=False)
        logger.info("Application starting from %s", Path(sys.executable))
        if activation_event is not None:
            activation_thread = Thread(
                target=_listen_for_activation,
                args=(
                    activation_event,
                    activation_stop,
                    lambda: notify_user(
                        "LeagueSkinManagerVN",
                        "LeagueSkinManagerVN is already running in the system tray. "
                        "Right-click its icon to open LTK, sync skins, or find its folders.",
                    ),
                    logger.getChild("activation"),
                ),
                name="instance-activation-listener",
                daemon=False,
            )
            activation_thread.start()
        tray.run()
        return 0
    except KeyboardInterrupt:
        logger.info("Application interrupted")
        return 0
    except Exception:
        logger.exception("Application startup or system tray failed")
        return 1
    finally:
        activation_stop.set()
        if activation_thread is not None and activation_thread.is_alive():
            activation_thread.join(1.0)
            if activation_thread.is_alive():
                logger.warning("Application activation listener did not stop promptly")
        if activation_event is not None:
            activation_event.close()
        if controller is not None:
            _wait_for_workers(controller, runtime.shutdown_timeout_seconds, logger)
        if ltk_tasks is not None:
            wait_for_ltk_tasks(ltk_tasks, runtime.shutdown_timeout_seconds, logger)
        if tray is not None:
            tray.stop()
        if cooldown_host is not None:
            _wait_for_window(
                cooldown_host,
                runtime.shutdown_timeout_seconds,
                logger,
            )
        try:
            if source is not None:
                source.close()
        except Exception:
            logger.exception("Could not close the skin source client")
        try:
            if release_client is not None:
                release_client.close()
        except Exception:
            logger.exception("Could not close the manager release client")
        if ltk_tasks is None and ltk_companion is not None:
            try:
                ltk_companion.close()
            except Exception:
                logger.exception("Could not close the LTK companion")
        elif ltk_companion is None and ltk_release_client is not None:
            try:
                ltk_release_client.close()
            except Exception:
                logger.exception("Could not close the LTK release client")
        mutex.release()


def main() -> None:
    parser = ArgumentParser(prog="LeagueSkinManagerVN")
    parser.add_argument(
        "--no-sync",
        action="store_true",
        help="Start from installed data without running the startup synchronization.",
    )
    parser.add_argument(
        "--background",
        action="store_true",
        help=(
            "Accepted for compatibility with the Windows startup entry; the tray is "
            "always the only interface."
        ),
    )
    arguments = parser.parse_args()
    raise SystemExit(run(sync_on_start=not arguments.no_sync))
