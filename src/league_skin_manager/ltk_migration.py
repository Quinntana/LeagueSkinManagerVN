"""Reconcile LTK Manager's skin library to an application-owned baseline.

LTK is installed and controlled by this application, so its skin library is
treated as a reproducible mirror of the current VN skin set rather than as
user-owned data.  The baseline is:

    LTK holds exactly one package per current VN-managed skin,
    nothing else, and nothing enabled.

Reconciliation is declarative and idempotent.  The desired set is derived from
the managed manifest and the verified package cache; the actual set is read from
LTK's storage.  Whatever is present but not desired is removed - stale versions,
and anything the user added by hand - and whatever is desired but absent is
queued.  This holds because LTK stores an imported package byte-for-byte under
its own identifier, so a package's SHA-256 is a stable identity across import.

Only LTK's *skin library* is managed.  Its application files, settings, and logs
are never touched here.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import stat
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import BinaryIO, Protocol, cast
from uuid import uuid4

from .atomic import atomic_write_json
from .skin_installer import (
    DEFAULT_MAX_COMPRESSED_BYTES,
    DEFAULT_MAX_MEMBERS,
    DEFAULT_MAX_UNCOMPRESSED_BYTES,
    FantomeError,
    validate_fantome,
)
from .sync_service import ManagedEntry, ManagedState, ManagedStateError

LOGGER = logging.getLogger(__name__)

LTK_DATA_DIRECTORY_NAME = "dev.leaguetoolkit.manager"
LTK_SETTINGS_FILENAME = "settings.json"
LTK_ARCHIVES_DIRECTORY_NAME = "archives"
RECONCILE_REPORT_SCHEMA_VERSION = 2
DIGEST_INDEX_SCHEMA_VERSION = 1
MANAGED_PACKAGE_PREFIX = "lsmvn-"
DEFAULT_MAX_MODS = 20_000
DEFAULT_MAX_EXISTING_ARCHIVES = 50_000
DEFAULT_MAX_INDEX_BYTES = 32 * 1024 * 1024
_COPY_CHUNK_SIZE = 1024 * 1024
# Enumerating the process table costs hundreds of milliseconds, so a scan over
# thousands of packages must not re-check it continuously. Writes still force a
# check at every package boundary; this only bounds the periodic re-check.
_PROCESS_CHECK_INTERVAL_SECONDS = 5.0
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_PACKAGE_SUFFIXES = frozenset({".fantome", ".modpkg"})


class LtkReconcileError(RuntimeError):
    """Base error for a reconcile that cannot safely proceed."""


class ReconcileBlockedError(LtkReconcileError):
    """A manager is running, or its process state cannot be verified."""


class ReconcileBusyError(LtkReconcileError):
    """Another reconcile is already using this service instance."""


class _ReconcileCancelled(LtkReconcileError):
    """Internal control flow for a user-requested cancellation."""


class CancelSignal(Protocol):
    def is_set(self) -> bool:
        """Return whether cancellation was requested."""


ModRemover = Callable[[tuple[str, ...]], object]
ToggleClearer = Callable[[], int]


@dataclass(frozen=True, slots=True)
class ReconcileProgress:
    """One UI-safe progress snapshot from a reconcile."""

    phase: str
    completed: int
    total: int
    skin_name: str | None = None


ProgressCallback = Callable[[ReconcileProgress], None]


@dataclass(frozen=True, slots=True)
class ReconcileIssue:
    """A skin or global condition that could not be reconciled."""

    reason: str
    skin_name: str | None = None


@dataclass(frozen=True, slots=True)
class ReconcileResult:
    """Complete or safely interrupted outcome of one reconcile pass."""

    storage_dir: Path
    archives_dir: Path
    report_path: Path
    status: str
    expected: int
    added: int
    removed: int
    unchanged: int
    toggles_cleared: int
    issues: tuple[ReconcileIssue, ...]
    report_error: str | None = None

    @property
    def cancelled(self) -> bool:
        return self.status == "cancelled"

    @property
    def blocked(self) -> bool:
        return self.status == "blocked"

    @property
    def changed(self) -> bool:
        return bool(self.added or self.removed or self.toggles_cleared)


@dataclass(frozen=True, slots=True)
class BaselineStatus:
    """Read-only drift summary between the baseline and LTK's actual library."""

    expected: int
    present: int
    extra: int

    @property
    def missing(self) -> int:
        return max(0, self.expected - self.present)

    @property
    def at_baseline(self) -> bool:
        return self.missing == 0 and self.extra == 0


@dataclass(frozen=True, slots=True)
class _DigestRecord:
    """Cached digest for a file, validated by its exact filesystem identity."""

    size: int
    mtime_ns: int
    ctime_ns: int
    device: int
    inode: int
    sha256: str


@dataclass(frozen=True, slots=True)
class _DesiredPackage:
    entry: ManagedEntry
    path: Path


class _Guard:
    """Cancellation and process-state checkpoint shared across a pass.

    ``check_processes`` is False for read-only inspection: nothing is being
    written, so there is no window to protect, and the process table is far too
    expensive to poll while scanning thousands of packages.
    """

    def __init__(
        self,
        service: LtkMigrationService,
        cancellation: CancelSignal | None,
        *,
        check_processes: bool = True,
    ) -> None:
        self._service = service
        self._cancellation = cancellation
        self._check_processes = check_processes
        self._last_process_check = time.monotonic()

    def checkpoint(self, *, force_process_check: bool = False) -> None:
        if self._cancellation is not None and self._cancellation.is_set():
            raise _ReconcileCancelled("LTK reconcile was cancelled")
        if not self._check_processes:
            return
        now = time.monotonic()
        if force_process_check or now - self._last_process_check >= _PROCESS_CHECK_INTERVAL_SECONDS:
            self._service._ensure_processes_stopped()
            self._last_process_check = now


class _ProgressReporter:
    def __init__(self, callback: ProgressCallback | None) -> None:
        self._callback = callback
        self._total = 0

    def set_total(self, total: int) -> None:
        self._total = total

    def emit(self, phase: str, completed: int, *, skin_name: str | None = None) -> None:
        if self._callback is None:
            return
        try:
            self._callback(ReconcileProgress(phase, completed, self._total, skin_name))
        except Exception:
            LOGGER.exception("LTK reconcile progress callback failed")


class LtkMigrationService:
    """Bring LTK's skin library to the application-owned baseline."""

    def __init__(
        self,
        managed_state_path: Path,
        package_cache_dir: Path,
        *,
        ltk_app_data_dir: Path | None = None,
        report_dir: Path | None = None,
        archive_index_path: Path | None = None,
        package_index_path: Path | None = None,
        cslol_is_running: Callable[[], bool] | None = None,
        ltk_is_running: Callable[[], bool] | None = None,
        remove_ltk_mods: ModRemover | None = None,
        clear_ltk_toggles: ToggleClearer | None = None,
        max_mods: int = DEFAULT_MAX_MODS,
        max_members_per_mod: int = DEFAULT_MAX_MEMBERS,
        max_compressed_bytes: int = DEFAULT_MAX_COMPRESSED_BYTES,
        max_uncompressed_bytes: int = DEFAULT_MAX_UNCOMPRESSED_BYTES,
        max_existing_archives: int = DEFAULT_MAX_EXISTING_ARCHIVES,
        max_index_bytes: int = DEFAULT_MAX_INDEX_BYTES,
    ) -> None:
        if (
            min(
                max_mods,
                max_members_per_mod,
                max_compressed_bytes,
                max_uncompressed_bytes,
                max_existing_archives,
                max_index_bytes,
            )
            < 1
        ):
            raise ValueError("LTK reconcile resource limits must be positive")
        self.managed_state_path = _absolute_path(managed_state_path)
        self.package_cache_dir = _absolute_path(package_cache_dir)
        if ltk_app_data_dir is None:
            appdata = os.environ.get("APPDATA")
            roaming = Path(appdata) if appdata else Path.home() / "AppData" / "Roaming"
            ltk_app_data_dir = roaming / LTK_DATA_DIRECTORY_NAME
        self.ltk_app_data_dir = _absolute_path(ltk_app_data_dir)
        self.report_dir = _absolute_path(
            report_dir
            if report_dir is not None
            else self.managed_state_path.parent / "migration-reports"
        )
        self.archive_index_path = _absolute_path(
            archive_index_path
            if archive_index_path is not None
            else self.managed_state_path.parent / "ltk_archive_index.json"
        )
        self.package_index_path = _absolute_path(
            package_index_path
            if package_index_path is not None
            else self.managed_state_path.parent / "ltk_package_index.json"
        )
        for label, path in (
            ("archive index", self.archive_index_path),
            ("package index", self.package_index_path),
        ):
            if path == self.managed_state_path:
                raise ValueError(f"LTK {label} must use its own application-owned file")
        if self.archive_index_path == self.package_index_path:
            raise ValueError("LTK archive and package indexes must be separate files")
        self.cslol_is_running = cslol_is_running or (lambda: False)
        self.ltk_is_running = ltk_is_running or (lambda: False)
        self._remove_ltk_mods = remove_ltk_mods
        self._clear_ltk_toggles = clear_ltk_toggles
        self.max_mods = max_mods
        self.max_members_per_mod = max_members_per_mod
        self.max_compressed_bytes = max_compressed_bytes
        self.max_uncompressed_bytes = max_uncompressed_bytes
        self.max_existing_archives = max_existing_archives
        self.max_index_bytes = max_index_bytes
        self._lock = threading.Lock()

    # ---------------------------------------------------------------- storage

    def resolve_storage_dir(self) -> Path:
        """Resolve LTK's configured storage root without creating it."""

        default = self.ltk_app_data_dir
        settings_path = self.ltk_app_data_dir / LTK_SETTINGS_FILENAME
        if not settings_path.exists():
            return default
        if not _is_safe_regular_file(settings_path):
            LOGGER.warning("Ignoring unsafe LTK settings file: %s", settings_path)
            return default
        try:
            raw = json.loads(settings_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            LOGGER.warning("Ignoring unreadable LTK settings file: %s", settings_path)
            return default
        if not isinstance(raw, Mapping):
            return default
        configured = raw.get("modStoragePath")
        if not isinstance(configured, str) or not configured.strip():
            return default
        path = Path(configured).expanduser()
        if not path.is_absolute():
            LOGGER.warning("Ignoring relative LTK modStoragePath: %s", configured)
            return default
        return _absolute_path(path)

    # ----------------------------------------------------------- inspection

    def inspect_baseline(self) -> BaselineStatus:
        """Report drift from the baseline without changing anything.

        Shares the reconcile lock so it cannot observe a pass in progress, and
        never creates or modifies files.
        """

        if not self._lock.acquire(blocking=False):
            raise ReconcileBusyError("An LTK reconcile is already in progress")
        try:
            desired = self._desired_packages([])
            archives_dir = self.resolve_storage_dir() / LTK_ARCHIVES_DIRECTORY_NAME
            if not _is_safe_directory(archives_dir):
                return BaselineStatus(expected=len(desired), present=0, extra=0)
            actual = self._index_packages(
                archives_dir,
                self.archive_index_path,
                _Guard(self, None, check_processes=False),
                max_entries=self.max_existing_archives,
            )
            present = sum(1 for digest in desired if digest in actual)
            extra = sum(1 for digest in actual if digest not in desired)
            return BaselineStatus(expected=len(desired), present=present, extra=extra)
        finally:
            self._lock.release()

    # ------------------------------------------------------------ reconcile

    def reconcile(
        self,
        *,
        cancel_event: CancelSignal | None = None,
        progress: ProgressCallback | None = None,
    ) -> ReconcileResult:
        """Bring LTK's library to the baseline in one pass.

        Cancellation or a manager starting mid-pass yields a partial result;
        a manager already running blocks before any mutation.
        """

        if progress is not None and not callable(progress):
            raise TypeError("progress must be callable")
        if not self._lock.acquire(blocking=False):
            raise ReconcileBusyError("An LTK reconcile is already in progress")
        try:
            return self._reconcile_locked(cancel_event, progress)
        finally:
            self._lock.release()

    def _reconcile_locked(
        self,
        cancel_event: CancelSignal | None,
        progress: ProgressCallback | None,
    ) -> ReconcileResult:
        storage_dir = self.resolve_storage_dir()
        archives_dir = storage_dir / LTK_ARCHIVES_DIRECTORY_NAME
        for label, path in (
            ("ledger", self.archive_index_path),
            ("package index", self.package_index_path),
        ):
            if _is_within(path, storage_dir) or _is_within(path, self.ltk_app_data_dir):
                raise LtkReconcileError(f"VN {label} cannot be stored in LTK-owned data")
        self._ensure_processes_stopped()

        guard = _Guard(self, cancel_event)
        reporter = _ProgressReporter(progress)
        report_path = self._new_report_path()
        issues: list[ReconcileIssue] = []
        added = removed = unchanged = toggles_cleared = 0
        status = "completed"
        desired: dict[str, _DesiredPackage] = {}

        try:
            reporter.emit("inspecting", 0)
            guard.checkpoint(force_process_check=True)
            desired = self._desired_packages(issues)
            reporter.set_total(len(desired))
            _ensure_safe_directory_tree(archives_dir)
            actual = self._index_packages(
                archives_dir,
                self.archive_index_path,
                guard,
                max_entries=self.max_existing_archives,
            )
            unchanged = sum(1 for digest in desired if digest in actual)

            removed = self._remove_extras(actual, desired, archives_dir, guard, issues, reporter)
            added = self._queue_missing(actual, desired, archives_dir, guard, issues, reporter)
            toggles_cleared = self._clear_toggles(guard, issues)
        except _ReconcileCancelled as error:
            status = "cancelled"
            issues.append(ReconcileIssue(reason=str(error)))
        except ReconcileBlockedError as error:
            status = "blocked"
            issues.append(ReconcileIssue(reason=str(error)))

        result = ReconcileResult(
            storage_dir=storage_dir,
            archives_dir=archives_dir,
            report_path=report_path,
            status=status,
            expected=len(desired),
            added=added,
            removed=removed,
            unchanged=unchanged,
            toggles_cleared=toggles_cleared,
            issues=tuple(issues),
        )
        try:
            self._write_report(result)
        except (OSError, LtkReconcileError) as error:
            # The library itself is the authoritative outcome. An unavailable
            # diagnostics directory must not turn a completed reconcile into a
            # misleading failure.
            message = str(error) or error.__class__.__name__
            LOGGER.error("LTK reconcile completed but its report was unavailable: %s", error)
            result = replace(result, report_error=message)
        reporter.emit(status, len(desired))
        return result

    def _remove_extras(
        self,
        actual: dict[str, Path],
        desired: Mapping[str, _DesiredPackage],
        archives_dir: Path,
        guard: _Guard,
        issues: list[ReconcileIssue],
        reporter: _ProgressReporter,
    ) -> int:
        """Delete everything present in LTK that the baseline does not want."""

        extras = {digest: path for digest, path in actual.items() if digest not in desired}
        if not extras:
            return 0
        reporter.emit("removing", 0)
        removed = 0
        imported_ids: list[str] = []
        for digest, path in sorted(extras.items(), key=lambda item: item[1].name):
            guard.checkpoint()
            if path.name.startswith(MANAGED_PACKAGE_PREFIX):
                # Queued by us but no longer wanted, and LTK has not taken it
                # yet, so it is only a file we own.
                try:
                    path.unlink()
                except OSError as error:
                    issues.append(ReconcileIssue(reason=f"Could not remove {path.name}: {error}"))
                    continue
                actual.pop(digest, None)
                removed += 1
            else:
                imported_ids.append(path.stem)

        if imported_ids:
            if self._remove_ltk_mods is None:
                issues.append(
                    ReconcileIssue(
                        reason=(
                            f"{len(imported_ids)} package(s) are not part of the baseline but no "
                            "removal boundary is configured"
                        )
                    )
                )
            else:
                guard.checkpoint(force_process_check=True)
                try:
                    self._remove_ltk_mods(tuple(imported_ids))
                except Exception as error:
                    LOGGER.warning("Could not remove non-baseline LTK mods: %s", error)
                    issues.append(
                        ReconcileIssue(reason=f"Could not remove non-baseline skins: {error}")
                    )
                else:
                    for digest, path in tuple(extras.items()):
                        if path.stem in set(imported_ids):
                            actual.pop(digest, None)
                    removed += len(imported_ids)
        if removed:
            _fsync_directory(archives_dir)
        return removed

    def _queue_missing(
        self,
        actual: Mapping[str, Path],
        desired: Mapping[str, _DesiredPackage],
        archives_dir: Path,
        guard: _Guard,
        issues: list[ReconcileIssue],
        reporter: _ProgressReporter,
    ) -> int:
        """Copy every desired package LTK does not already hold into its inbox."""

        missing = {digest: pkg for digest, pkg in desired.items() if digest not in actual}
        added = 0
        ordered = sorted(missing.items(), key=lambda item: item[1].entry.directory)
        if ordered:
            # Confirm the managers are down once before writing anything; the
            # per-package checkpoints below then re-check on the usual interval
            # rather than paying a process-table walk for every skin.
            guard.checkpoint(force_process_check=True)
        for completed, (digest, package) in enumerate(ordered):
            guard.checkpoint()
            reporter.emit("queueing", completed, skin_name=package.entry.name)
            try:
                staged = self._copy_to_partial(package.path, archives_dir, guard)
                try:
                    validate_fantome(
                        staged,
                        expected_size=package.entry.size,
                        expected_sha=package.entry.source_sha,
                        max_members=self.max_members_per_mod,
                        max_compressed_bytes=self.max_compressed_bytes,
                        max_uncompressed_bytes=self.max_uncompressed_bytes,
                    )
                    if self._commit_package(staged, digest):
                        added += 1
                finally:
                    staged.unlink(missing_ok=True)
            except (_ReconcileCancelled, ReconcileBlockedError):
                raise
            except (FantomeError, LtkReconcileError, OSError, ValueError) as error:
                issues.append(
                    ReconcileIssue(
                        reason=str(error) or error.__class__.__name__,
                        skin_name=package.entry.name,
                    )
                )
                LOGGER.warning("Could not queue %s for LTK: %s", package.entry.name, error)
        if added:
            _fsync_directory(archives_dir)
        return added

    def _clear_toggles(self, guard: _Guard, issues: list[ReconcileIssue]) -> int:
        """Reset LTK's enabled selections; the baseline has nothing enabled."""

        if self._clear_ltk_toggles is None:
            return 0
        guard.checkpoint(force_process_check=True)
        try:
            cleared = self._clear_ltk_toggles()
        except Exception as error:
            LOGGER.warning("Could not reset LTK enabled selections: %s", error)
            issues.append(ReconcileIssue(reason=f"Could not reset enabled skins: {error}"))
            return 0
        return cleared if isinstance(cleared, int) else 0

    # ------------------------------------------------------- desired packages

    def _desired_packages(self, issues: list[ReconcileIssue]) -> dict[str, _DesiredPackage]:
        """Map each wanted package digest to the managed entry that supplies it.

        The verified package cache is the only source. Its contents are checked
        against the manifest's upstream byte count and Git blob SHA-1, so no
        extracted CSLOL copy is needed or consulted.
        """

        try:
            state = self._load_managed_state()
        except ManagedStateError as error:
            issues.append(ReconcileIssue(reason=f"Managed skin state is unavailable: {error}"))
            return {}
        entries = state.entries[: self.max_mods]
        if len(state.entries) > self.max_mods:
            issues.append(
                ReconcileIssue(
                    reason=f"Managed skin set exceeds the {self.max_mods} skin safety limit"
                )
            )
        digests = self._package_digests(entries)
        desired: dict[str, _DesiredPackage] = {}
        for entry in entries:
            path = self._cache_for_entry(entry)
            if path is None:
                continue
            digest = digests.get(path.name)
            if digest is None:
                continue
            desired.setdefault(digest, _DesiredPackage(entry=entry, path=path))
        return desired

    def _package_digests(self, entries: tuple[ManagedEntry, ...]) -> dict[str, str]:
        """Map each needed cache package's filename to its digest."""

        wanted = {f"{entry.source_sha}.fantome" for entry in entries if entry.source_sha}
        if not wanted or not _is_safe_directory(self.package_cache_dir):
            return {}
        by_digest = self._index_packages(
            self.package_cache_dir,
            self.package_index_path,
            _Guard(self, None, check_processes=False),
            max_entries=max(self.max_mods, len(wanted)),
            names=wanted,
        )
        return {path.name: digest for digest, path in by_digest.items()}

    def _cache_for_entry(self, entry: ManagedEntry) -> Path | None:
        """Return the cached package for one managed entry, cheaply.

        This is deliberately a stat-level check - the file exists, is a real
        regular file, and matches the manifest's recorded byte count. Full
        structural and CRC validation happens in :meth:`_queue_missing` on the
        staged copy immediately before it is published into LTK, which is the
        only point where integrity actually matters. Keeping this cheap is what
        lets the read-only baseline inspection run without reading every
        package, so it can be called from the startup path.
        """

        if not entry.source_sha:
            return None
        cached = self.package_cache_dir / f"{entry.source_sha}.fantome"
        try:
            info = _safe_file_stat(cached)
        except LtkReconcileError:
            return None
        if info.st_size != entry.size:
            LOGGER.warning("Ignoring cached package with unexpected size for %s", entry.directory)
            return None
        return cached

    # ------------------------------------------------------------ digest index

    def _index_packages(
        self,
        directory: Path,
        index_path: Path,
        guard: _Guard,
        *,
        max_entries: int,
        names: set[str] | None = None,
    ) -> dict[str, Path]:
        """Map package digest to file for *directory*, caching by file identity.

        Digests are only recomputed when a file's size, timestamps, or identity
        differ from the cached record, so a large unchanged library costs stats
        rather than reads.
        """

        cached = self._load_digest_index(directory, index_path)
        try:
            entries = sorted(
                directory.iterdir(), key=lambda item: (item.name.casefold(), item.name)
            )
        except OSError as error:
            raise LtkReconcileError(f"Could not inspect package directory: {directory}") from error
        candidates = [
            entry
            for entry in entries
            if entry.suffix.casefold() in _PACKAGE_SUFFIXES
            and (names is None or entry.name in names)
        ]
        if len(candidates) > max_entries:
            raise LtkReconcileError(
                f"Package directory contains too many packages to index safely: {directory}"
            )
        digests: dict[str, Path] = {}
        updated: dict[str, _DigestRecord] = {}
        for package in candidates:
            guard.checkpoint()
            if not _is_safe_regular_file(package):
                LOGGER.warning("Ignoring unsafe package: %s", package)
                continue
            try:
                info = _safe_file_stat(package)
                if info.st_size > self.max_compressed_bytes:
                    LOGGER.warning("Ignoring oversized package: %s", package)
                    continue
                record = cached.get(package.name)
                if record is not None and _digest_record_matches(record, info):
                    digest = record.sha256
                else:
                    digest = _stable_sha256(package, guard)
                    info = _safe_file_stat(package)
                digests.setdefault(digest, package)
                updated[package.name] = _digest_record(info, digest)
            except (LtkReconcileError, OSError):
                LOGGER.warning("Could not digest package: %s", package)
        self._write_digest_index(directory, index_path, updated)
        return digests

    def _load_digest_index(self, directory: Path, index_path: Path) -> dict[str, _DigestRecord]:
        if not os.path.lexists(index_path):
            return {}
        if not _is_safe_regular_file(index_path):
            LOGGER.warning("Ignoring unsafe digest index: %s", index_path)
            return {}
        try:
            before = _safe_file_stat(index_path)
            if before.st_size > self.max_index_bytes:
                raise ValueError("digest index exceeds its size limit")
            with _open_regular_file(index_path, before) as stream:
                encoded = stream.read(self.max_index_bytes + 1)
            _require_same_file(index_path, before)
            raw = json.loads(encoded.decode("utf-8"))
            if not isinstance(raw, Mapping):
                raise ValueError("digest index must be a JSON object")
            if raw.get("schema_version") != DIGEST_INDEX_SCHEMA_VERSION:
                raise ValueError("digest index schema is unsupported")
            expected = os.path.normcase(str(_absolute_path(directory)))
            stored = raw.get("directory")
            if not isinstance(stored, str) or os.path.normcase(stored) != expected:
                return {}
            records = raw.get("packages")
            if not isinstance(records, Mapping):
                raise ValueError("digest index records are invalid")
            return {
                _validate_index_name(name): _digest_record_from_json(value)
                for name, value in records.items()
            }
        except (
            LtkReconcileError,
            OSError,
            UnicodeDecodeError,
            json.JSONDecodeError,
            ValueError,
        ) as error:
            LOGGER.warning("Ignoring unreadable digest index: %s", error)
            return {}

    def _write_digest_index(
        self,
        directory: Path,
        index_path: Path,
        records: Mapping[str, _DigestRecord],
    ) -> None:
        if os.path.lexists(index_path) and not _is_safe_regular_file(index_path):
            LOGGER.warning("Cannot update unsafe digest index: %s", index_path)
            return
        payload = {
            "schema_version": DIGEST_INDEX_SCHEMA_VERSION,
            "directory": str(_absolute_path(directory)),
            "packages": {
                name: {
                    "size": record.size,
                    "mtime_ns": record.mtime_ns,
                    "ctime_ns": record.ctime_ns,
                    "device": record.device,
                    "inode": record.inode,
                    "sha256": record.sha256,
                }
                for name, record in sorted(records.items())
            },
        }
        try:
            _ensure_safe_directory_tree(index_path.parent)
            encoded = (
                json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
            ).encode("utf-8")
            if len(encoded) > self.max_index_bytes:
                raise ValueError("digest index would exceed its size limit")
            atomic_write_json(index_path, payload)
        except (LtkReconcileError, OSError, ValueError) as error:
            LOGGER.warning("Could not update digest index: %s", error)

    # ------------------------------------------------------------------ files

    def _copy_to_partial(self, source: Path, archives_dir: Path, guard: _Guard) -> Path:
        staged = archives_dir / f".{MANAGED_PACKAGE_PREFIX}{uuid4().hex}.partial"
        try:
            source_stat = _safe_file_stat(source)
            with (
                _open_regular_file(source, source_stat) as input_stream,
                staged.open("xb") as output,
            ):
                while chunk := input_stream.read(_COPY_CHUNK_SIZE):
                    guard.checkpoint()
                    output.write(chunk)
                output.flush()
                os.fsync(output.fileno())
            _require_same_file(source, source_stat)
            guard.checkpoint()
            return staged
        except BaseException:
            staged.unlink(missing_ok=True)
            raise

    def _commit_package(self, staged: Path, digest: str) -> bool:
        """Atomically publish a staged package under its content digest."""

        if not _SHA256_PATTERN.fullmatch(digest):
            raise LtkReconcileError("Package digest is invalid")
        destination = staged.parent / f"{MANAGED_PACKAGE_PREFIX}{digest}.fantome"
        if destination.exists() or destination.is_symlink():
            if _is_safe_regular_file(destination) and _plain_sha256(destination) == digest:
                return False
            raise LtkReconcileError(f"LTK inbox collision: {destination.name}")
        try:
            if os.name == "nt":
                # Windows rename is atomic and refuses to replace an existing
                # destination, so the watcher never observes partial bytes.
                os.rename(staged, destination)
            else:
                os.link(staged, destination)
                staged.unlink()
        except FileExistsError as error:
            if _is_safe_regular_file(destination) and _plain_sha256(destination) == digest:
                return False
            raise LtkReconcileError(f"LTK inbox collision: {destination.name}") from error
        except OSError as error:
            raise LtkReconcileError(f"Could not queue package: {destination.name}") from error
        return True

    # ------------------------------------------------------------------ state

    def _load_managed_state(self) -> ManagedState:
        path = self.managed_state_path
        if not os.path.lexists(path):
            return ManagedState.empty()
        if not _is_safe_regular_file(path):
            raise ManagedStateError(f"Unsafe managed-skin state: {path}")
        try:
            before = _safe_file_stat(path)
            with _open_regular_file(path, before) as stream:
                encoded = stream.read()
            _require_same_file(path, before)
            raw = json.loads(encoded.decode("utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, LtkReconcileError) as error:
            raise ManagedStateError("Managed-skin state is unreadable") from error
        return ManagedState.from_json(raw)

    def _ensure_processes_stopped(self) -> None:
        for label, predicate in (
            ("CSLOL Manager", self.cslol_is_running),
            ("LTK Manager", self.ltk_is_running),
        ):
            try:
                running = predicate()
            except Exception as error:
                raise ReconcileBlockedError(
                    f"Could not verify whether {label} is running"
                ) from error
            if not isinstance(running, bool):
                raise ReconcileBlockedError(f"Could not verify whether {label} is running")
            if running:
                raise ReconcileBlockedError(f"Close {label} before rebuilding the LTK library")

    def _new_report_path(self) -> Path:
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        return self.report_dir / f"ltk-reconcile-{timestamp}-{uuid4().hex[:8]}.json"

    def _write_report(self, result: ReconcileResult) -> None:
        _ensure_safe_directory_tree(result.report_path.parent)
        atomic_write_json(
            result.report_path,
            {
                "schema_version": RECONCILE_REPORT_SCHEMA_VERSION,
                "created_at": datetime.now(timezone.utc).isoformat(),
                "status": result.status,
                "storage_dir": str(result.storage_dir),
                "archives_dir": str(result.archives_dir),
                "expected": result.expected,
                "added": result.added,
                "removed": result.removed,
                "unchanged": result.unchanged,
                "toggles_cleared": result.toggles_cleared,
                "issues": [
                    {"skin_name": issue.skin_name, "reason": issue.reason}
                    for issue in result.issues
                ],
            },
        )


def _validate_index_name(value: object) -> str:
    if (
        not isinstance(value, str)
        or not value
        or Path(value).name != value
        or "/" in value
        or "\\" in value
        or Path(value).suffix.casefold() not in _PACKAGE_SUFFIXES
    ):
        raise ValueError("digest index contains an invalid filename")
    return value


def _digest_record_from_json(value: object) -> _DigestRecord:
    if not isinstance(value, Mapping):
        raise ValueError("digest index record must be a JSON object")
    fields = tuple(value.get(name) for name in ("size", "mtime_ns", "ctime_ns", "device", "inode"))
    if any(isinstance(field, bool) or not isinstance(field, int) or field < 0 for field in fields):
        raise ValueError("digest index record has invalid file identity")
    sha256 = value.get("sha256")
    if not isinstance(sha256, str) or not _SHA256_PATTERN.fullmatch(sha256):
        raise ValueError("digest index record has an invalid digest")
    size, mtime_ns, ctime_ns, device, inode = (cast(int, field) for field in fields)
    return _DigestRecord(size, mtime_ns, ctime_ns, device, inode, sha256)


def _digest_record(value: os.stat_result, sha256: str) -> _DigestRecord:
    return _DigestRecord(
        size=value.st_size,
        mtime_ns=value.st_mtime_ns,
        ctime_ns=value.st_ctime_ns,
        device=value.st_dev,
        inode=value.st_ino,
        sha256=sha256,
    )


def _digest_record_matches(record: _DigestRecord, value: os.stat_result) -> bool:
    return record == _digest_record(value, record.sha256)


def _stable_sha256(path: Path, guard: _Guard) -> str:
    before = _safe_file_stat(path)
    digest = hashlib.sha256()
    with _open_regular_file(path, before) as stream:
        while chunk := stream.read(_COPY_CHUNK_SIZE):
            guard.checkpoint()
            digest.update(chunk)
    _require_same_file(path, before)
    return digest.hexdigest()


def _plain_sha256(path: Path) -> str:
    before = _safe_file_stat(path)
    digest = hashlib.sha256()
    with _open_regular_file(path, before) as stream:
        while chunk := stream.read(_COPY_CHUNK_SIZE):
            digest.update(chunk)
    _require_same_file(path, before)
    return digest.hexdigest()


def _safe_file_stat(path: Path) -> os.stat_result:
    try:
        value = os.stat(path, follow_symlinks=False)
    except OSError as error:
        raise LtkReconcileError(f"Could not inspect regular file: {path}") from error
    if not stat.S_ISREG(value.st_mode) or _is_reparse_stat(value) or path.is_symlink():
        raise LtkReconcileError(f"Unsafe regular file: {path}")
    return value


def _open_regular_file(path: Path, expected: os.stat_result) -> BinaryIO:
    flags = os.O_RDONLY | int(getattr(os, "O_BINARY", 0))
    flags |= int(getattr(os, "O_NOFOLLOW", 0))
    descriptor = os.open(path, flags)
    try:
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode) or not _same_file_stat(expected, opened):
            raise LtkReconcileError(f"File changed while being opened: {path}")
        return os.fdopen(descriptor, "rb")
    except BaseException:
        os.close(descriptor)
        raise


def _require_same_file(path: Path, expected: os.stat_result) -> None:
    try:
        current = os.stat(path, follow_symlinks=False)
    except OSError as error:
        raise LtkReconcileError(f"File changed while being read: {path}") from error
    if _is_reparse_stat(current) or not _same_file_stat(expected, current):
        raise LtkReconcileError(f"File changed while being read: {path}")


def _same_file_stat(before: os.stat_result, after: os.stat_result) -> bool:
    if (
        before.st_mode != after.st_mode
        or before.st_size != after.st_size
        or before.st_mtime_ns != after.st_mtime_ns
    ):
        return False
    if before.st_dev and after.st_dev and before.st_dev != after.st_dev:
        return False
    return not (before.st_ino and after.st_ino and before.st_ino != after.st_ino)


def _is_safe_directory(path: Path) -> bool:
    try:
        value = os.stat(path, follow_symlinks=False)
    except OSError:
        return False
    return stat.S_ISDIR(value.st_mode) and not _is_reparse_stat(value) and not path.is_symlink()


def _is_safe_regular_file(path: Path) -> bool:
    try:
        value = os.stat(path, follow_symlinks=False)
    except OSError:
        return False
    return stat.S_ISREG(value.st_mode) and not _is_reparse_stat(value) and not path.is_symlink()


def _is_reparse_stat(value: os.stat_result) -> bool:
    attributes = int(getattr(value, "st_file_attributes", 0))
    reparse_flag = int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))
    return bool(attributes & reparse_flag)


def _absolute_path(path: str | os.PathLike[str]) -> Path:
    return Path(os.path.abspath(os.path.expanduser(os.fspath(path))))


def _ensure_safe_directory_tree(path: Path) -> None:
    current = Path(path.anchor) if path.anchor else Path()
    if path.anchor and not _is_safe_directory(current):
        raise LtkReconcileError(f"Directory path has an unsafe root: {current}")
    parts = path.parts[1:] if path.anchor else path.parts
    for part in parts:
        current /= part
        if not os.path.lexists(current):
            try:
                current.mkdir()
            except FileExistsError:
                pass
            except OSError as error:
                raise LtkReconcileError(f"Could not create directory: {current}") from error
        if not _is_safe_directory(current):
            raise LtkReconcileError(f"Directory path contains a reparse or unsafe entry: {current}")


def _is_within(path: Path, directory: Path) -> bool:
    try:
        path.relative_to(directory)
    except ValueError:
        return False
    return True


def _fsync_directory(path: Path) -> None:
    try:
        descriptor = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    except OSError:
        pass
    finally:
        os.close(descriptor)


__all__ = [
    "LTK_ARCHIVES_DIRECTORY_NAME",
    "MANAGED_PACKAGE_PREFIX",
    "BaselineStatus",
    "CancelSignal",
    "LtkMigrationService",
    "LtkReconcileError",
    "ModRemover",
    "ProgressCallback",
    "ReconcileBlockedError",
    "ReconcileBusyError",
    "ReconcileIssue",
    "ReconcileProgress",
    "ReconcileResult",
    "ToggleClearer",
]
