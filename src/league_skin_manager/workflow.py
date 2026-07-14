"""High-level manager update and skin synchronization workflow."""

from __future__ import annotations

import logging
from collections.abc import Callable
from pathlib import Path
from threading import Event
from typing import Protocol

from .controller import AppState, SyncOutcome
from .manager_update import ManagerUpdateStatus, UntrustedReleaseError
from .skin_source import SkinManifest, SkinSourceError
from .sync_service import SkinSource, SyncMutationBlocked, SyncProgress, SyncResult


class ManifestSource(SkinSource, Protocol):
    def fetch_manifest(self) -> SkinManifest: ...


class SyncService(Protocol):
    def sync(
        self,
        source: SkinSource,
        manifest: SkinManifest,
        *,
        cancel_event: Event | None = None,
        progress: object | None = None,
    ) -> SyncResult: ...


class ManagerUpdateService(Protocol):
    def update(self, cancel_event: Event) -> ManagerUpdateStatus: ...


class SynchronizationWorkflow:
    def __init__(
        self,
        *,
        source: ManifestSource,
        sync_service: SyncService,
        manager_updater: ManagerUpdateService,
        manager_executable: Path,
        installed_dir: Path,
        logger: logging.Logger,
        manager_is_running: Callable[[], bool] | None = None,
    ) -> None:
        self.source = source
        self.sync_service = sync_service
        self.manager_updater = manager_updater
        self.manager_executable = manager_executable
        self.installed_dir = installed_dir
        self.logger = logger
        self.manager_is_running = manager_is_running or (lambda: False)

    def __call__(self, cancel_event: Event) -> SyncOutcome:
        if self._manager_is_running():
            return SyncOutcome(
                AppState.OFFLINE_READY,
                "Sync paused - close CSLOL Manager and try again",
            )
        manager_status: ManagerUpdateStatus | None = None
        manager_error: Exception | None = None
        try:
            manager_status = self.manager_updater.update(cancel_event)
        except Exception as exc:
            manager_error = exc
            self.logger.exception("CSLOL Manager update check failed; retaining current install")

        if cancel_event.is_set():
            return SyncOutcome(AppState.OFFLINE_READY, "Stopping sync")

        if self._manager_is_running():
            return SyncOutcome(
                AppState.OFFLINE_READY,
                "Sync paused - close CSLOL Manager and try again",
            )

        try:
            manifest = self.source.fetch_manifest()
            if self._manager_is_running():
                return SyncOutcome(
                    AppState.OFFLINE_READY,
                    "Sync paused - close CSLOL Manager and try again",
                )
            result = self.sync_service.sync(
                self.source,
                manifest,
                cancel_event=cancel_event,
                progress=self._progress,
            )
        except SyncMutationBlocked as exc:
            self.logger.info("Skin sync deferred because manager state is unsafe: %s", exc)
            return SyncOutcome(
                AppState.OFFLINE_READY,
                "Sync paused - close CSLOL Manager and try again",
            )
        except SkinSourceError as exc:
            if self._has_usable_install():
                self.logger.warning("Skin source unavailable; using installed cache: %s", exc)
                return SyncOutcome(
                    AppState.OFFLINE_READY,
                    "Offline - using installed skins",
                )
            raise

        patch = result.patch or "unknown patch"
        detail = f"Ready - {result.installed} skins ({patch})"
        if manager_error is not None:
            if not self.manager_executable.is_file():
                return SyncOutcome(
                    AppState.OFFLINE_READY,
                    f"Skins ready ({patch}); install CSLOL Manager manually - see log",
                )
            if isinstance(manager_error, UntrustedReleaseError):
                detail += "; manager update requires a newer app build"
            else:
                detail += "; manager update unavailable"
        elif manager_status is ManagerUpdateStatus.DEFERRED_RUNNING:
            detail += "; manager update deferred"
        return SyncOutcome(AppState.READY, detail)

    def _manager_is_running(self) -> bool:
        try:
            return self.manager_is_running()
        except Exception:
            self.logger.exception("Could not verify whether CSLOL Manager is running; pausing sync")
            return True

    def _has_usable_install(self) -> bool:
        if not self.manager_executable.is_file() or not self.installed_dir.is_dir():
            return False
        try:
            return any(self.installed_dir.iterdir())
        except OSError:
            return False

    def _progress(self, value: object) -> None:
        if not isinstance(value, SyncProgress):
            return
        if value.phase in {"preparing", "committing", "complete"}:
            self.logger.info(
                "Skin sync %s (%s/%s)",
                value.phase,
                value.completed,
                value.total,
            )
