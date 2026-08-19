"""Composition root: the only module that knows concrete types.

Everything below this file talks to Protocols and plain values.  This is where
adapters are constructed and wired to the shell, and where the application's
few sequencing decisions live.
"""

from __future__ import annotations

import logging
import sys
from dataclasses import dataclass
from pathlib import Path
from threading import Event, Lock, Thread

from . import ltk, porofessor, windows
from . import settings as settings_module
from .cache import PackageCache
from .config import APP_DISPLAY_NAME, AppPaths
from .github import GitHubSkinSource
from .logging_setup import configure_logging
from .process_watch import GameWatcher
from .settings import Settings
from .sync import synchronize
from .tray import Tray, TrayActions, TrayState
from .uninstall import uninstall

LOGGER = logging.getLogger(__name__)

BLOCKED_MESSAGE = (
    "LTK Manager is already installed and was not installed by this application. "
    "Skin syncing is disabled so an existing library is never wiped. "
    "Uninstall LTK Manager and restart to let this application manage it."
)


@dataclass
class App:
    """Wires the adapters to the tray and owns the background worker."""

    paths: AppPaths
    executable: Path
    settings: Settings
    logger: logging.Logger = LOGGER

    def __post_init__(self) -> None:
        self._lock = Lock()
        self._stop = Event()
        self._worker: Thread | None = None
        self._source = GitHubSkinSource(logger=self.logger.getChild("github"))
        self._cache = PackageCache(self.paths.package_cache_dir, self.logger.getChild("cache"))
        self._blocked: str | None = None
        self._cooldown_open = False
        self._suppressed_for_match = False

        self.tray = Tray(
            actions=TrayActions(
                open_ltk=self._open_ltk,
                sync=self._request_sync,
                open_cooldowns=self._open_cooldowns,
                get_porofessor=porofessor.open_download_page,
                open_folder=lambda: windows.open_path(self.paths.data_dir),
                set_cooldown_auto_run=self._set_cooldown_auto_run,
                set_startup=self._set_startup,
                set_opacity=self._set_opacity,
                set_scale=self._set_scale,
                uninstall=self._uninstall,
                exit_app=self._exit,
            ),
            state=TrayState(
                skins=self.settings.skins,
                patch=self.settings.patch,
                synced_at=self.settings.synced_at,
                ltk_installed=ltk.locate() is not None,
                cooldown_auto_run=self.settings.cooldown_auto_run,
                startup_enabled=windows.startup_enabled(self.executable),
                opacity=self.settings.cooldown_opacity,
                scale=self.settings.cooldown_scale,
            ),
            logger=self.logger.getChild("tray"),
        )
        self.watcher = GameWatcher(self._on_game_change, logger=self.logger.getChild("game_watch"))

    # -- lifecycle -------------------------------------------------------

    def run(self) -> int:
        self.logger.info("%s starting from %s", APP_DISPLAY_NAME, self.executable)
        windows.create_start_menu_shortcut(self.executable)
        windows.repair_startup_path(self.executable)
        self.watcher.start()
        self._start_worker(self._startup)
        try:
            self.tray.run()
        finally:
            self.shutdown()
        return 0

    def shutdown(self, timeout: float = 15.0) -> None:
        self._stop.set()
        self.watcher.stop(timeout)
        worker = self._worker
        if worker is not None and worker.is_alive():
            worker.join(timeout)
        self._close_cooldowns()
        try:
            self._source.close()
        except Exception:  # noqa: BLE001 - shutdown must not raise
            self.logger.debug("Closing the skin source failed", exc_info=True)

    # -- background work -------------------------------------------------

    def _start_worker(self, target: object) -> bool:
        with self._lock:
            if self._worker is not None and self._worker.is_alive():
                return False
            self._worker = Thread(target=target, name="app-worker", daemon=True)  # type: ignore[arg-type]
            self._worker.start()
        return True

    def _startup(self) -> None:
        """First launch does the whole setup; later launches just sync."""

        try:
            installed = ltk.locate()
            if installed is None and not self.settings.ltk_installed_by_app:
                self._install_ltk()
            elif installed is not None and not self.settings.ltk_installed_by_app:
                self._block(BLOCKED_MESSAGE)
                return

            # Both are lazy and skipped while LTK runs. apply_settings only
            # edits a file LTK has already written; clearing enabled mods
            # restores the baseline LTK undoes every time it adopts a package.
            if not ltk.is_running(windows.ProcessLookup):
                ltk.apply_settings(self.paths.ltk_data_dir)
                ltk.clear_enabled_mods(self.paths.ltk_data_dir)

            first_sync = self.settings.commit is None
            self._sync()
            if first_sync and self.settings.commit is not None:
                self._advertise()
        except Exception as error:  # noqa: BLE001 - the worker is the boundary
            self.logger.exception("Startup failed")
            self._fail("Startup failed", str(error))

    def _install_ltk(self) -> None:
        self.tray.refresh(working=True, detail="Installing LTK Manager...")
        client = ltk.ReleaseClient()
        try:
            ltk.install(client, self.paths.ltk_cache_dir)
        finally:
            client.close()
        # Recorded now because it cannot be derived later, and it decides both
        # whether uninstall removes LTK and whether we may touch its library.
        self._save(_replace(self.settings, ltk_installed_by_app=True))
        self.tray.refresh(ltk_installed=True)
        self.logger.info("LTK Manager installed and recorded as owned by this application")

    def _advertise(self) -> None:
        """Open LTK once after the first successful sync, so the skins are visible."""

        executable = ltk.locate()
        if executable is None:
            return
        self.logger.info("Opening LTK Manager once to show the newly seeded library")
        ltk.launch(executable)

    def _request_sync(self) -> None:
        if self._blocked is not None:
            self.tray.notify(APP_DISPLAY_NAME, self._blocked)
            return
        if not self._start_worker(self._sync_guarded):
            self.tray.notify(APP_DISPLAY_NAME, "A sync is already running.")

    def _sync_guarded(self) -> None:
        try:
            self._sync()
        except Exception as error:  # noqa: BLE001 - the worker is the boundary
            self.logger.exception("Sync failed")
            self._fail("Sync failed", str(error))

    def _sync(self) -> None:
        self.tray.refresh(working=True, detail="Checking for skin updates...")
        result, updated = synchronize(
            source=self._source,
            cache=self._cache,
            settings=self.settings,
            storage_dir=ltk.resolve_storage_dir(self.paths.ltk_data_dir),
            cancel=_EventCancel(self._stop),
            on_progress=self._on_download_progress,
        )
        if result.changed:
            self._save(updated)
            # LTK enables whatever it adopts, so the baseline is restored on
            # the next launch that finds LTK closed.
            if ltk.is_running(windows.ProcessLookup):
                self.tray.notify(
                    APP_DISPLAY_NAME,
                    "Skins updated — restart LTK Manager to load them.",
                )
        self.tray.refresh(
            working=False,
            detail=None,
            skins=self.settings.skins,
            patch=self.settings.patch,
            synced_at=self.settings.synced_at,
        )

    def _on_download_progress(self, done: int, total: int) -> None:
        if done == total or done % 100 == 0:
            self.tray.refresh(detail=f"Downloading skins… {done:,} / {total:,}")

    # -- tray actions ----------------------------------------------------

    def _open_ltk(self) -> None:
        executable = ltk.locate()
        if executable is not None:
            ltk.launch(executable)
            return
        if self._blocked is not None:
            self.tray.notify(APP_DISPLAY_NAME, self._blocked)
            return
        self._start_worker(self._install_then_sync)

    def _install_then_sync(self) -> None:
        try:
            self._install_ltk()
            self._sync()
            self._advertise()
        except Exception as error:  # noqa: BLE001
            self.logger.exception("LTK installation failed")
            self._fail("LTK Manager", str(error))

    def _open_cooldowns(self) -> None:
        if not self.watcher.match_active:
            self.tray.notify(APP_DISPLAY_NAME, "Cooldown timers open during a match.")
            return
        self._show_cooldowns(automatic=False)

    def _set_cooldown_auto_run(self, enabled: bool) -> None:
        self._save(_replace(self.settings, cooldown_auto_run=enabled))
        self.tray.refresh(cooldown_auto_run=enabled)

    def _set_startup(self, enabled: bool) -> None:
        windows.set_startup_enabled(self.executable, enabled)
        self.tray.refresh(startup_enabled=windows.startup_enabled(self.executable))

    def _set_opacity(self, value: float) -> None:
        updated = self.settings.with_display(opacity=value)
        self._save(updated)
        self.tray.refresh(opacity=updated.cooldown_opacity)
        self._apply_display()

    def _set_scale(self, value: float) -> None:
        updated = self.settings.with_display(scale=value)
        self._save(updated)
        self.tray.refresh(scale=updated.cooldown_scale)
        self._apply_display()

    def _uninstall(self) -> None:
        if self._worker is not None and self._worker.is_alive():
            self.tray.notify(APP_DISPLAY_NAME, "Wait for the current operation to finish.")
            return
        report = uninstall(
            data_dir=self.paths.data_dir,
            executable=self.executable,
            remove_ltk=self.settings.ltk_installed_by_app,
            ltk_data_dir=self.paths.ltk_data_dir,
        )
        self.tray.notify(
            APP_DISPLAY_NAME,
            f"{report.summary()} Delete {self.executable.name} to finish.",
        )
        self._exit()

    def _exit(self) -> None:
        self._stop.set()
        self.tray.stop()

    # -- cooldown panel --------------------------------------------------

    def _on_game_change(self, running: bool) -> None:
        self.tray.refresh(match_active=running)
        if running:
            self._suppressed_for_match = False
            if self.settings.cooldown_auto_run:
                self._show_cooldowns(automatic=True)
        else:
            self._suppressed_for_match = False
            self._close_cooldowns()

    def _show_cooldowns(self, *, automatic: bool) -> None:
        if automatic and self._suppressed_for_match:
            return
        try:
            from .cooldown import open_panel
        except ImportError:
            self.logger.warning("The cooldown panel is unavailable")
            return
        with _logged(self.logger, "opening the cooldown panel"):
            self._cooldown_open = bool(
                open_panel(
                    cache_dir=self.paths.ltk_cache_dir.parent / "cooldowns",
                    opacity=self.settings.cooldown_opacity,
                    scale=self.settings.cooldown_scale,
                    on_closed=self._on_cooldowns_closed,
                )
            )

    def _on_cooldowns_closed(self) -> None:
        """A manual close during a match suppresses re-opening for that match only."""

        self._cooldown_open = False
        if self.watcher.match_active:
            self._suppressed_for_match = True

    def _close_cooldowns(self) -> None:
        if not self._cooldown_open:
            return
        try:
            from .cooldown import close_panel
        except ImportError:
            return
        with _logged(self.logger, "closing the cooldown panel"):
            close_panel()
        self._cooldown_open = False

    def _apply_display(self) -> None:
        try:
            from .cooldown import apply_display
        except ImportError:
            return
        with _logged(self.logger, "applying cooldown display settings"):
            apply_display(self.settings.cooldown_opacity, self.settings.cooldown_scale)

    # -- helpers ---------------------------------------------------------

    def _save(self, updated: Settings) -> None:
        self.settings = updated
        with _logged(self.logger, "saving settings"):
            settings_module.save(self.paths.settings_file, updated)

    def _block(self, reason: str) -> None:
        self._blocked = reason
        self.logger.warning("Skin syncing disabled: %s", reason)
        self.tray.refresh(working=False, blocked_reason=reason, ltk_installed=True)
        self.tray.notify(APP_DISPLAY_NAME, reason)

    def _fail(self, title: str, message: str) -> None:
        self.tray.refresh(working=False, detail=f"{title} — see log")
        self.tray.notify(title, message)


class _EventCancel:
    """Adapts a threading.Event to the CancelSignal port."""

    def __init__(self, event: Event) -> None:
        self._event = event

    def is_set(self) -> bool:
        return self._event.is_set()


class _logged:  # noqa: N801 - used as a context manager
    def __init__(self, logger: logging.Logger, what: str) -> None:
        self._logger = logger
        self._what = what

    def __enter__(self) -> None:
        return None

    def __exit__(self, exc_type: type[BaseException] | None, *_rest: object) -> bool:
        if exc_type is not None:
            self._logger.exception("Failure while %s", self._what)
        return exc_type is not None


def _replace(value: Settings, **changes: object) -> Settings:
    from dataclasses import replace

    return replace(value, **changes)  # type: ignore[arg-type]


def run() -> int:
    """Start the application. Returns a process exit code."""

    if sys.platform != "win32":
        logging.getLogger(__name__).error("%s is Windows-only", APP_DISPLAY_NAME)
        return 1

    instance = windows.SingleInstance()
    if not instance.acquire():
        return 2
    try:
        paths = AppPaths.discover()
        paths.ensure()
        logger = configure_logging(paths.log_dir)
        current = settings_module.load(paths.settings_file)
        app = App(
            paths=paths,
            executable=windows.running_executable(),
            settings=current,
            logger=logger,
        )
        return app.run()
    except KeyboardInterrupt:
        return 0
    except Exception:
        logging.getLogger(__name__).exception("Application startup failed")
        return 1
    finally:
        instance.release()


__all__ = ["BLOCKED_MESSAGE", "App", "run"]
