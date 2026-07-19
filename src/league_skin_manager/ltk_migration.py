"""Safe, one-way handoff of extracted CSLOL mods to LTK Manager.

LTK owns its library, profiles, and index.  This module deliberately writes
only complete ``.fantome`` files to LTK's ``archives`` inbox.  CSLOL's source
tree is treated as read-only and is checked for Windows path ambiguity,
reparse points, special files, resource exhaustion, and changes while it is
being packaged.
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
import zipfile
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import BinaryIO, Protocol, cast
from uuid import uuid4

from .atomic import atomic_write_bytes, atomic_write_json
from .skin_installer import (
    DEFAULT_MAX_COMPRESSED_BYTES,
    DEFAULT_MAX_MEMBERS,
    DEFAULT_MAX_METADATA_BYTES,
    DEFAULT_MAX_UNCOMPRESSED_BYTES,
    ExtractedModFingerprint,
    FantomeError,
    inspect_extracted_mod,
    validate_fantome,
)
from .sync_service import ManagedEntry, ManagedState, ManagedStateError

LOGGER = logging.getLogger(__name__)

LTK_DATA_DIRECTORY_NAME = "dev.leaguetoolkit.manager"
LTK_SETTINGS_FILENAME = "settings.json"
LTK_ARCHIVES_DIRECTORY_NAME = "archives"
MIGRATION_REPORT_SCHEMA_VERSION = 1
MIGRATION_STATE_SCHEMA_VERSION = 1
ARCHIVE_INDEX_SCHEMA_VERSION = 1
DEFAULT_MAX_MODS = 20_000
DEFAULT_MAX_EXISTING_ARCHIVES = 50_000
DEFAULT_MAX_TOTAL_UNCOMPRESSED_BYTES = 16 * 1024 * 1024 * 1024
DEFAULT_MAX_HISTORY_RECORDS = 100_000
DEFAULT_MAX_HISTORY_BYTES = 32 * 1024 * 1024
DEFAULT_HISTORY_CHECKPOINT_RECORDS = 64
_COPY_CHUNK_SIZE = 1024 * 1024
_PROCESS_CHECK_INTERVAL_SECONDS = 0.5
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_WINDOWS_INVALID_CHARACTERS = frozenset('<>:"/\\|?*')
_MAX_HISTORY_SOURCE_LENGTH = 32_768
_MAX_HISTORY_NAME_LENGTH = 4_096
_MAX_HISTORY_TIMESTAMP_LENGTH = 64
_WINDOWS_RESERVED_NAMES = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{number}" for number in range(1, 10)),
    *(f"LPT{number}" for number in range(1, 10)),
}


class LtkMigrationError(RuntimeError):
    """Base error for a migration that cannot safely proceed."""


class MigrationSourceError(LtkMigrationError):
    """The selected folder is not a safe CSLOL ``installed`` directory."""


class MigrationBlockedError(LtkMigrationError):
    """A manager is running, or its process state cannot be verified."""


class MigrationBusyError(LtkMigrationError):
    """Another migration is already using this service instance."""


class MigrationHistoryError(LtkMigrationError):
    """The VN-owned migration ledger cannot be trusted or persisted."""


class _MigrationCancelled(LtkMigrationError):
    """Internal control flow for a user-requested cancellation."""


class CancelSignal(Protocol):
    def is_set(self) -> bool:
        """Return whether cancellation was requested."""


@dataclass(frozen=True, slots=True)
class MigrationProgress:
    """One UI-safe progress snapshot from a migration."""

    phase: str
    completed: int
    total: int
    mod_name: str | None = None
    source: Path | None = None


ProgressCallback = Callable[[MigrationProgress], None]


@dataclass(frozen=True, slots=True)
class MigrationIssue:
    """A mod or global condition that could not be migrated."""

    source: Path
    reason: str
    mod_name: str | None = None


@dataclass(frozen=True, slots=True)
class MigrationResult:
    """Complete or safely interrupted outcome of a migration pass."""

    source_dir: Path
    storage_dir: Path
    archives_dir: Path
    report_path: Path
    status: str
    discovered: int
    queued: int
    skipped: int
    failed: int
    reused_cache: int
    packaged: int
    issues: tuple[MigrationIssue, ...]
    report_error: str | None = None

    @property
    def cancelled(self) -> bool:
        return self.status == "cancelled"

    @property
    def blocked(self) -> bool:
        return self.status == "blocked"


@dataclass(frozen=True, slots=True)
class ManagedPortStatus:
    """Read-only summary of VN-managed mods not yet handed to LTK."""

    total: int
    pending: int


@dataclass(frozen=True, slots=True)
class _TreeFile:
    path: Path
    relative: PurePosixPath
    stat_result: os.stat_result


@dataclass(frozen=True, slots=True)
class _ModTree:
    directories: tuple[PurePosixPath, ...]
    files: tuple[_TreeFile, ...]
    total_bytes: int


@dataclass(frozen=True, slots=True)
class _PreparedMod:
    tree: _ModTree
    fingerprint: ExtractedModFingerprint
    display_name: str


@dataclass(frozen=True, slots=True)
class _HistoryRecord:
    source: str
    name: str
    queued_at: str
    content_sha256: str | None = None


@dataclass(frozen=True, slots=True)
class _ArchiveIndexRecord:
    size: int
    mtime_ns: int
    ctime_ns: int
    device: int
    inode: int
    sha256: str


@dataclass(slots=True)
class _HistorySession:
    records: dict[str, _HistoryRecord]
    pending_hashes: set[str]
    pending_queued_archives: dict[str, Path]


class _Guard:
    def __init__(
        self,
        service: LtkMigrationService,
        cancellation: CancelSignal | None,
    ) -> None:
        self._service = service
        self._cancellation = cancellation
        self._last_process_check = 0.0

    def checkpoint(self, *, force_process_check: bool = False) -> None:
        if self._cancellation is not None and self._cancellation.is_set():
            raise _MigrationCancelled("LTK migration was cancelled")
        now = time.monotonic()
        if force_process_check or now - self._last_process_check >= _PROCESS_CHECK_INTERVAL_SECONDS:
            self._service._ensure_processes_stopped()
            self._last_process_check = now


class _ProgressReporter:
    def __init__(self, callback: ProgressCallback | None, total: int) -> None:
        self._callback = callback
        self._total = total

    def emit(
        self,
        phase: str,
        completed: int,
        *,
        mod_name: str | None = None,
        source: Path | None = None,
    ) -> None:
        if self._callback is None:
            return
        try:
            self._callback(
                MigrationProgress(
                    phase=phase,
                    completed=completed,
                    total=self._total,
                    mod_name=mod_name,
                    source=source,
                )
            )
        except Exception:
            LOGGER.exception("LTK migration progress callback failed")


class LtkMigrationService:
    """Synchronously queue extracted CSLOL mods for LTK to import later."""

    def __init__(
        self,
        managed_state_path: Path,
        package_cache_dir: Path,
        *,
        ltk_app_data_dir: Path | None = None,
        report_dir: Path | None = None,
        migration_state_path: Path | None = None,
        archive_index_path: Path | None = None,
        cslol_is_running: Callable[[], bool] | None = None,
        ltk_is_running: Callable[[], bool] | None = None,
        max_mods: int = DEFAULT_MAX_MODS,
        max_members_per_mod: int = DEFAULT_MAX_MEMBERS,
        max_compressed_bytes: int = DEFAULT_MAX_COMPRESSED_BYTES,
        max_uncompressed_bytes: int = DEFAULT_MAX_UNCOMPRESSED_BYTES,
        max_total_uncompressed_bytes: int = DEFAULT_MAX_TOTAL_UNCOMPRESSED_BYTES,
        max_existing_archives: int = DEFAULT_MAX_EXISTING_ARCHIVES,
        max_history_records: int = DEFAULT_MAX_HISTORY_RECORDS,
        max_history_bytes: int = DEFAULT_MAX_HISTORY_BYTES,
        history_checkpoint_records: int = DEFAULT_HISTORY_CHECKPOINT_RECORDS,
    ) -> None:
        if (
            min(
                max_mods,
                max_members_per_mod,
                max_compressed_bytes,
                max_uncompressed_bytes,
                max_total_uncompressed_bytes,
                max_existing_archives,
                max_history_records,
                max_history_bytes,
                history_checkpoint_records,
            )
            < 1
        ):
            raise ValueError("LTK migration resource limits must be positive")
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
        self.migration_state_path = _absolute_path(
            migration_state_path
            if migration_state_path is not None
            else self.managed_state_path.parent / "ltk_migration_state.json"
        )
        self.archive_index_path = _absolute_path(
            archive_index_path
            if archive_index_path is not None
            else self.migration_state_path.parent / "ltk_archive_index.json"
        )
        if self.migration_state_path == self.managed_state_path:
            raise ValueError("Migration ledger must be separate from managed-skin state")
        if self.archive_index_path in {self.managed_state_path, self.migration_state_path}:
            raise ValueError("LTK archive index must use its own VN-owned file")
        self.cslol_is_running = cslol_is_running or (lambda: False)
        self.ltk_is_running = ltk_is_running or (lambda: False)
        self.max_mods = max_mods
        self.max_members_per_mod = max_members_per_mod
        self.max_compressed_bytes = max_compressed_bytes
        self.max_uncompressed_bytes = max_uncompressed_bytes
        self.max_total_uncompressed_bytes = max_total_uncompressed_bytes
        self.max_existing_archives = max_existing_archives
        self.max_history_records = max_history_records
        self.max_history_bytes = max_history_bytes
        self.history_checkpoint_records = history_checkpoint_records
        self._lock = threading.Lock()

    def normalize_source(self, selection: Path) -> Path:
        """Return the selected CSLOL ``installed`` directory without following links."""

        selected = _absolute_path(selection)
        if not _is_safe_directory(selected):
            raise MigrationSourceError(f"Selected folder is not a safe directory: {selected}")
        if selected.name.casefold() == "installed":
            return selected

        try:
            matching = [
                child for child in selected.iterdir() if child.name.casefold() == "installed"
            ]
        except OSError as error:
            raise MigrationSourceError(f"Could not inspect selected folder: {selected}") from error
        if len(matching) != 1 or not _is_safe_directory(matching[0]):
            raise MigrationSourceError("Select CSLOL Manager's root folder or its installed folder")
        return _absolute_path(matching[0])

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

    def inspect_managed_port_status(self, selection: Path) -> ManagedPortStatus:
        """Return how many VN-managed mods still need an explicit LTK port.

        A mod is considered ported only when the migration ledger contains the
        exact absolute installed source path together with the managed state's
        installed-content digest. The inspection shares the migration lock so
        it cannot observe a ledger checkpoint in progress, and it never creates
        or changes files.
        """

        if not self._lock.acquire(blocking=False):
            raise MigrationBusyError("An LTK migration is already in progress")
        try:
            source_dir = self.normalize_source(selection)
            state = self._load_managed_state()
            history = self._load_history()
            ported = {
                (record.source, record.content_sha256)
                for record in history.values()
                if record.content_sha256 is not None
            }
            managed_sources = {
                str(_absolute_path(source_dir / entry.directory)) for entry in state.entries
            }
            # The ledger is keyed by archive digest, so two different VN-owned
            # directories with byte-identical package content intentionally share
            # one record. Treat that content as handed off only when the retained
            # record belongs to another current VN-managed entry; an unrelated
            # user mod or external CSLOL folder must not clear VN's reminder.
            ported_content = {
                record.content_sha256
                for record in history.values()
                if record.content_sha256 is not None and record.source in managed_sources
            }
            pending = sum(
                1
                for entry in state.entries
                if not entry.content_sha256
                or (
                    (
                        str(_absolute_path(source_dir / entry.directory)),
                        entry.content_sha256,
                    )
                    not in ported
                    and entry.content_sha256 not in ported_content
                )
            )
            return ManagedPortStatus(total=len(state.entries), pending=pending)
        finally:
            self._lock.release()

    def forget_history(self) -> None:
        """Atomically clear VN's migration ledger so packages may be requeued.

        This is an explicit recovery/user action.  A malformed regular ledger
        can be reset, but a symlink, reparse point, or non-file is rejected.
        No LTK-owned state is read or changed.
        """

        if not self._lock.acquire(blocking=False):
            raise MigrationBusyError("An LTK migration is already in progress")
        try:
            storage_dir = self.resolve_storage_dir()
            _require_history_outside_ltk(self.migration_state_path, self.ltk_app_data_dir)
            _require_history_outside_ltk(self.migration_state_path, storage_dir)
            _require_archive_index_outside_ltk(self.archive_index_path, self.ltk_app_data_dir)
            _require_archive_index_outside_ltk(self.archive_index_path, storage_dir)
            if os.path.lexists(self.migration_state_path) and not _is_safe_regular_file(
                self.migration_state_path
            ):
                raise MigrationHistoryError(f"Unsafe migration ledger: {self.migration_state_path}")
            self._write_history({})
        finally:
            self._lock.release()

    def migrate(
        self,
        selection: Path,
        *,
        cancel_event: CancelSignal | None = None,
        progress: ProgressCallback | None = None,
    ) -> MigrationResult:
        """Queue every valid immediate child mod in *selection* for LTK.

        Invalid individual mods are reported and do not prevent other mods
        from being queued.  Cancellation or a manager starting mid-pass yields
        a partial result; managers already running block before any mutation.
        """

        if progress is not None and not callable(progress):
            raise TypeError("progress must be callable")
        if not self._lock.acquire(blocking=False):
            raise MigrationBusyError("An LTK migration is already in progress")
        try:
            return self._migrate_locked(selection, cancel_event, progress)
        finally:
            self._lock.release()

    def _migrate_locked(
        self,
        selection: Path,
        cancel_event: CancelSignal | None,
        progress: ProgressCallback | None,
    ) -> MigrationResult:
        source_dir = self.normalize_source(selection)
        storage_dir = self.resolve_storage_dir()
        archives_dir = storage_dir / LTK_ARCHIVES_DIRECTORY_NAME
        _require_disjoint(source_dir, archives_dir)
        if _is_within(self.migration_state_path, source_dir):
            raise MigrationHistoryError("Migration ledger cannot be inside the CSLOL source")
        if _is_within(self.archive_index_path, source_dir):
            raise MigrationHistoryError("Archive index cannot be inside the CSLOL source")
        _require_history_outside_ltk(self.migration_state_path, self.ltk_app_data_dir)
        _require_history_outside_ltk(self.migration_state_path, storage_dir)
        _require_archive_index_outside_ltk(self.archive_index_path, self.ltk_app_data_dir)
        _require_archive_index_outside_ltk(self.archive_index_path, storage_dir)
        self._ensure_processes_stopped()
        history = self._load_history()
        history_session = _HistorySession(
            records=history,
            pending_hashes=set(),
            pending_queued_archives={},
        )
        known_hashes = set(history)
        known_content = {
            (record.source, record.content_sha256)
            for record in history.values()
            if record.content_sha256 is not None
        }
        legacy_sources = {
            record.source for record in history.values() if record.content_sha256 is None
        }

        candidates = self._scan_candidates(source_dir)
        reporter = _ProgressReporter(progress, len(candidates))
        guard = _Guard(self, cancel_event)
        report_path = self._new_report_path()
        issues: list[MigrationIssue] = []
        queued = 0
        skipped = 0
        failed = 0
        reused_cache = 0
        packaged = 0
        processed = 0
        status = "completed"
        archives_indexed = False

        def index_archives_once() -> None:
            nonlocal archives_indexed
            if archives_indexed:
                return
            _ensure_safe_directory_tree(archives_dir)
            reporter.emit("indexing", processed)
            known_hashes.update(self._index_existing_archives(archives_dir, guard))
            archives_indexed = True

        try:
            reporter.emit("scanning", 0)
            guard.checkpoint(force_process_check=True)
            managed_entries = self._load_managed_entries()
            aggregate_bytes = 0

            for candidate in candidates:
                guard.checkpoint(force_process_check=True)
                reporter.emit(
                    "migrating",
                    processed,
                    mod_name=candidate.name,
                    source=candidate,
                )
                try:
                    prepared = self._prepare_mod(candidate)
                    aggregate_bytes += prepared.tree.total_bytes
                    if aggregate_bytes > self.max_total_uncompressed_bytes:
                        raise LtkMigrationError(
                            "Selected mods exceed the aggregate uncompressed-size limit"
                        )
                    content_sha256 = prepared.fingerprint.sha256
                    if (str(candidate), content_sha256) in known_content:
                        skipped += 1
                        processed += 1
                        reporter.emit("migrating", processed, source=candidate)
                        continue
                    entry = managed_entries.get(candidate.name)
                    cache_path = self._verified_cache(entry, prepared.fingerprint)
                    if cache_path is not None:
                        reused_cache += 1
                        archive_hash = _stable_sha256(cache_path, guard)
                        if archive_hash not in known_hashes:
                            index_archives_once()
                        if archive_hash in known_hashes:
                            self._remember_history(
                                history_session,
                                archive_hash,
                                candidate,
                                prepared.display_name,
                                content_sha256,
                                known_hashes=known_hashes,
                            )
                            skipped += 1
                        else:
                            reporter.emit(
                                "copying",
                                processed,
                                mod_name=prepared.display_name,
                                source=candidate,
                            )
                            staged = self._copy_to_partial(cache_path, archives_dir, guard)
                            try:
                                self._validate_staged_archive(
                                    staged,
                                    expected_size=entry.size if entry is not None else None,
                                    expected_sha=entry.source_sha if entry is not None else None,
                                )
                                self._require_unchanged(candidate, prepared.fingerprint)
                                guard.checkpoint(force_process_check=True)
                                if self._commit_and_remember(
                                    staged,
                                    archive_hash,
                                    known_hashes,
                                    history_session,
                                    candidate,
                                    prepared.display_name,
                                    content_sha256,
                                ):
                                    queued += 1
                                else:
                                    skipped += 1
                            finally:
                                staged.unlink(missing_ok=True)
                    else:
                        packaged += 1
                        reporter.emit(
                            "packaging",
                            processed,
                            mod_name=prepared.display_name,
                            source=candidate,
                        )
                        _ensure_safe_directory_tree(archives_dir)
                        staged = self._package_to_partial(
                            prepared.tree,
                            archives_dir,
                            guard,
                            fast=str(candidate) not in legacy_sources,
                        )
                        try:
                            self._validate_staged_archive(staged)
                            archive_hash = _stable_sha256(staged, guard)
                            self._require_unchanged(candidate, prepared.fingerprint)
                            guard.checkpoint(force_process_check=True)
                            if archive_hash not in known_hashes:
                                index_archives_once()
                            if archive_hash in known_hashes:
                                self._remember_history(
                                    history_session,
                                    archive_hash,
                                    candidate,
                                    prepared.display_name,
                                    content_sha256,
                                    known_hashes=known_hashes,
                                )
                                skipped += 1
                            elif self._commit_and_remember(
                                staged,
                                archive_hash,
                                known_hashes,
                                history_session,
                                candidate,
                                prepared.display_name,
                                content_sha256,
                            ):
                                queued += 1
                            else:
                                skipped += 1
                        finally:
                            staged.unlink(missing_ok=True)
                except (_MigrationCancelled, MigrationBlockedError):
                    raise
                except MigrationHistoryError:
                    raise
                except (FantomeError, LtkMigrationError, OSError, ValueError) as error:
                    failed += 1
                    issues.append(
                        MigrationIssue(
                            source=candidate,
                            mod_name=candidate.name,
                            reason=str(error) or error.__class__.__name__,
                        )
                    )
                    LOGGER.warning("Could not migrate %s: %s", candidate, error)
                processed += 1
                reporter.emit("migrating", processed, source=candidate)
        except _MigrationCancelled as error:
            status = "cancelled"
            issues.append(MigrationIssue(source=source_dir, reason=str(error)))
        except MigrationBlockedError as error:
            status = "blocked"
            issues.append(MigrationIssue(source=source_dir, reason=str(error)))

        self._checkpoint_history(history_session, known_hashes)
        result = MigrationResult(
            source_dir=source_dir,
            storage_dir=storage_dir,
            archives_dir=archives_dir,
            report_path=report_path,
            status=status,
            discovered=len(candidates),
            queued=queued,
            skipped=skipped,
            failed=failed,
            reused_cache=reused_cache,
            packaged=packaged,
            issues=tuple(issues),
        )
        try:
            self._write_report(result)
        except (OSError, LtkMigrationError) as error:
            # Archive and ledger checkpoints are the authoritative handoff. An
            # unavailable diagnostics directory must not turn a completed,
            # durable queue operation into a misleading "not started" failure.
            message = str(error) or error.__class__.__name__
            LOGGER.error("LTK migration completed but its audit report was unavailable: %s", error)
            result = replace(result, report_error=message)
        reporter.emit(status, processed)
        return result

    def _scan_candidates(self, source_dir: Path) -> tuple[Path, ...]:
        try:
            entries = sorted(
                source_dir.iterdir(),
                key=lambda item: (item.name.casefold(), item.name),
            )
        except OSError as error:
            raise MigrationSourceError(
                f"Could not scan CSLOL installed folder: {source_dir}"
            ) from error
        candidates: list[Path] = []
        for entry in entries:
            try:
                entry_stat = os.stat(entry, follow_symlinks=False)
            except OSError:
                # Keep unreadable entries so they appear in the per-mod report.
                candidates.append(entry)
                continue
            if (
                stat.S_ISDIR(entry_stat.st_mode)
                or entry.is_symlink()
                or _is_reparse_stat(entry_stat)
            ):
                candidates.append(entry)
        if len(candidates) > self.max_mods:
            raise MigrationSourceError(
                f"Installed folder contains too many mods ({len(candidates)} > {self.max_mods})"
            )
        return tuple(candidates)

    def _prepare_mod(self, source: Path) -> _PreparedMod:
        tree = _collect_mod_tree(
            source,
            max_members=self.max_members_per_mod,
            max_uncompressed_bytes=self.max_uncompressed_bytes,
        )
        fingerprint = inspect_extracted_mod(
            source,
            max_uncompressed_bytes=self.max_uncompressed_bytes,
        )
        if fingerprint is None or fingerprint.file_count != len(tree.files):
            raise LtkMigrationError("Mod is unsafe, malformed, or changed while being inspected")
        display_name = _read_display_name(tree)
        return _PreparedMod(tree=tree, fingerprint=fingerprint, display_name=display_name)

    def _load_managed_entries(self) -> dict[str, ManagedEntry]:
        try:
            state = self._load_managed_state()
        except ManagedStateError:
            LOGGER.warning("Managed-skin state is unavailable; live mods will be repackaged")
            return {}
        return {entry.directory: entry for entry in state.entries}

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
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, LtkMigrationError) as error:
            raise ManagedStateError("Managed-skin state is unreadable") from error
        return ManagedState.from_json(raw)

    def _load_history(self) -> dict[str, _HistoryRecord]:
        path = self.migration_state_path
        if not os.path.lexists(path):
            return {}
        if not _is_safe_regular_file(path):
            raise MigrationHistoryError(f"Unsafe migration ledger: {path}")
        try:
            before = _safe_file_stat(path)
        except LtkMigrationError as error:
            raise MigrationHistoryError(f"Could not inspect migration ledger: {path}") from error
        if before.st_size > self.max_history_bytes:
            raise MigrationHistoryError(
                f"Migration ledger exceeds its size limit "
                f"({before.st_size} > {self.max_history_bytes})"
            )
        try:
            with _open_regular_file(path, before) as stream:
                encoded = stream.read(self.max_history_bytes + 1)
            _require_same_file(path, before)
            raw = json.loads(encoded.decode("utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, LtkMigrationError) as error:
            raise MigrationHistoryError("Migration ledger is not valid UTF-8 JSON") from error
        if not isinstance(raw, Mapping):
            raise MigrationHistoryError("Migration ledger must be a JSON object")
        schema_version = raw.get("schema_version")
        if (
            isinstance(schema_version, bool)
            or not isinstance(schema_version, int)
            or schema_version != MIGRATION_STATE_SCHEMA_VERSION
        ):
            raise MigrationHistoryError("Migration ledger has an unsupported schema version")
        packages = raw.get("packages")
        if not isinstance(packages, Mapping):
            raise MigrationHistoryError("Migration ledger packages must be a JSON object")
        if len(packages) > self.max_history_records:
            raise MigrationHistoryError(
                f"Migration ledger contains too many records "
                f"({len(packages)} > {self.max_history_records})"
            )

        history: dict[str, _HistoryRecord] = {}
        for digest, value in packages.items():
            if not isinstance(digest, str) or not _SHA256_PATTERN.fullmatch(digest):
                raise MigrationHistoryError("Migration ledger contains an invalid package digest")
            history[digest] = _history_record_from_json(value)
        return history

    def _write_history(self, history: Mapping[str, _HistoryRecord]) -> None:
        if len(history) > self.max_history_records:
            raise MigrationHistoryError("Migration history record limit has been reached")
        for digest, record in history.items():
            if not _SHA256_PATTERN.fullmatch(digest):
                raise MigrationHistoryError("Migration history contains an invalid digest")
            _validate_history_record(record)
        payload = {
            "schema_version": MIGRATION_STATE_SCHEMA_VERSION,
            "packages": {
                digest: {
                    "source": record.source,
                    "name": record.name,
                    "queued_at": record.queued_at,
                    "content_sha256": record.content_sha256,
                }
                for digest, record in history.items()
            },
        }
        encoded = (json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False) + "\n").encode(
            "utf-8"
        )
        if len(encoded) > self.max_history_bytes:
            raise MigrationHistoryError("Migration ledger would exceed its size limit")
        try:
            _ensure_safe_directory_tree(self.migration_state_path.parent)
            atomic_write_bytes(self.migration_state_path, encoded)
        except (OSError, LtkMigrationError) as error:
            raise MigrationHistoryError("Could not persist the migration ledger") from error

    def _remember_history(
        self,
        session: _HistorySession,
        archive_hash: str,
        source: Path,
        display_name: str,
        content_sha256: str,
        *,
        known_hashes: set[str],
        queued_archive: Path | None = None,
    ) -> None:
        if not _SHA256_PATTERN.fullmatch(content_sha256):
            raise MigrationHistoryError("Migration history content digest is invalid")
        existing = session.records.get(archive_hash)
        if existing is not None:
            if existing.source == str(source) and existing.content_sha256 is None:
                session.records[archive_hash] = _HistoryRecord(
                    source=existing.source,
                    name=existing.name,
                    queued_at=existing.queued_at,
                    content_sha256=content_sha256,
                )
                session.pending_hashes.add(archive_hash)
                if len(session.pending_hashes) >= self.history_checkpoint_records:
                    self._checkpoint_history(session, known_hashes)
            return
        if len(session.records) >= self.max_history_records:
            raise MigrationHistoryError("Migration history record limit has been reached")
        record = _HistoryRecord(
            source=str(source),
            name=display_name,
            queued_at=datetime.now(timezone.utc).isoformat(),
            content_sha256=content_sha256,
        )
        _validate_history_record(record)
        session.records[archive_hash] = record
        session.pending_hashes.add(archive_hash)
        if queued_archive is not None:
            session.pending_queued_archives[archive_hash] = queued_archive
        if len(session.pending_hashes) >= self.history_checkpoint_records:
            self._checkpoint_history(session, known_hashes)

    def _checkpoint_history(
        self,
        session: _HistorySession,
        known_hashes: set[str],
    ) -> None:
        if not session.pending_hashes:
            return
        try:
            self._write_history(session.records)
        except MigrationHistoryError:
            rollback_errors: list[BaseException] = []
            for digest, archive in tuple(session.pending_queued_archives.items()):
                try:
                    self._rollback_queued_archive(archive, digest)
                    known_hashes.discard(digest)
                except (OSError, LtkMigrationError) as error:
                    rollback_errors.append(error)
            for digest in session.pending_hashes:
                session.records.pop(digest, None)
            session.pending_hashes.clear()
            session.pending_queued_archives.clear()
            if rollback_errors:
                raise MigrationHistoryError(
                    "Could not persist migration history or roll back every pending archive"
                ) from rollback_errors[0]
            raise
        session.pending_hashes.clear()
        session.pending_queued_archives.clear()

    def _rollback_queued_archive(self, archive: Path, archive_hash: str) -> None:
        if not _is_safe_regular_file(archive):
            raise OSError("queued archive is no longer a safe regular file")
        if _plain_sha256(archive) != archive_hash:
            raise OSError("queued archive changed before ledger persistence")
        archive.unlink()

    def _verified_cache(
        self,
        entry: ManagedEntry | None,
        fingerprint: ExtractedModFingerprint,
    ) -> Path | None:
        if entry is None or not entry.content_sha256 or entry.content_sha256 != fingerprint.sha256:
            return None
        cached = self.package_cache_dir / f"{entry.source_sha}.fantome"
        try:
            validate_fantome(
                cached,
                expected_size=entry.size,
                expected_sha=entry.source_sha,
                max_members=self.max_members_per_mod,
                max_compressed_bytes=self.max_compressed_bytes,
                max_uncompressed_bytes=self.max_uncompressed_bytes,
            )
        except (FantomeError, OSError, ValueError):
            LOGGER.warning("Ignoring invalid cached Fantome for %s", entry.directory)
            return None
        return cached

    def _index_existing_archives(
        self,
        archives_dir: Path,
        guard: _Guard,
    ) -> set[str]:
        cached = self._load_archive_index(archives_dir)
        try:
            entries = sorted(
                archives_dir.iterdir(),
                key=lambda item: (item.name.casefold(), item.name),
            )
        except OSError as error:
            raise LtkMigrationError(
                f"Could not inspect LTK archive inbox: {archives_dir}"
            ) from error
        candidates = [
            entry for entry in entries if entry.suffix.casefold() in {".fantome", ".modpkg"}
        ]
        if len(candidates) > self.max_existing_archives:
            raise LtkMigrationError(
                "LTK archive inbox contains too many packages to deduplicate safely"
            )
        hashes: set[str] = set()
        updated: dict[str, _ArchiveIndexRecord] = {}
        for archive in candidates:
            guard.checkpoint()
            if not _is_safe_regular_file(archive):
                LOGGER.warning("Ignoring unsafe existing LTK archive: %s", archive)
                continue
            try:
                archive_stat = _safe_file_stat(archive)
                if archive_stat.st_size > self.max_compressed_bytes:
                    LOGGER.warning("Ignoring oversized existing LTK archive: %s", archive)
                    continue
                record = cached.get(archive.name)
                if record is not None and _archive_record_matches(record, archive_stat):
                    digest = record.sha256
                else:
                    digest = _stable_sha256(archive, guard)
                    archive_stat = _safe_file_stat(archive)
                hashes.add(digest)
                updated[archive.name] = _archive_record(archive_stat, digest)
            except (LtkMigrationError, OSError):
                LOGGER.warning("Could not hash existing LTK archive: %s", archive)
        self._write_archive_index(archives_dir, updated)
        return hashes

    def _load_archive_index(self, archives_dir: Path) -> dict[str, _ArchiveIndexRecord]:
        path = self.archive_index_path
        if not os.path.lexists(path):
            return {}
        if not _is_safe_regular_file(path):
            LOGGER.warning("Ignoring unsafe VN-owned LTK archive index: %s", path)
            return {}
        try:
            before = _safe_file_stat(path)
            if before.st_size > self.max_history_bytes:
                raise ValueError("archive index exceeds its size limit")
            with _open_regular_file(path, before) as stream:
                encoded = stream.read(self.max_history_bytes + 1)
            _require_same_file(path, before)
            raw = json.loads(encoded.decode("utf-8"))
            if not isinstance(raw, Mapping):
                raise ValueError("archive index must be a JSON object")
            if raw.get("schema_version") != ARCHIVE_INDEX_SCHEMA_VERSION:
                raise ValueError("archive index schema is unsupported")
            expected_storage = os.path.normcase(str(_absolute_path(archives_dir)))
            raw_storage = raw.get("archives_dir")
            if (
                not isinstance(raw_storage, str)
                or os.path.normcase(raw_storage) != expected_storage
            ):
                return {}
            records = raw.get("archives")
            if not isinstance(records, Mapping) or len(records) > self.max_existing_archives:
                raise ValueError("archive index records are invalid")
            parsed: dict[str, _ArchiveIndexRecord] = {}
            for name, value in records.items():
                parsed[_validate_archive_index_name(name)] = _archive_record_from_json(value)
            return parsed
        except (
            LtkMigrationError,
            OSError,
            UnicodeDecodeError,
            json.JSONDecodeError,
            ValueError,
        ) as error:
            LOGGER.warning("Ignoring unreadable VN-owned LTK archive index: %s", error)
            return {}

    def _write_archive_index(
        self,
        archives_dir: Path,
        records: Mapping[str, _ArchiveIndexRecord],
    ) -> None:
        path = self.archive_index_path
        if os.path.lexists(path) and not _is_safe_regular_file(path):
            LOGGER.warning("Cannot update unsafe VN-owned LTK archive index: %s", path)
            return
        payload = {
            "schema_version": ARCHIVE_INDEX_SCHEMA_VERSION,
            "archives_dir": str(_absolute_path(archives_dir)),
            "archives": {
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
            _ensure_safe_directory_tree(path.parent)
            # Match atomic_write_json's exact formatting so the limit applies
            # to the bytes that will actually be persisted.
            encoded = (
                json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
            ).encode("utf-8")
            if len(encoded) > self.max_history_bytes:
                raise ValueError("archive index would exceed its size limit")
            atomic_write_json(path, payload)
        except (LtkMigrationError, OSError, ValueError) as error:
            LOGGER.warning("Could not update VN-owned LTK archive index: %s", error)

    def _copy_to_partial(self, source: Path, archives_dir: Path, guard: _Guard) -> Path:
        staged = archives_dir / f".lsmvn-{uuid4().hex}.partial"
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
            guard.checkpoint(force_process_check=True)
            return staged
        except BaseException:
            staged.unlink(missing_ok=True)
            raise

    def _package_to_partial(
        self,
        tree: _ModTree,
        archives_dir: Path,
        guard: _Guard,
        *,
        fast: bool,
    ) -> Path:
        staged = archives_dir / f".lsmvn-{uuid4().hex}.partial"
        compression = zipfile.ZIP_STORED if fast else zipfile.ZIP_DEFLATED
        try:
            with staged.open("xb") as raw_output:
                with zipfile.ZipFile(
                    raw_output,
                    mode="w",
                    compression=compression,
                    compresslevel=None if fast else 9,
                    strict_timestamps=True,
                ) as archive:
                    for relative in tree.directories:
                        guard.checkpoint()
                        info = _deterministic_zip_info(
                            f"{relative.as_posix()}/",
                            directory=True,
                            compression=compression,
                        )
                        archive.writestr(info, b"")
                    for item in tree.files:
                        guard.checkpoint()
                        info = _deterministic_zip_info(
                            item.relative.as_posix(),
                            directory=False,
                            compression=compression,
                        )
                        with (
                            _open_regular_file(item.path, item.stat_result) as source,
                            archive.open(info, mode="w", force_zip64=True) as destination,
                        ):
                            while chunk := source.read(_COPY_CHUNK_SIZE):
                                guard.checkpoint()
                                destination.write(chunk)
                        _require_same_file(item.path, item.stat_result)
                raw_output.flush()
                os.fsync(raw_output.fileno())
            guard.checkpoint(force_process_check=True)
            return staged
        except BaseException:
            staged.unlink(missing_ok=True)
            raise

    def _validate_staged_archive(
        self,
        staged: Path,
        *,
        expected_size: int | None = None,
        expected_sha: str | None = None,
    ) -> None:
        validate_fantome(
            staged,
            expected_size=expected_size,
            expected_sha=expected_sha,
            max_members=self.max_members_per_mod,
            max_compressed_bytes=self.max_compressed_bytes,
            max_uncompressed_bytes=self.max_uncompressed_bytes,
        )

    def _require_unchanged(
        self,
        source: Path,
        expected: ExtractedModFingerprint,
    ) -> None:
        current = inspect_extracted_mod(
            source,
            max_uncompressed_bytes=self.max_uncompressed_bytes,
        )
        if current != expected:
            raise LtkMigrationError(f"Source mod changed while being migrated: {source.name}")

    def _commit_archive(
        self,
        staged: Path,
        archive_hash: str,
        existing_hashes: set[str],
    ) -> bool:
        if not _SHA256_PATTERN.fullmatch(archive_hash):
            raise LtkMigrationError("Generated archive digest is invalid")
        if archive_hash in existing_hashes:
            return False
        destination = staged.parent / f"lsmvn-{archive_hash}.fantome"
        if destination.exists() or destination.is_symlink():
            if _is_safe_regular_file(destination) and _plain_sha256(destination) == archive_hash:
                existing_hashes.add(archive_hash)
                return False
            raise LtkMigrationError(f"LTK archive destination collision: {destination.name}")

        try:
            if os.name == "nt":
                # Windows rename is atomic and refuses to replace an existing
                # destination, so the watcher can never observe partial bytes.
                os.rename(staged, destination)
            else:
                # POSIX rename replaces an existing destination.  An atomic
                # hard-link provides create-if-absent semantics instead.
                os.link(staged, destination)
                staged.unlink()
        except FileExistsError as error:
            if _is_safe_regular_file(destination) and _plain_sha256(destination) == archive_hash:
                existing_hashes.add(archive_hash)
                return False
            raise LtkMigrationError(
                f"LTK archive destination collision: {destination.name}"
            ) from error
        except OSError as error:
            if destination.exists() or destination.is_symlink():
                raise LtkMigrationError(
                    f"LTK archive destination collision: {destination.name}"
                ) from error
            raise LtkMigrationError(f"Could not commit LTK archive: {destination.name}") from error
        _fsync_directory(destination.parent)
        existing_hashes.add(archive_hash)
        return True

    def _commit_and_remember(
        self,
        staged: Path,
        archive_hash: str,
        known_hashes: set[str],
        session: _HistorySession,
        source: Path,
        display_name: str,
        content_sha256: str,
    ) -> bool:
        queued = self._commit_archive(staged, archive_hash, known_hashes)
        destination = staged.parent / f"lsmvn-{archive_hash}.fantome"
        try:
            self._remember_history(
                session,
                archive_hash,
                source,
                display_name,
                content_sha256,
                known_hashes=known_hashes,
                queued_archive=destination if queued else None,
            )
        except MigrationHistoryError:
            if queued and (destination.exists() or destination.is_symlink()):
                try:
                    self._rollback_queued_archive(destination, archive_hash)
                    known_hashes.discard(archive_hash)
                except (OSError, LtkMigrationError) as rollback_error:
                    raise MigrationHistoryError(
                        "Could not persist migration history or roll back the queued archive"
                    ) from rollback_error
            raise
        return queued

    def _ensure_processes_stopped(self) -> None:
        for label, predicate in (
            ("CSLOL Manager", self.cslol_is_running),
            ("LTK Manager", self.ltk_is_running),
        ):
            try:
                running = predicate()
            except Exception as error:
                raise MigrationBlockedError(
                    f"Could not verify whether {label} is running"
                ) from error
            if not isinstance(running, bool):
                raise MigrationBlockedError(f"Could not verify whether {label} is running")
            if running:
                raise MigrationBlockedError(f"Close {label} before migrating skins")

    def _new_report_path(self) -> Path:
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        return self.report_dir / f"ltk-migration-{timestamp}-{uuid4().hex[:8]}.json"

    def _write_report(self, result: MigrationResult) -> None:
        _ensure_safe_directory_tree(result.report_path.parent)
        atomic_write_json(
            result.report_path,
            {
                "schema_version": MIGRATION_REPORT_SCHEMA_VERSION,
                "created_at": datetime.now(timezone.utc).isoformat(),
                "status": result.status,
                "source_dir": str(result.source_dir),
                "storage_dir": str(result.storage_dir),
                "archives_dir": str(result.archives_dir),
                "discovered": result.discovered,
                "queued": result.queued,
                "skipped": result.skipped,
                "failed": result.failed,
                "reused_cache": result.reused_cache,
                "packaged": result.packaged,
                "issues": [
                    {
                        "source": str(issue.source),
                        "mod_name": issue.mod_name,
                        "reason": issue.reason,
                    }
                    for issue in result.issues
                ],
            },
        )


def _collect_mod_tree(
    root: Path,
    *,
    max_members: int,
    max_uncompressed_bytes: int,
) -> _ModTree:
    if not _is_safe_directory(root):
        raise LtkMigrationError("Mod root is a symlink, reparse point, or unsafe directory")
    pending: list[tuple[Path, PurePosixPath]] = [(root, PurePosixPath())]
    directories: list[PurePosixPath] = []
    files: list[_TreeFile] = []
    seen_windows_paths: set[str] = set()
    member_count = 0
    total_bytes = 0

    while pending:
        parent, relative_parent = pending.pop()
        try:
            children = sorted(
                os.scandir(parent),
                key=lambda child: (child.name.casefold(), child.name),
            )
        except OSError as error:
            raise LtkMigrationError(f"Could not inspect mod directory: {parent}") from error
        for child in children:
            if not _safe_windows_component(child.name):
                raise LtkMigrationError(f"Mod contains an unsafe Windows name: {child.name}")
            relative = relative_parent / child.name
            collision_key = relative.as_posix().casefold()
            if collision_key in seen_windows_paths:
                raise LtkMigrationError(
                    f"Mod contains duplicate Windows path: {relative.as_posix()}"
                )
            seen_windows_paths.add(collision_key)
            member_count += 1
            if member_count > max_members:
                raise LtkMigrationError(
                    f"Mod contains too many entries ({member_count} > {max_members})"
                )
            try:
                child_stat = child.stat(follow_symlinks=False)
            except OSError as error:
                raise LtkMigrationError(f"Could not inspect mod entry: {relative}") from error
            if child.is_symlink() or _is_reparse_stat(child_stat):
                raise LtkMigrationError(f"Mod contains a symlink or reparse point: {relative}")
            child_path = Path(child.path)
            if stat.S_ISDIR(child_stat.st_mode):
                directories.append(relative)
                pending.append((child_path, relative))
            elif stat.S_ISREG(child_stat.st_mode):
                total_bytes += child_stat.st_size
                if total_bytes > max_uncompressed_bytes:
                    raise LtkMigrationError("Mod exceeds the uncompressed-size safety limit")
                files.append(
                    _TreeFile(
                        path=child_path,
                        relative=relative,
                        stat_result=child_stat,
                    )
                )
            else:
                raise LtkMigrationError(f"Mod contains a special file: {relative}")

    directories.sort(key=lambda path: (path.as_posix().casefold(), path.as_posix()))
    files.sort(key=lambda item: (item.relative.as_posix().casefold(), item.relative.as_posix()))
    return _ModTree(
        directories=tuple(directories),
        files=tuple(files),
        total_bytes=total_bytes,
    )


def _read_display_name(tree: _ModTree) -> str:
    metadata = next(
        (item for item in tree.files if item.relative.as_posix() == "META/info.json"),
        None,
    )
    if metadata is None or metadata.stat_result.st_size > DEFAULT_MAX_METADATA_BYTES:
        raise LtkMigrationError("Mod is missing safe META/info.json metadata")
    try:
        with _open_regular_file(metadata.path, metadata.stat_result) as stream:
            raw = stream.read(DEFAULT_MAX_METADATA_BYTES + 1)
        _require_same_file(metadata.path, metadata.stat_result)
        value = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise LtkMigrationError("Mod META/info.json is not valid UTF-8 JSON") from error
    if not isinstance(value, Mapping):
        raise LtkMigrationError("Mod metadata must be a JSON object")
    for key in ("Name", "name"):
        name = value.get(key)
        if isinstance(name, str) and name.strip():
            return name.strip()
    return metadata.path.parent.parent.name


def _validate_archive_index_name(value: object) -> str:
    if (
        not isinstance(value, str)
        or not value
        or Path(value).name != value
        or "/" in value
        or "\\" in value
        or Path(value).suffix.casefold() not in {".fantome", ".modpkg"}
    ):
        raise ValueError("archive index contains an invalid filename")
    return value


def _archive_record_from_json(value: object) -> _ArchiveIndexRecord:
    if not isinstance(value, Mapping):
        raise ValueError("archive index record must be a JSON object")
    fields = tuple(value.get(name) for name in ("size", "mtime_ns", "ctime_ns", "device", "inode"))
    if any(isinstance(field, bool) or not isinstance(field, int) or field < 0 for field in fields):
        raise ValueError("archive index record has invalid file identity")
    sha256 = value.get("sha256")
    if not isinstance(sha256, str) or not _SHA256_PATTERN.fullmatch(sha256):
        raise ValueError("archive index record has an invalid digest")
    size, mtime_ns, ctime_ns, device, inode = (cast(int, field) for field in fields)
    return _ArchiveIndexRecord(size, mtime_ns, ctime_ns, device, inode, sha256)


def _archive_record(value: os.stat_result, sha256: str) -> _ArchiveIndexRecord:
    return _ArchiveIndexRecord(
        size=value.st_size,
        mtime_ns=value.st_mtime_ns,
        ctime_ns=value.st_ctime_ns,
        device=value.st_dev,
        inode=value.st_ino,
        sha256=sha256,
    )


def _archive_record_matches(record: _ArchiveIndexRecord, value: os.stat_result) -> bool:
    return record == _archive_record(value, record.sha256)


def _history_record_from_json(value: object) -> _HistoryRecord:
    if not isinstance(value, Mapping):
        raise MigrationHistoryError("Migration history record must be a JSON object")
    source = value.get("source")
    name = value.get("name")
    queued_at = value.get("queued_at")
    content_sha256 = value.get("content_sha256")
    if not isinstance(source, str) or not isinstance(name, str) or not isinstance(queued_at, str):
        raise MigrationHistoryError("Migration history record has invalid fields")
    if content_sha256 is not None and not isinstance(content_sha256, str):
        raise MigrationHistoryError("Migration history content digest has an invalid type")
    record = _HistoryRecord(
        source=source,
        name=name,
        queued_at=queued_at,
        content_sha256=content_sha256,
    )
    _validate_history_record(record)
    return record


def _validate_history_record(record: _HistoryRecord) -> None:
    if not record.source or len(record.source) > _MAX_HISTORY_SOURCE_LENGTH:
        raise MigrationHistoryError("Migration history source path is invalid")
    if not record.name or len(record.name) > _MAX_HISTORY_NAME_LENGTH:
        raise MigrationHistoryError("Migration history package name is invalid")
    if not record.queued_at or len(record.queued_at) > _MAX_HISTORY_TIMESTAMP_LENGTH:
        raise MigrationHistoryError("Migration history timestamp is invalid")
    if record.content_sha256 is not None and not _SHA256_PATTERN.fullmatch(record.content_sha256):
        raise MigrationHistoryError("Migration history content digest is invalid")
    try:
        timestamp = datetime.fromisoformat(record.queued_at)
    except ValueError as error:
        raise MigrationHistoryError("Migration history timestamp is invalid") from error
    if timestamp.tzinfo is None or timestamp.utcoffset() is None:
        raise MigrationHistoryError("Migration history timestamp must include a timezone")


def _deterministic_zip_info(
    name: str,
    *,
    directory: bool,
    compression: int,
) -> zipfile.ZipInfo:
    info = zipfile.ZipInfo(filename=name, date_time=(1980, 1, 1, 0, 0, 0))
    info.compress_type = compression
    info.create_system = 3
    info.external_attr = ((stat.S_IFDIR | 0o755) if directory else (stat.S_IFREG | 0o644)) << 16
    if directory:
        info.external_attr |= 0x10
    return info


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
        raise LtkMigrationError(f"Could not inspect regular file: {path}") from error
    if not stat.S_ISREG(value.st_mode) or _is_reparse_stat(value) or path.is_symlink():
        raise LtkMigrationError(f"Unsafe regular file: {path}")
    return value


def _open_regular_file(path: Path, expected: os.stat_result) -> BinaryIO:
    flags = os.O_RDONLY | int(getattr(os, "O_BINARY", 0))
    flags |= int(getattr(os, "O_NOFOLLOW", 0))
    descriptor = os.open(path, flags)
    try:
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode) or not _same_file_stat(expected, opened):
            raise LtkMigrationError(f"File changed while being opened: {path}")
        return os.fdopen(descriptor, "rb")
    except BaseException:
        os.close(descriptor)
        raise


def _require_same_file(path: Path, expected: os.stat_result) -> None:
    try:
        current = os.stat(path, follow_symlinks=False)
    except OSError as error:
        raise LtkMigrationError(f"File changed while being read: {path}") from error
    if _is_reparse_stat(current) or not _same_file_stat(expected, current):
        raise LtkMigrationError(f"File changed while being read: {path}")


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


def _safe_windows_component(component: str) -> bool:
    if not component or component in {".", ".."} or "\x00" in component:
        return False
    if (
        any(character in _WINDOWS_INVALID_CHARACTERS for character in component)
        or any(ord(character) < 32 for character in component)
        or component.endswith((" ", "."))
    ):
        return False
    return component.split(".", 1)[0].upper() not in _WINDOWS_RESERVED_NAMES


def _absolute_path(path: str | os.PathLike[str]) -> Path:
    return Path(os.path.abspath(os.path.expanduser(os.fspath(path))))


def _ensure_safe_directory_tree(path: Path) -> None:
    current = Path(path.anchor) if path.anchor else Path()
    if path.anchor and not _is_safe_directory(current):
        raise LtkMigrationError(f"Directory path has an unsafe root: {current}")
    parts = path.parts[1:] if path.anchor else path.parts
    for part in parts:
        current /= part
        if not os.path.lexists(current):
            try:
                current.mkdir()
            except FileExistsError:
                pass
            except OSError as error:
                raise LtkMigrationError(f"Could not create directory: {current}") from error
        if not _is_safe_directory(current):
            raise LtkMigrationError(f"Directory path contains a reparse or unsafe entry: {current}")


def _require_disjoint(source_dir: Path, archives_dir: Path) -> None:
    try:
        source_dir.relative_to(archives_dir)
    except ValueError:
        pass
    else:
        raise LtkMigrationError("LTK archive inbox cannot contain the CSLOL source folder")
    try:
        archives_dir.relative_to(source_dir)
    except ValueError:
        return
    raise LtkMigrationError("LTK archive inbox cannot be inside the CSLOL source folder")


def _require_history_outside_ltk(history_path: Path, storage_dir: Path) -> None:
    if _is_within(history_path, storage_dir):
        raise MigrationHistoryError("VN migration ledger cannot be stored in LTK-owned data")


def _require_archive_index_outside_ltk(index_path: Path, storage_dir: Path) -> None:
    if _is_within(index_path, storage_dir):
        raise MigrationHistoryError("VN archive index cannot be stored in LTK-owned data")


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
