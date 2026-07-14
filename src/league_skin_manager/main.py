"""Application composition root."""

from __future__ import annotations

import logging
import sys
from argparse import ArgumentParser
from collections.abc import Callable
from pathlib import Path
from threading import Event, Thread

from .config import (
    CSLOL_RELEASES_URL,
    LEAGUE_PROCESS_NAME,
    LTK_PROCESS_NAMES,
    MANAGER_PROCESS_NAME,
    AppPaths,
    RuntimeConfig,
)
from .controller import AppController, AppState
from .desktop import DesktopApplication
from .logging_setup import configure_logging
from .ltk_companion import (
    LtkCompanion,
    LtkInstallLocator,
    LtkReleaseClient,
    PowerShellAuthenticodeVerifier,
)
from .ltk_migration import LtkMigrationService
from .ltk_tasks import LtkTaskCoordinator, wait_for_ltk_tasks
from .manager_update import ManagerReleaseClient, ManagerUpdater
from .operation_gate import OperationGate
from .process_monitor import LeagueProcessMonitor, WindowsProcessLookup
from .skin_source import GitHubSkinSource
from .sync_service import SkinSyncService
from .tray import TrayApplication
from .windows_integration import (
    InstanceActivationEvent,
    ProcessLauncher,
    SingleInstanceMutex,
    StartupRegistration,
    open_path,
    running_executable,
)
from .workflow import SynchronizationWorkflow


def _wait_for_workers(
    controller: AppController,
    timeout_seconds: float,
    logger: logging.Logger,
) -> None:
    """Retain process-owned resources until every non-daemon worker has stopped."""

    while not controller.shutdown(timeout_seconds):
        logger.warning("Background work is still stopping; retaining app resources and mutex")


def _listen_for_activation(
    activation_event: InstanceActivationEvent,
    stop_event: Event,
    on_activate: Callable[[], object],
    logger: logging.Logger,
) -> None:
    """Wait for later launches and marshal their request to the desktop queue."""

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
            logger.exception("Could not show the desktop after application activation")


def run(*, sync_on_start: bool = True, show_window: bool = True) -> int:
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
                logger.info("Asked the active application instance to show its desktop")
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
    desktop: DesktopApplication | None = None
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

        def launch_manager() -> bool:
            if launcher.is_running_under(paths.manager_dir):
                return True
            return launcher.launch(manager_executable)

        def ltk_is_running() -> bool:
            return launcher.is_any_running(LTK_PROCESS_NAMES)

        def startup_enabled() -> bool:
            return startup.is_enabled(executable)

        def set_startup_enabled(enabled: bool) -> bool:
            startup.set_enabled(executable, enabled)
            return True

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
            if result and desktop is not None:
                desktop.stop()
            return result

        def exit_from_desktop() -> bool:
            result = shutdown_services()
            if result and tray is not None:
                tray.stop()
            return result

        desktop = DesktopApplication(
            catalog_path=paths.managed_manifest_file,
            installed_dir=paths.installed_dir,
            data_dir=paths.data_dir,
            log_file=paths.log_dir / "LeagueSkinManagerVN.log",
            on_sync=lambda: active_controller().request_sync(),
            on_start_manager=lambda: active_controller().start_manager(),
            on_start_ltk=lambda: active_ltk_tasks().request_start(),
            on_migrate_to_ltk=lambda source: active_ltk_tasks().request_migration(source),
            on_cancel_ltk_migration=lambda: active_ltk_tasks().cancel_migration(),
            on_reset_ltk_migration=lambda: active_ltk_tasks().request_history_reset(),
            on_exit=exit_from_desktop,
            startup_enabled=startup_enabled,
            set_startup_enabled=set_startup_enabled,
            path_opener=open_path,
            logger=logger.getChild("desktop"),
        )
        tray = TrayApplication(
            on_start=start_services,
            on_show=desktop.show,
            on_sync=lambda: active_controller().request_sync(),
            on_start_manager=lambda: active_controller().start_manager(),
            on_start_ltk=lambda: active_ltk_tasks().request_start(),
            on_migrate_to_ltk=desktop.request_ltk_migration,
            startup_enabled=startup_enabled,
            set_startup_enabled=set_startup_enabled,
            on_exit=exit_from_tray,
            logger=logger.getChild("tray"),
        )

        ltk_release_client = LtkReleaseClient()
        ltk_locator = LtkInstallLocator(excluded_roots=(paths.ltk_cache_dir,))
        ltk_companion = LtkCompanion(
            ltk_release_client,
            ltk_locator,
            PowerShellAuthenticodeVerifier(),
            paths.ltk_cache_dir,
        )
        migration = LtkMigrationService(
            paths.managed_manifest_file,
            paths.package_cache_dir,
            ltk_app_data_dir=paths.ltk_data_dir,
            report_dir=paths.migration_report_dir,
            migration_state_path=paths.ltk_migration_state_file,
            cslol_is_running=lambda: launcher.is_running_under(paths.manager_dir),
            ltk_is_running=ltk_is_running,
        )

        def update_ltk_status(detail: str, migration_active: bool) -> None:
            if desktop is not None:
                desktop.update_ltk_status(detail, migration_active=migration_active)

        def notify_from_ltk(title: str, message: str) -> None:
            if tray is not None:
                tray.notify(title, message)

        ltk_tasks = LtkTaskCoordinator(
            companion=ltk_companion,
            migration=migration,
            operation_gate=operation_gate,
            ltk_is_running=ltk_is_running,
            resume_cslol_launches=lambda: active_controller().resume_pending_manager_launches(),
            notify_sink=notify_from_ltk,
            status_sink=update_ltk_status,
            logger=logger.getChild("ltk_tasks"),
        )

        def update_status(state: AppState, detail: str) -> None:
            if tray is not None:
                tray.update_status(state, detail)
            if desktop is not None:
                desktop.update_status(state, detail)

        controller = AppController(
            sync=workflow,
            launcher=launch_manager,
            monitor=monitor,
            status_sink=update_status,
            notify_sink=tray.notify,
            sync_on_start=sync_on_start,
            shutdown_timeout_seconds=runtime.shutdown_timeout_seconds,
            operation_gate=operation_gate,
            logger=logger.getChild("controller"),
        )

        logger.info("Application starting from %s", Path(sys.executable))
        if activation_event is not None:
            activation_thread = Thread(
                target=_listen_for_activation,
                args=(
                    activation_event,
                    activation_stop,
                    desktop.show,
                    logger.getChild("activation"),
                ),
                name="instance-activation-listener",
                daemon=False,
            )
            activation_thread.start()
        tray.run_detached()
        desktop.run(show_on_start=show_window)
        return 0
    except KeyboardInterrupt:
        logger.info("Application interrupted")
        return 0
    except Exception:
        logger.exception("Application startup, desktop, or system tray failed")
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
        if desktop is not None:
            desktop.stop()
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
        help="Start with the desktop window hidden and remain available in the tray.",
    )
    arguments = parser.parse_args()
    raise SystemExit(
        run(
            sync_on_start=not arguments.no_sync,
            show_window=not arguments.background,
        )
    )
