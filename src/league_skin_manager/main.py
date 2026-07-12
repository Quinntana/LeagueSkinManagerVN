"""Application composition root."""

from __future__ import annotations

import logging
import sys
from argparse import ArgumentParser
from pathlib import Path

from .config import (
    CSLOL_RELEASES_URL,
    LEAGUE_PROCESS_NAME,
    MANAGER_PROCESS_NAME,
    AppPaths,
    RuntimeConfig,
)
from .controller import AppController
from .logging_setup import configure_logging
from .manager_update import ManagerReleaseClient, ManagerUpdater
from .process_monitor import LeagueProcessMonitor, PsutilProcessLookup
from .skin_source import GitHubSkinSource
from .sync_service import SkinSyncService
from .tray import TrayApplication
from .windows_integration import (
    ProcessLauncher,
    SingleInstanceMutex,
    StartupRegistration,
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


def run(*, sync_on_start: bool = True) -> int:
    paths = AppPaths.discover()
    paths.ensure()
    logger = configure_logging(paths.log_dir)
    if sys.platform != "win32":
        logger.error("LeagueSkinManagerVN is Windows-only")
        return 1

    mutex = SingleInstanceMutex()
    if not mutex.acquire():
        logger.info("Another service instance is active; exiting without launching manager")
        return 2

    source: GitHubSkinSource | None = None
    release_client: ManagerReleaseClient | None = None
    controller: AppController | None = None
    runtime = RuntimeConfig()
    try:
        launcher = ProcessLauncher(logger.getChild("launcher"))
        manager_executable = paths.manager_dir / MANAGER_PROCESS_NAME
        source = GitHubSkinSource(
            attempts=runtime.download_attempts,
            logger=logger.getChild("skin_source"),
        )
        sync_service = SkinSyncService(
            paths.installed_dir,
            paths.managed_manifest_file,
            cache_dir=paths.package_cache_dir,
            max_workers=runtime.download_workers,
        )
        release_client = ManagerReleaseClient(
            CSLOL_RELEASES_URL,
            logger.getChild("manager_release"),
        )
        manager_updater = ManagerUpdater(
            release_client,
            paths.manager_dir,
            paths.manager_version_file,
            lambda: launcher.is_running(MANAGER_PROCESS_NAME),
            logger.getChild("manager_update"),
        )
        workflow = SynchronizationWorkflow(
            source=source,
            sync_service=sync_service,
            manager_updater=manager_updater,
            manager_executable=manager_executable,
            installed_dir=paths.installed_dir,
            logger=logger.getChild("sync"),
        )
        monitor = LeagueProcessMonitor(
            PsutilProcessLookup(),
            LEAGUE_PROCESS_NAME,
            runtime.process_poll_seconds,
            logger.getChild("process_monitor"),
        )
        startup = StartupRegistration()
        executable = running_executable()

        def launch_manager() -> bool:
            if launcher.is_running(MANAGER_PROCESS_NAME):
                return True
            return launcher.launch(manager_executable)

        def startup_enabled() -> bool:
            return startup.is_enabled(executable)

        def set_startup_enabled(enabled: bool) -> bool:
            startup.set_enabled(executable, enabled)
            return True

        def active_controller() -> AppController:
            if controller is None:
                raise RuntimeError("Application controller is not initialized")
            return controller

        tray = TrayApplication(
            on_start=lambda: active_controller().start(),
            on_sync=lambda: active_controller().request_sync(),
            on_start_manager=lambda: active_controller().start_manager(),
            startup_enabled=startup_enabled,
            set_startup_enabled=set_startup_enabled,
            on_exit=lambda: active_controller().shutdown(runtime.shutdown_timeout_seconds),
            logger=logger.getChild("tray"),
        )
        controller = AppController(
            sync=workflow,
            launcher=launch_manager,
            monitor=monitor,
            status_sink=tray.update_status,
            notify_sink=tray.notify,
            sync_on_start=sync_on_start,
            shutdown_timeout_seconds=runtime.shutdown_timeout_seconds,
            logger=logger.getChild("controller"),
        )

        logger.info("Application starting from %s", Path(sys.executable))
        tray.run()
        return 0
    except KeyboardInterrupt:
        logger.info("Application interrupted")
        return 0
    except Exception:
        logger.exception("Application startup or system tray failed")
        return 1
    finally:
        if controller is not None:
            _wait_for_workers(controller, runtime.shutdown_timeout_seconds, logger)
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
        finally:
            mutex.release()


def main() -> None:
    parser = ArgumentParser(prog="LeagueSkinManagerVN")
    parser.add_argument(
        "--no-sync",
        action="store_true",
        help="Start from installed data without running the startup synchronization.",
    )
    arguments = parser.parse_args()
    raise SystemExit(run(sync_on_start=not arguments.no_sync))
