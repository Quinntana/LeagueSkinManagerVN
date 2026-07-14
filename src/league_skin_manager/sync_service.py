"""Transactional synchronization of source artifacts into CSLOL's mod folder."""

from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
import threading
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Protocol, cast
from uuid import uuid4

from .atomic import atomic_write_json
from .skin_installer import (
    DEFAULT_MAX_COMPRESSED_BYTES,
    DEFAULT_MAX_UNCOMPRESSED_BYTES,
    MANAGED_DIRECTORY_PREFIX,
    ExtractedModFingerprint,
    ExtractionCancelled,
    FantomeError,
    extract_fantome,
    inspect_extracted_mod,
    managed_directory_name,
    validate_fantome,
)
from .skin_source import (
    MAXIMUM_MANIFEST_BYTES,
    DownloadCancelledError,
    SkinAsset,
    SkinManifest,
)

STATE_SCHEMA_VERSION = 1
TRANSACTION_DIRECTORY_PREFIX = ".lsmvn-transaction-"
DEFAULT_MAX_WORKERS = 4
MAX_WORKERS = 16
DEFAULT_MAX_TOTAL_UNCOMPRESSED_BYTES = 16 * 1024 * 1024 * 1024
DEFAULT_FREE_SPACE_RESERVE_BYTES = 256 * 1024 * 1024
_SHA_PATTERN = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")


class SyncError(RuntimeError):
    """Base error for a skin synchronization operation."""


class SyncCancelled(SyncError):
    """The operation was cancelled before the live commit began."""


class ManagedStateError(SyncError):
    """The managed-state file or recovery journal is invalid."""


class ManagedCollisionError(SyncError):
    """A desired managed path is occupied by content we do not own."""


class TransactionError(SyncError):
    """A live commit or rollback failed."""


class SyncMutationBlocked(SyncError):
    """Live managed content cannot be changed while process state is unsafe."""


class CancelSignal(Protocol):
    def is_set(self) -> bool:
        """Return whether cancellation was requested."""


class DiskUsageResult(Protocol):
    free: int


DiskUsageGetter = Callable[[Path], DiskUsageResult]
_DEFAULT_DISK_USAGE = cast(DiskUsageGetter, shutil.disk_usage)


DownloadProgress = Callable[[int, int], None]


class SkinSource(Protocol):
    def download(
        self,
        asset: SkinAsset,
        target: Path,
        *,
        cancel_event: CancelSignal | None = None,
        progress: DownloadProgress | None = None,
    ) -> Path:
        """Download *asset* to *target* and return the resulting path."""


@dataclass(frozen=True, slots=True)
class ManagedEntry:
    champion: str
    name: str
    source_path: str
    source_sha: str
    size: int
    directory: str
    content_sha256: str = ""

    @classmethod
    def from_json(cls, value: object) -> ManagedEntry:
        if not isinstance(value, Mapping):
            raise ManagedStateError("Managed entry must be a JSON object")
        try:
            entry = cls(
                champion=_required_string(value, "champion"),
                name=_required_string(value, "name"),
                source_path=_required_string(value, "source_path"),
                source_sha=_required_string(value, "source_sha").lower(),
                size=_required_int(value, "size"),
                directory=_required_string(value, "directory"),
                content_sha256=_optional_string(value, "content_sha256").lower(),
            )
        except (TypeError, ValueError) as error:
            raise ManagedStateError("Managed entry has invalid fields") from error
        expected_directory = managed_directory_name(
            entry.champion,
            entry.name,
            entry.source_path,
        )
        if entry.directory != expected_directory:
            raise ManagedStateError("Managed entry directory does not match its identity")
        if entry.size <= 0 or not _SHA_PATTERN.fullmatch(entry.source_sha):
            raise ManagedStateError("Managed entry has invalid artifact metadata")
        if entry.content_sha256 and not _SHA256_PATTERN.fullmatch(entry.content_sha256):
            raise ManagedStateError("Managed entry has invalid installed-content digest")
        return entry


@dataclass(frozen=True, slots=True)
class ManagedState:
    schema_version: int
    transaction_id: str
    source_commit: str
    patch: str | None
    entries: tuple[ManagedEntry, ...]

    @classmethod
    def empty(cls) -> ManagedState:
        return cls(
            schema_version=STATE_SCHEMA_VERSION,
            transaction_id="",
            source_commit="",
            patch=None,
            entries=(),
        )

    @classmethod
    def from_json(cls, value: object) -> ManagedState:
        if not isinstance(value, Mapping):
            raise ManagedStateError("Managed state must be a JSON object")
        if value.get("schema_version") != STATE_SCHEMA_VERSION:
            raise ManagedStateError("Managed state has an unsupported schema version")
        raw_entries = value.get("entries")
        if not isinstance(raw_entries, list):
            raise ManagedStateError("Managed state entries must be a list")
        patch_value = value.get("patch")
        if patch_value is not None and not isinstance(patch_value, str):
            raise ManagedStateError("Managed state patch must be a string or null")
        state = cls(
            schema_version=STATE_SCHEMA_VERSION,
            transaction_id=_optional_string(value, "transaction_id"),
            source_commit=_optional_string(value, "source_commit"),
            patch=patch_value,
            entries=tuple(ManagedEntry.from_json(entry) for entry in raw_entries),
        )
        _validate_state_uniqueness(state.entries)
        return state

    def to_json(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "transaction_id": self.transaction_id,
            "source_commit": self.source_commit,
            "patch": self.patch,
            "entries": [asdict(entry) for entry in self.entries],
        }


@dataclass(frozen=True, slots=True)
class SyncProgress:
    phase: str
    completed: int
    total: int
    asset_path: str | None = None
    bytes_completed: int | None = None
    bytes_total: int | None = None


ProgressCallback = Callable[[SyncProgress], None]


@dataclass(frozen=True, slots=True)
class SyncResult:
    commit: str
    patch: str | None
    installed: int
    downloaded: int
    reused: int
    removed: int


@dataclass(frozen=True, slots=True)
class _AssetPlan:
    asset: SkinAsset
    entry: ManagedEntry


@dataclass(frozen=True, slots=True)
class _Journal:
    transaction_id: str
    transaction_root: Path
    previous_directories: frozenset[str]
    desired_directories: frozenset[str]
    existing_before: frozenset[str]

    def to_json(self) -> dict[str, object]:
        return {
            "schema_version": STATE_SCHEMA_VERSION,
            "transaction_id": self.transaction_id,
            "transaction_root": str(self.transaction_root),
            "previous_directories": sorted(self.previous_directories),
            "desired_directories": sorted(self.desired_directories),
            "existing_before": sorted(self.existing_before),
        }


class _CombinedCancellation:
    def __init__(self, external: CancelSignal | None) -> None:
        self._external = external
        self._internal = threading.Event()

    def is_set(self) -> bool:
        return self._internal.is_set() or (self._external is not None and self._external.is_set())

    def cancel(self) -> None:
        self._internal.set()


class _ProgressReporter:
    def __init__(self, callback: ProgressCallback | None, total: int) -> None:
        self._callback = callback
        self._total = total
        self._lock = threading.Lock()

    def emit(
        self,
        phase: str,
        completed: int,
        *,
        asset_path: str | None = None,
        bytes_completed: int | None = None,
        bytes_total: int | None = None,
    ) -> None:
        if self._callback is None:
            return
        event = SyncProgress(
            phase=phase,
            completed=completed,
            total=self._total,
            asset_path=asset_path,
            bytes_completed=bytes_completed,
            bytes_total=bytes_total,
        )
        # Sources may report download bytes from several worker threads.  A UI
        # callback should never be invoked concurrently.
        with self._lock:
            try:
                self._callback(event)
            except Exception:
                # Progress is observational; a broken UI callback cannot be
                # allowed to invalidate an otherwise safe transaction.
                return

    def download_callback(self, asset: SkinAsset) -> DownloadProgress:
        def report(completed: int, total: int) -> None:
            self.emit(
                "downloading",
                0,
                asset_path=asset.path,
                bytes_completed=completed,
                bytes_total=total,
            )

        return report


class SkinSyncService:
    """Prepare a complete managed skin set, then commit only owned folders."""

    def __init__(
        self,
        installed_dir: Path,
        state_path: Path,
        *,
        cache_dir: Path | None = None,
        max_workers: int = DEFAULT_MAX_WORKERS,
        max_asset_compressed_bytes: int = DEFAULT_MAX_COMPRESSED_BYTES,
        max_total_compressed_bytes: int = MAXIMUM_MANIFEST_BYTES,
        max_asset_uncompressed_bytes: int = DEFAULT_MAX_UNCOMPRESSED_BYTES,
        max_total_uncompressed_bytes: int = DEFAULT_MAX_TOTAL_UNCOMPRESSED_BYTES,
        free_space_reserve_bytes: int = DEFAULT_FREE_SPACE_RESERVE_BYTES,
        disk_usage: DiskUsageGetter = _DEFAULT_DISK_USAGE,
        manager_is_running: Callable[[], bool] | None = None,
    ) -> None:
        if not 1 <= max_workers <= MAX_WORKERS:
            raise ValueError(f"max_workers must be between 1 and {MAX_WORKERS}")
        if (
            min(
                max_asset_compressed_bytes,
                max_total_compressed_bytes,
                max_asset_uncompressed_bytes,
                max_total_uncompressed_bytes,
            )
            < 1
        ):
            raise ValueError("Synchronization resource limits must be positive")
        if free_space_reserve_bytes < 0:
            raise ValueError("free_space_reserve_bytes cannot be negative")
        self.installed_dir = Path(installed_dir).resolve()
        self.state_path = Path(state_path).resolve()
        self.cache_dir = (
            Path(cache_dir).resolve()
            if cache_dir is not None
            else self.state_path.parent / "skin-cache"
        )
        self.max_workers = max_workers
        self.max_asset_compressed_bytes = max_asset_compressed_bytes
        self.max_total_compressed_bytes = max_total_compressed_bytes
        self.max_asset_uncompressed_bytes = max_asset_uncompressed_bytes
        self.max_total_uncompressed_bytes = max_total_uncompressed_bytes
        self.free_space_reserve_bytes = free_space_reserve_bytes
        self.disk_usage = disk_usage
        self.manager_is_running = manager_is_running or (lambda: False)
        self.journal_path = self.state_path.parent / f".{self.state_path.name}.transaction.json"
        self._lock = threading.Lock()

    def sync(
        self,
        source: SkinSource,
        manifest: SkinManifest,
        *,
        cancel_event: threading.Event | None = None,
        progress: object | None = None,
    ) -> SyncResult:
        if progress is not None and not callable(progress):
            raise TypeError("progress must be callable")
        progress_callback = cast(ProgressCallback | None, progress)
        if not self._lock.acquire(blocking=False):
            raise SyncError("A skin synchronization is already in progress")
        try:
            return self._sync_locked(source, manifest, cancel_event, progress_callback)
        finally:
            self._lock.release()

    def recover(self) -> bool:
        """Recover a prior interrupted live commit, if one exists."""

        if not self._lock.acquire(blocking=False):
            raise SyncError("A skin synchronization is already in progress")
        try:
            return self._recover_interrupted_transaction()
        finally:
            self._lock.release()

    def load_state(self) -> ManagedState:
        if not self.state_path.exists():
            return ManagedState.empty()
        try:
            raw = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ManagedStateError(
                "Managed state is unreadable; refusing to alter live mods"
            ) from error
        return ManagedState.from_json(raw)

    def _sync_locked(
        self,
        source: SkinSource,
        manifest: SkinManifest,
        cancel_event: CancelSignal | None,
        progress: ProgressCallback | None,
    ) -> SyncResult:
        self._ensure_manager_stopped()
        self.installed_dir.mkdir(parents=True, exist_ok=True)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self._recover_interrupted_transaction()
        previous = self.load_state()
        plans = _build_plans(
            manifest,
            max_asset_compressed_bytes=self.max_asset_compressed_bytes,
            max_total_compressed_bytes=self.max_total_compressed_bytes,
        )
        reporter = _ProgressReporter(progress, len(plans))
        cancellation = _CombinedCancellation(cancel_event)
        _raise_if_cancelled(cancellation)
        reporter.emit("preparing", 0)

        previous_by_path = {entry.source_path: entry for entry in previous.entries}
        verified_live: dict[str, ExtractedModFingerprint] = {}
        if (
            previous.source_commit == manifest.commit
            and previous.patch == manifest.patch
            and len(previous.entries) == len(plans)
        ):
            all_valid = True
            total_uncompressed = 0
            for completed, plan in enumerate(plans, start=1):
                _raise_if_cancelled(cancellation)
                fingerprint = self._trusted_live_fingerprint(
                    plan,
                    previous_by_path.get(plan.entry.source_path),
                )
                if fingerprint is None:
                    all_valid = False
                    break
                verified_live[plan.entry.source_path] = fingerprint
                total_uncompressed += fingerprint.uncompressed_bytes
                if total_uncompressed > self.max_total_uncompressed_bytes:
                    raise SyncError("Managed skins exceed the aggregate uncompressed-size limit")
                reporter.emit("reusing", completed, asset_path=plan.asset.path)
            if all_valid:
                reporter.emit("complete", len(plans))
                return SyncResult(
                    commit=manifest.commit,
                    patch=manifest.patch,
                    installed=len(plans),
                    downloaded=0,
                    reused=len(plans),
                    removed=0,
                )

        transaction_root = Path(
            tempfile.mkdtemp(
                prefix=TRANSACTION_DIRECTORY_PREFIX,
                dir=self.installed_dir.parent,
            )
        )
        staged_root = transaction_root / "staged"
        staged_root.mkdir()
        downloaded = 0
        reused = 0
        committed = False
        try:
            reusable: dict[str, ExtractedModFingerprint] = {}
            needs_archive: list[_AssetPlan] = []
            for plan in plans:
                _raise_if_cancelled(cancellation)
                old = previous_by_path.get(plan.entry.source_path)
                fingerprint = verified_live.get(plan.entry.source_path)
                if fingerprint is None:
                    fingerprint = self._trusted_live_fingerprint(plan, old)
                if fingerprint is None:
                    needs_archive.append(plan)
                else:
                    reusable[plan.entry.source_path] = fingerprint

            downloaded = self._ensure_cached(
                source,
                needs_archive,
                cancellation,
                reporter,
            )
            total_uncompressed = sum(
                fingerprint.uncompressed_bytes for fingerprint in reusable.values()
            )
            for plan in needs_archive:
                validated = validate_fantome(
                    self._cache_path(plan.asset),
                    expected_size=plan.asset.size,
                    expected_sha=plan.asset.sha,
                    max_compressed_bytes=self.max_asset_compressed_bytes,
                    max_uncompressed_bytes=self.max_asset_uncompressed_bytes,
                )
                total_uncompressed += validated.uncompressed_bytes
                if total_uncompressed > self.max_total_uncompressed_bytes:
                    raise SyncError("Manifest exceeds the aggregate uncompressed-size limit")
            self._require_free_space(
                self.installed_dir.parent,
                total_uncompressed,
                "transaction staging",
            )

            desired_entries: list[ManagedEntry] = []
            for completed, plan in enumerate(plans, start=1):
                _raise_if_cancelled(cancellation)
                staged = staged_root / plan.entry.directory
                expected_live = reusable.get(plan.entry.source_path)
                if expected_live is not None:
                    live = self.installed_dir / plan.entry.directory
                    shutil.copytree(live, staged, symlinks=True)
                    fingerprint = inspect_extracted_mod(
                        staged,
                        max_uncompressed_bytes=self.max_asset_uncompressed_bytes,
                    )
                    if fingerprint is None or fingerprint.sha256 != expected_live.sha256:
                        raise SyncError(
                            f"Managed live content changed during staging: {plan.entry.directory}"
                        )
                    reused += 1
                    reporter.emit("reusing", completed, asset_path=plan.asset.path)
                else:
                    try:
                        extract_fantome(
                            self._cache_path(plan.asset),
                            staged,
                            expected_size=plan.asset.size,
                            expected_sha=plan.asset.sha,
                            cancel_event=cancellation,
                            max_compressed_bytes=self.max_asset_compressed_bytes,
                            max_uncompressed_bytes=self.max_asset_uncompressed_bytes,
                        )
                    except ExtractionCancelled as error:
                        raise SyncCancelled("Skin synchronization was cancelled") from error
                    fingerprint = inspect_extracted_mod(
                        staged,
                        max_uncompressed_bytes=self.max_asset_uncompressed_bytes,
                    )
                    if fingerprint is None:
                        raise SyncError(
                            f"Extracted managed content failed fingerprinting: {plan.asset.path}"
                        )
                    reporter.emit("extracting", completed, asset_path=plan.asset.path)
                desired_entries.append(replace(plan.entry, content_sha256=fingerprint.sha256))

            _raise_if_cancelled(cancellation)
            reporter.emit("committing", len(plans))
            transaction_id = uuid4().hex
            desired_state = ManagedState(
                schema_version=STATE_SCHEMA_VERSION,
                transaction_id=transaction_id,
                source_commit=manifest.commit,
                patch=manifest.patch,
                entries=tuple(desired_entries),
            )
            removed = len(
                {entry.directory for entry in previous.entries}
                - {entry.directory for entry in desired_state.entries}
            )
            self._commit(
                transaction_root,
                staged_root,
                previous,
                desired_state,
            )
            committed = True
            reporter.emit("complete", len(plans))
            return SyncResult(
                commit=manifest.commit,
                patch=manifest.patch,
                installed=len(plans),
                downloaded=downloaded,
                reused=reused,
                removed=removed,
            )
        except DownloadCancelledError as error:
            raise SyncCancelled("Skin synchronization was cancelled") from error
        finally:
            if not committed and not self.journal_path.exists():
                shutil.rmtree(transaction_root, ignore_errors=True)

    def _ensure_cached(
        self,
        source: SkinSource,
        plans: Sequence[_AssetPlan],
        cancellation: _CombinedCancellation,
        reporter: _ProgressReporter,
    ) -> int:
        by_sha: dict[str, _AssetPlan] = {}
        for plan in plans:
            existing = by_sha.get(plan.asset.sha)
            if existing is not None and existing.asset.size != plan.asset.size:
                raise SyncError("Manifest reuses an artifact SHA with different sizes")
            by_sha.setdefault(plan.asset.sha, plan)

        pending: list[_AssetPlan] = []
        for plan in by_sha.values():
            cached = self._cache_path(plan.asset)
            try:
                validate_fantome(
                    cached,
                    expected_size=plan.asset.size,
                    expected_sha=plan.asset.sha,
                    max_compressed_bytes=self.max_asset_compressed_bytes,
                    max_uncompressed_bytes=self.max_asset_uncompressed_bytes,
                )
            except FantomeError:
                cached.unlink(missing_ok=True)
                pending.append(plan)
            except OSError:
                cached.unlink(missing_ok=True)
                pending.append(plan)

        if not pending:
            return 0
        self._require_free_space(
            self.cache_dir,
            sum(plan.asset.size for plan in pending),
            "skin downloads",
        )

        futures: dict[Future[Path], _AssetPlan] = {}
        executor = ThreadPoolExecutor(
            max_workers=min(self.max_workers, len(pending)),
            thread_name_prefix="skin-download",
        )
        try:
            for plan in pending:
                future = executor.submit(
                    self._download_to_cache,
                    source,
                    plan.asset,
                    cancellation,
                    reporter,
                )
                futures[future] = plan
            for completed, future in enumerate(as_completed(futures), start=1):
                _raise_if_cancelled(cancellation)
                plan = futures[future]
                future.result()
                reporter.emit("downloaded", completed, asset_path=plan.asset.path)
        except BaseException:
            cancellation.cancel()
            for future in futures:
                future.cancel()
            raise
        finally:
            executor.shutdown(wait=True, cancel_futures=True)
        return len(pending)

    def _download_to_cache(
        self,
        source: SkinSource,
        asset: SkinAsset,
        cancellation: _CombinedCancellation,
        reporter: _ProgressReporter,
    ) -> Path:
        _raise_if_cancelled(cancellation)
        temporary = self.cache_dir / f".{asset.sha}.{uuid4().hex}.part"
        try:
            result = source.download(
                asset,
                temporary,
                cancel_event=cancellation,
                progress=reporter.download_callback(asset),
            )
            downloaded_path = Path(result)
            if downloaded_path.resolve() != temporary.resolve():
                raise SyncError("Skin source returned a path other than its requested target")
            _raise_if_cancelled(cancellation)
            validate_fantome(
                downloaded_path,
                expected_size=asset.size,
                expected_sha=asset.sha,
                max_compressed_bytes=self.max_asset_compressed_bytes,
                max_uncompressed_bytes=self.max_asset_uncompressed_bytes,
            )
            cached = self._cache_path(asset)
            os.replace(downloaded_path, cached)
            return cached
        finally:
            temporary.unlink(missing_ok=True)

    def _cache_path(self, asset: SkinAsset) -> Path:
        return self.cache_dir / f"{asset.sha.lower()}.fantome"

    def _trusted_live_fingerprint(
        self,
        plan: _AssetPlan,
        previous: ManagedEntry | None,
    ) -> ExtractedModFingerprint | None:
        if (
            previous is None
            or not previous.content_sha256
            or not _same_artifact(previous, plan.entry)
        ):
            return None
        fingerprint = inspect_extracted_mod(
            self.installed_dir / plan.entry.directory,
            max_uncompressed_bytes=self.max_asset_uncompressed_bytes,
        )
        if fingerprint is None or fingerprint.sha256 != previous.content_sha256:
            return None
        return fingerprint

    def _require_free_space(self, path: Path, content_bytes: int, purpose: str) -> None:
        required = content_bytes + self.free_space_reserve_bytes
        try:
            usage = self.disk_usage(path)
            free = usage.free
        except (OSError, AttributeError) as error:
            raise SyncError(f"Could not determine free space for {purpose}") from error
        if isinstance(free, bool) or not isinstance(free, int) or free < 0:
            raise SyncError(f"Disk usage returned invalid free space for {purpose}")
        if free < required:
            raise SyncError(
                f"Insufficient free space for {purpose}: need {required} bytes, have {free}"
            )

    def _commit(
        self,
        transaction_root: Path,
        staged_root: Path,
        previous: ManagedState,
        desired: ManagedState,
    ) -> None:
        previous_directories = frozenset(entry.directory for entry in previous.entries)
        desired_directories = frozenset(entry.directory for entry in desired.entries)
        all_directories = previous_directories | desired_directories

        for directory in desired_directories:
            live = self.installed_dir / directory
            if (live.exists() or live.is_symlink()) and directory not in previous_directories:
                raise ManagedCollisionError(
                    f"Refusing to replace unowned CSLOL content: {directory}"
                )

        existing_before = frozenset(
            directory
            for directory in all_directories
            if (self.installed_dir / directory).exists()
            or (self.installed_dir / directory).is_symlink()
        )
        journal = _Journal(
            transaction_id=desired.transaction_id,
            transaction_root=transaction_root,
            previous_directories=previous_directories,
            desired_directories=desired_directories,
            existing_before=existing_before,
        )
        # Staging may run for minutes. Recheck immediately before creating the
        # recovery journal and moving any live CSLOL directories.
        self._ensure_manager_stopped()
        atomic_write_json(self.journal_path, journal.to_json())

        backup_root = transaction_root / "backup"
        backup_root.mkdir()
        state_committed = False
        try:
            for directory in sorted(existing_before):
                os.replace(
                    self.installed_dir / directory,
                    backup_root / directory,
                )
            for directory in sorted(desired_directories):
                staged = staged_root / directory
                if not staged.is_dir() or staged.is_symlink():
                    raise TransactionError(f"Staged managed directory is missing: {directory}")
                os.replace(staged, self.installed_dir / directory)

            atomic_write_json(self.state_path, desired.to_json())
            state_committed = True
        except BaseException as error:
            try:
                self._rollback(journal)
            except BaseException as rollback_error:
                raise TransactionError(
                    "Skin commit failed and rollback could not be completed; "
                    "the recovery journal was preserved"
                ) from rollback_error
            raise TransactionError("Skin commit failed; live mods were restored") from error
        finally:
            if state_committed:
                self._cleanup_transaction(journal)

    def _recover_interrupted_transaction(self) -> bool:
        if not self.journal_path.exists():
            return False
        self._ensure_manager_stopped()
        journal = self._load_journal()
        current_transaction_id = self._current_transaction_id()
        if current_transaction_id == journal.transaction_id:
            self._cleanup_transaction(journal)
            return True
        self._rollback(journal)
        return True

    def _ensure_manager_stopped(self) -> None:
        try:
            running = self.manager_is_running()
        except Exception as error:
            raise SyncMutationBlocked(
                "Could not safely verify that CSLOL Manager is stopped"
            ) from error
        if running:
            raise SyncMutationBlocked(
                "CSLOL Manager is running; live managed skins were not changed"
            )

    def _rollback(self, journal: _Journal) -> None:
        backup_root = journal.transaction_root / "backup"
        try:
            for directory in sorted(journal.desired_directories):
                live = self.installed_dir / directory
                backup = backup_root / directory
                if backup.exists() or directory not in journal.existing_before:
                    _remove_path(live)

            for directory in sorted(journal.existing_before):
                backup = backup_root / directory
                if not (backup.exists() or backup.is_symlink()):
                    # The move may not have happened before interruption; in
                    # that case the original live path must be left alone.
                    continue
                live = self.installed_dir / directory
                _remove_path(live)
                os.replace(backup, live)
        except BaseException as error:
            raise TransactionError("Could not restore managed directories") from error
        self._cleanup_transaction(journal)

    def _cleanup_transaction(self, journal: _Journal) -> None:
        try:
            shutil.rmtree(journal.transaction_root)
        except FileNotFoundError:
            pass
        except OSError:
            # Keep the journal so cleanup is retried on the next run.  A
            # matching transaction ID makes recovery recognize this as a
            # committed transaction rather than rolling it back.
            return
        try:
            self.journal_path.unlink(missing_ok=True)
        except OSError:
            # The committed state transaction ID makes a leftover journal
            # safe; cleanup will be retried on the next start.
            return

    def _load_journal(self) -> _Journal:
        try:
            raw = json.loads(self.journal_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ManagedStateError("Transaction journal is unreadable") from error
        if not isinstance(raw, Mapping) or raw.get("schema_version") != STATE_SCHEMA_VERSION:
            raise ManagedStateError("Transaction journal has an unsupported schema")
        transaction_id = _required_string(raw, "transaction_id")
        root_value = _required_string(raw, "transaction_root")
        transaction_root = Path(root_value).resolve()
        if (
            transaction_root.parent != self.installed_dir.parent
            or not transaction_root.name.startswith(TRANSACTION_DIRECTORY_PREFIX)
        ):
            raise ManagedStateError("Transaction journal references an unsafe directory")
        previous = _managed_directory_set(raw.get("previous_directories"))
        desired = _managed_directory_set(raw.get("desired_directories"))
        existing = _managed_directory_set(raw.get("existing_before"))
        if not existing.issubset(previous | desired):
            raise ManagedStateError("Transaction journal has inconsistent ownership data")
        return _Journal(
            transaction_id=transaction_id,
            transaction_root=transaction_root,
            previous_directories=previous,
            desired_directories=desired,
            existing_before=existing,
        )

    def _current_transaction_id(self) -> str | None:
        try:
            raw = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (FileNotFoundError, OSError, UnicodeDecodeError, json.JSONDecodeError):
            return None
        if isinstance(raw, Mapping):
            value = raw.get("transaction_id")
            if isinstance(value, str):
                return value
        return None


def _build_plans(
    manifest: SkinManifest,
    *,
    max_asset_compressed_bytes: int,
    max_total_compressed_bytes: int,
) -> tuple[_AssetPlan, ...]:
    if not manifest.assets:
        raise SyncError("Refusing to replace managed skins from an empty manifest")
    plans: list[_AssetPlan] = []
    seen_paths: set[str] = set()
    seen_directories: set[str] = set()
    seen_sha_sizes: dict[str, int] = {}
    total_compressed = 0
    for asset in sorted(manifest.assets, key=lambda item: item.path.casefold()):
        if not asset.champion.strip() or not asset.name.strip() or not asset.path.strip():
            raise SyncError("Manifest contains an asset with missing identity fields")
        path_key = asset.path.casefold()
        if path_key in seen_paths:
            raise SyncError(f"Manifest contains duplicate asset path: {asset.path}")
        seen_paths.add(path_key)
        digest = asset.sha.lower()
        if (
            asset.size <= 0
            or asset.size > max_asset_compressed_bytes
            or not _SHA_PATTERN.fullmatch(digest)
        ):
            raise SyncError(f"Manifest contains invalid artifact metadata: {asset.path}")
        total_compressed += asset.size
        if total_compressed > max_total_compressed_bytes:
            raise SyncError("Manifest exceeds the aggregate compressed-size limit")
        old_size = seen_sha_sizes.setdefault(digest, asset.size)
        if old_size != asset.size:
            raise SyncError("Manifest reuses an artifact SHA with different sizes")
        directory = managed_directory_name(asset.champion, asset.name, asset.path)
        if directory.casefold() in seen_directories:
            raise SyncError(f"Manifest contains colliding managed paths: {asset.path}")
        seen_directories.add(directory.casefold())
        entry = ManagedEntry(
            champion=asset.champion,
            name=asset.name,
            source_path=asset.path,
            source_sha=digest,
            size=asset.size,
            directory=directory,
        )
        plans.append(_AssetPlan(asset=asset, entry=entry))
    return tuple(plans)


def _same_artifact(previous: ManagedEntry, planned: ManagedEntry) -> bool:
    return (
        previous.champion == planned.champion
        and previous.name == planned.name
        and previous.source_path == planned.source_path
        and previous.source_sha == planned.source_sha
        and previous.size == planned.size
        and previous.directory == planned.directory
    )


def _validate_state_uniqueness(entries: Sequence[ManagedEntry]) -> None:
    paths: set[str] = set()
    directories: set[str] = set()
    for entry in entries:
        path_key = entry.source_path.casefold()
        directory_key = entry.directory.casefold()
        if path_key in paths or directory_key in directories:
            raise ManagedStateError("Managed state contains duplicate entries")
        paths.add(path_key)
        directories.add(directory_key)


def _managed_directory_set(value: object) -> frozenset[str]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ManagedStateError("Transaction directory list is invalid")
    result = frozenset(value)
    if len(result) != len(value):
        raise ManagedStateError("Transaction directory list contains duplicates")
    for directory in result:
        if (
            not directory.startswith(MANAGED_DIRECTORY_PREFIX)
            or Path(directory).name != directory
            or directory in {".", ".."}
        ):
            raise ManagedStateError("Transaction journal contains an unsafe directory name")
    return result


def _required_string(value: Mapping[object, object], key: str) -> str:
    item = value.get(key)
    if not isinstance(item, str) or not item:
        raise ValueError(f"{key} must be a non-empty string")
    return item


def _optional_string(value: Mapping[object, object], key: str) -> str:
    item = value.get(key, "")
    if not isinstance(item, str):
        raise ManagedStateError(f"{key} must be a string")
    return item


def _required_int(value: Mapping[object, object], key: str) -> int:
    item = value.get(key)
    if isinstance(item, bool) or not isinstance(item, int):
        raise ValueError(f"{key} must be an integer")
    return item


def _raise_if_cancelled(cancel_event: CancelSignal) -> None:
    if cancel_event.is_set():
        raise SyncCancelled("Skin synchronization was cancelled")


def _remove_path(path: Path) -> None:
    if path.is_symlink() or path.is_file():
        path.unlink(missing_ok=True)
    elif path.is_dir():
        shutil.rmtree(path)


__all__ = [
    "ManagedCollisionError",
    "ManagedEntry",
    "ManagedState",
    "ManagedStateError",
    "SkinSyncService",
    "SyncCancelled",
    "SyncError",
    "SyncProgress",
    "SyncResult",
    "TransactionError",
]
