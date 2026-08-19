"""Explicit, process-gated removal of every skin from LTK Manager storage.

This service is deliberately separate from application uninstall.  LTK owns its
installation, settings, and logs; the user-triggered cleanup removes only the
documented mod library artifacts beneath LTK's resolved storage directory.
"""

from __future__ import annotations

import json
import os
import shutil
import stat
import threading
from collections.abc import Callable, Collection, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .atomic import atomic_write_json

LTK_LIBRARY_FILENAME = "library.json"
LTK_REPORTS_FILENAME = "wad-reports.json"
LTK_SKIN_DIRECTORIES = ("archives", "mods", "profiles")
MAX_LIBRARY_BYTES = 32 * 1024 * 1024

StorageResolver = Callable[[], Path]
RunningPredicate = Callable[[], bool]


class LtkSkinCleanupError(RuntimeError):
    """Base error for an LTK skin-library cleanup that cannot finish safely."""


class LtkSkinCleanupBusyError(LtkSkinCleanupError):
    """Another cleanup is already active in this service."""


class LtkSkinCleanupBlockedError(LtkSkinCleanupError):
    """LTK is running or its process state cannot be verified."""


@dataclass(frozen=True, slots=True)
class LtkSkinCleanupResult:
    storage_dir: Path
    library_mods: int
    archives: int
    metadata_directories: int
    profile_directories: int
    reports_removed: bool
    library_reset: bool

    @property
    def removed_items(self) -> int:
        return self.archives + self.metadata_directories + self.profile_directories


@dataclass(frozen=True, slots=True)
class LtkModRemovalResult:
    """Outcome of removing specific, individually identified LTK mods."""

    storage_dir: Path
    removed_ids: tuple[str, ...]
    archives_removed: int
    metadata_directories_removed: int
    reports_removed: int
    references_cleared: int

    @property
    def removed(self) -> int:
        return len(self.removed_ids)


class LtkSkinCleanupService:
    """Remove all LTK skin artifacts while preserving LTK itself and preferences."""

    def __init__(
        self,
        storage_resolver: StorageResolver,
        *,
        ltk_is_running: RunningPredicate,
        max_library_bytes: int = MAX_LIBRARY_BYTES,
    ) -> None:
        if max_library_bytes < 1:
            raise ValueError("max_library_bytes must be positive")
        self._storage_resolver = storage_resolver
        self._ltk_is_running = ltk_is_running
        self._max_library_bytes = max_library_bytes
        self._lock = threading.Lock()

    def remove_all(self) -> LtkSkinCleanupResult:
        """Synchronously remove the complete LTK skin library.

        The caller is responsible for obtaining the application's shared
        operation gate.  This method independently rejects concurrent calls and
        checks LTK's process state twice: before preflight and immediately before
        the first filesystem mutation.
        """

        if not self._lock.acquire(blocking=False):
            raise LtkSkinCleanupBusyError("An LTK skin cleanup is already in progress")
        try:
            return self._remove_all_locked()
        finally:
            self._lock.release()

    def remove_mods(self, mod_ids: Collection[str]) -> LtkModRemovalResult:
        """Remove specific mods from LTK's library, leaving every other mod intact.

        This mirrors the exact artifact set LTK itself maintains per mod, as
        observed from its own import/removal behaviour: the stored archive, the
        extracted metadata directory, the ``library.json`` entry, every profile
        and folder reference to that identifier, and its WAD report. The caller
        must have established that each identifier belongs to content this
        application queued; this method verifies only structure and safety.
        """

        identifiers = tuple(dict.fromkeys(mod_ids))
        for identifier in identifiers:
            if not _is_safe_mod_id(identifier):
                raise LtkSkinCleanupError(f"Unsafe LTK mod identifier: {identifier!r}")
        if not identifiers:
            return LtkModRemovalResult(_absolute_path(self._storage_resolver()), (), 0, 0, 0, 0)
        if not self._lock.acquire(blocking=False):
            raise LtkSkinCleanupBusyError("An LTK skin cleanup is already in progress")
        try:
            return self._remove_mods_locked(identifiers)
        finally:
            self._lock.release()

    def clear_enabled_mods(self) -> int:
        """Reset every profile's enabled selections, keeping the library intact.

        The managed baseline has nothing enabled, so this is the toggle half of
        a rebuild.  Mod membership, profile definitions, and folder structure are
        preserved; only the enabled/order/layer selections are emptied.
        """

        if not self._lock.acquire(blocking=False):
            raise LtkSkinCleanupBusyError("An LTK skin cleanup is already in progress")
        try:
            self._ensure_ltk_stopped()
            storage_dir = _absolute_path(self._storage_resolver())
            library_path = storage_dir / LTK_LIBRARY_FILENAME
            if not os.path.lexists(library_path):
                return 0
            _preflight_optional_file(library_path, "LTK library index")
            library, _mods = self._load_library(library_path)
            if library is None:
                return 0
            cleared, count = _library_without_selections(library)
            if count:
                self._ensure_ltk_stopped()
                atomic_write_json(library_path, cleared)
            return count
        finally:
            self._lock.release()

    def _remove_mods_locked(self, identifiers: tuple[str, ...]) -> LtkModRemovalResult:
        self._ensure_ltk_stopped()
        storage_dir = _absolute_path(self._storage_resolver())
        if not os.path.lexists(storage_dir):
            return LtkModRemovalResult(storage_dir, (), 0, 0, 0, 0)
        _require_safe_directory_path(storage_dir, "LTK storage directory")

        archives_dir = storage_dir / "archives"
        mods_dir = storage_dir / "mods"
        library_path = storage_dir / LTK_LIBRARY_FILENAME
        reports_path = storage_dir / LTK_REPORTS_FILENAME
        _preflight_optional_file(library_path, "LTK library index")
        _preflight_optional_file(reports_path, "LTK WAD report cache")

        # Preflight every per-mod target before deleting any of them so a
        # reparse point or unexpected object aborts with nothing removed.
        targets: list[tuple[str, Path | None, Path | None]] = []
        for identifier in identifiers:
            archive_path = archives_dir / f"{identifier}.fantome"
            metadata_path = mods_dir / identifier
            archive: Path | None = None
            metadata: Path | None = None
            if os.path.lexists(archive_path):
                _preflight_optional_file(archive_path, "LTK mod archive")
                archive = archive_path
            if os.path.lexists(metadata_path):
                _preflight_optional_directory(metadata_path, "LTK mod metadata directory")
                _preflight_tree(metadata_path, "LTK mod metadata directory")
                metadata = metadata_path
            targets.append((identifier, archive, metadata))

        library, _mods = self._load_library(library_path)
        self._ensure_ltk_stopped()

        archives_removed = 0
        metadata_removed = 0
        for _identifier, archive, metadata in targets:
            if archive is not None:
                archive.unlink()
                archives_removed += 1
            if metadata is not None:
                shutil.rmtree(metadata)
                if os.path.lexists(metadata):
                    raise LtkSkinCleanupError(f"LTK mod metadata still exists: {metadata}")
                metadata_removed += 1

        references_cleared = 0
        if library is not None:
            pruned, references_cleared = _library_without_mods(library, set(identifiers))
            atomic_write_json(library_path, pruned)

        reports_removed = self._remove_reports(reports_path, set(identifiers))
        return LtkModRemovalResult(
            storage_dir=storage_dir,
            removed_ids=identifiers,
            archives_removed=archives_removed,
            metadata_directories_removed=metadata_removed,
            reports_removed=reports_removed,
            references_cleared=references_cleared,
        )

    def _remove_reports(self, reports_path: Path, identifiers: set[str]) -> int:
        if not os.path.lexists(reports_path):
            return 0
        try:
            before = reports_path.stat(follow_symlinks=False)
            if before.st_size > self._max_library_bytes:
                return 0
            raw = json.loads(reports_path.read_bytes().decode("utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            return 0
        if not isinstance(raw, dict):
            return 0
        reports = raw.get("reports")
        if not isinstance(reports, dict):
            return 0
        removed = 0
        for identifier in identifiers:
            if reports.pop(identifier, None) is not None:
                removed += 1
        if removed:
            atomic_write_json(reports_path, raw)
        return removed

    def _remove_all_locked(self) -> LtkSkinCleanupResult:
        self._ensure_ltk_stopped()
        storage_dir = _absolute_path(self._storage_resolver())
        if not os.path.lexists(storage_dir):
            return LtkSkinCleanupResult(storage_dir, 0, 0, 0, 0, False, False)
        _require_safe_directory_path(storage_dir, "LTK storage directory")

        archives_dir, mods_dir, profiles_dir = (storage_dir / name for name in LTK_SKIN_DIRECTORIES)
        reports_path = storage_dir / LTK_REPORTS_FILENAME
        library_path = storage_dir / LTK_LIBRARY_FILENAME

        # Validate every target before deleting any target.  A reparse point or
        # unexpected filesystem object aborts the entire operation.
        for path, label in (
            (archives_dir, "LTK archives directory"),
            (mods_dir, "LTK metadata directory"),
            (profiles_dir, "LTK profiles directory"),
        ):
            _preflight_optional_directory(path, label)
            _preflight_tree(path, label)
        _preflight_optional_file(reports_path, "LTK WAD report cache")
        _preflight_optional_file(library_path, "LTK library index")

        library, library_mods = self._load_library(library_path)
        archives = _count_immediate_files(archives_dir)
        metadata_directories = _count_immediate_directories(mods_dir)
        profile_directories = _count_immediate_directories(profiles_dir)

        self._ensure_ltk_stopped()
        for path in (archives_dir, mods_dir, profiles_dir):
            if os.path.lexists(path):
                shutil.rmtree(path)
                if os.path.lexists(path):
                    raise LtkSkinCleanupError(f"LTK skin directory still exists: {path}")

        reports_removed = False
        if os.path.lexists(reports_path):
            reports_path.unlink()
            reports_removed = True

        library_reset = False
        if library is not None:
            sanitized = _sanitized_library(library)
            atomic_write_json(library_path, sanitized)
            library_reset = True
        elif os.path.lexists(library_path):
            # LTK itself treats malformed library JSON as recoverable and
            # starts from a default index.  Removing the exact, preflighted
            # file gives the same clean recovery without leaving stale refs.
            library_path.unlink()
            library_reset = True

        return LtkSkinCleanupResult(
            storage_dir=storage_dir,
            library_mods=library_mods,
            archives=archives,
            metadata_directories=metadata_directories,
            profile_directories=profile_directories,
            reports_removed=reports_removed,
            library_reset=library_reset,
        )

    def _load_library(self, path: Path) -> tuple[dict[str, Any] | None, int]:
        if not os.path.lexists(path):
            return None, 0
        try:
            before = path.stat(follow_symlinks=False)
            if before.st_size > self._max_library_bytes:
                return None, 0
            encoded = path.read_bytes()
            after = path.stat(follow_symlinks=False)
        except OSError as error:
            raise LtkSkinCleanupError("Could not read the LTK library index") from error
        if len(encoded) > self._max_library_bytes or not _same_file_snapshot(before, after):
            raise LtkSkinCleanupError("LTK library index changed during cleanup preflight")
        try:
            raw = json.loads(encoded.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return None, 0
        if not isinstance(raw, dict):
            return None, 0
        mods = raw.get("mods")
        if not isinstance(mods, list):
            return None, 0
        return raw, len(mods)

    def _ensure_ltk_stopped(self) -> None:
        try:
            running = self._ltk_is_running()
        except Exception as error:
            raise LtkSkinCleanupBlockedError(
                "Could not verify whether LTK Manager is running"
            ) from error
        if not isinstance(running, bool):
            raise LtkSkinCleanupBlockedError("Could not verify whether LTK Manager is running")
        if running:
            raise LtkSkinCleanupBlockedError(
                "Close LTK Manager and its patcher before removing all LTK skins"
            )


def _is_safe_mod_id(value: object) -> bool:
    """Accept only the opaque identifier shape LTK uses for its own mods."""

    if not isinstance(value, str) or not 1 <= len(value) <= 128:
        return False
    return all(character.isalnum() or character in "-_" for character in value)


def _library_without_mods(
    library: Mapping[str, Any],
    identifiers: set[str],
) -> tuple[dict[str, Any], int]:
    """Drop specific mods and every reference to them, keeping all other state."""

    pruned = dict(library)
    references = 0

    mods = pruned.get("mods")
    if isinstance(mods, list):
        kept = [
            mod for mod in mods if not (isinstance(mod, Mapping) and mod.get("id") in identifiers)
        ]
        references += len(mods) - len(kept)
        pruned["mods"] = kept

    profiles = pruned.get("profiles")
    if isinstance(profiles, list):
        clean_profiles: list[Any] = []
        for profile in profiles:
            if not isinstance(profile, Mapping):
                clean_profiles.append(profile)
                continue
            clean = dict(profile)
            for key in ("enabledMods", "modOrder"):
                values = clean.get(key)
                if isinstance(values, list):
                    remaining = [value for value in values if value not in identifiers]
                    references += len(values) - len(remaining)
                    clean[key] = remaining
            layers = clean.get("layerStates")
            if isinstance(layers, Mapping):
                remaining_layers = {
                    key: value for key, value in layers.items() if key not in identifiers
                }
                references += len(layers) - len(remaining_layers)
                clean["layerStates"] = remaining_layers
            clean_profiles.append(clean)
        pruned["profiles"] = clean_profiles

    folders = pruned.get("folders")
    if isinstance(folders, list):
        clean_folders: list[Any] = []
        for folder in folders:
            if not isinstance(folder, Mapping):
                clean_folders.append(folder)
                continue
            clean = dict(folder)
            values = clean.get("modIds")
            if isinstance(values, list):
                remaining = [value for value in values if value not in identifiers]
                references += len(values) - len(remaining)
                clean["modIds"] = remaining
            clean_folders.append(clean)
        pruned["folders"] = clean_folders

    return pruned, references


def _library_without_selections(library: Mapping[str, Any]) -> tuple[dict[str, Any], int]:
    """Empty every profile selection list, leaving mods and folders in place."""

    cleared = dict(library)
    removed = 0
    profiles = cleared.get("profiles")
    if not isinstance(profiles, list):
        return cleared, 0
    clean_profiles: list[Any] = []
    for profile in profiles:
        if not isinstance(profile, Mapping):
            clean_profiles.append(profile)
            continue
        clean = dict(profile)
        for key in ("enabledMods", "modOrder"):
            values = clean.get(key)
            if isinstance(values, list) and values:
                removed += len(values)
                clean[key] = []
        layers = clean.get("layerStates")
        if isinstance(layers, Mapping) and layers:
            removed += len(layers)
            clean["layerStates"] = {}
        clean_profiles.append(clean)
    cleared["profiles"] = clean_profiles
    return cleared, removed


def _sanitized_library(library: Mapping[str, Any]) -> dict[str, Any]:
    """Preserve profiles/folders while removing every reference to a mod."""

    sanitized = dict(library)
    sanitized["mods"] = []

    profiles = sanitized.get("profiles")
    if isinstance(profiles, list):
        clean_profiles: list[Any] = []
        for profile in profiles:
            if not isinstance(profile, Mapping):
                clean_profiles.append(profile)
                continue
            clean = dict(profile)
            clean["enabledMods"] = []
            clean["modOrder"] = []
            clean["layerStates"] = {}
            clean_profiles.append(clean)
        sanitized["profiles"] = clean_profiles

    folders = sanitized.get("folders")
    if isinstance(folders, list):
        clean_folders: list[Any] = []
        for folder in folders:
            if not isinstance(folder, Mapping):
                clean_folders.append(folder)
                continue
            clean = dict(folder)
            clean["modIds"] = []
            clean_folders.append(clean)
        sanitized["folders"] = clean_folders
    return sanitized


def _absolute_path(path: Path) -> Path:
    # Keep the lexical path so a symlink/junction at the storage root remains
    # visible to the destructive-operation safety checks below.
    return Path(os.path.abspath(Path(path).expanduser()))


def _is_reparse_point(path: Path) -> bool:
    if path.is_symlink():
        return True
    try:
        attributes = path.lstat().st_file_attributes
    except (AttributeError, FileNotFoundError, OSError):
        return False
    return bool(attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0))


def _require_safe_directory(path: Path, label: str) -> None:
    if not path.is_dir() or _is_reparse_point(path):
        raise LtkSkinCleanupError(f"{label} is not a normal directory: {path}")


def _require_safe_directory_path(path: Path, label: str) -> None:
    """Reject a destructive target reached through any reparse point."""

    _require_safe_directory(path, label)
    current = path
    while current != current.parent:
        if _is_reparse_point(current):
            raise LtkSkinCleanupError(f"{label} contains a reparse point: {current}")
        current = current.parent


def _preflight_optional_directory(path: Path, label: str) -> None:
    if not os.path.lexists(path):
        return
    _require_safe_directory(path, label)


def _preflight_optional_file(path: Path, label: str) -> None:
    if not os.path.lexists(path):
        return
    if not path.is_file() or _is_reparse_point(path):
        raise LtkSkinCleanupError(f"{label} is not a normal file: {path}")


def _preflight_tree(path: Path, label: str) -> None:
    """Reject links, reparse points, and special files anywhere below *path*."""

    if not path.exists():
        return
    pending = [path]
    while pending:
        parent = pending.pop()
        try:
            children = tuple(os.scandir(parent))
        except OSError as error:
            raise LtkSkinCleanupError(f"Could not inspect {label}: {parent}") from error
        for child in children:
            child_path = Path(child.path)
            try:
                child_stat = child.stat(follow_symlinks=False)
            except OSError as error:
                raise LtkSkinCleanupError(f"Could not inspect {label}: {child_path}") from error
            if child.is_symlink() or _is_reparse_stat(child_stat):
                raise LtkSkinCleanupError(f"{label} contains a link or reparse point: {child_path}")
            if stat.S_ISDIR(child_stat.st_mode):
                pending.append(child_path)
            elif not stat.S_ISREG(child_stat.st_mode):
                raise LtkSkinCleanupError(f"{label} contains a special file: {child_path}")


def _count_immediate_files(path: Path) -> int:
    if not path.exists():
        return 0
    try:
        return sum(1 for child in path.iterdir() if child.is_file() and not child.is_symlink())
    except OSError as error:
        raise LtkSkinCleanupError(f"Could not inspect LTK directory: {path}") from error


def _count_immediate_directories(path: Path) -> int:
    if not path.exists():
        return 0
    try:
        return sum(1 for child in path.iterdir() if child.is_dir() and not child.is_symlink())
    except OSError as error:
        raise LtkSkinCleanupError(f"Could not inspect LTK directory: {path}") from error


def _same_file_snapshot(before: os.stat_result, after: os.stat_result) -> bool:
    return (
        before.st_dev,
        before.st_ino,
        before.st_size,
        before.st_mtime_ns,
    ) == (
        after.st_dev,
        after.st_ino,
        after.st_size,
        after.st_mtime_ns,
    )


def _is_reparse_stat(value: os.stat_result) -> bool:
    attributes = getattr(value, "st_file_attributes", 0)
    return bool(attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0))


__all__ = [
    "LtkSkinCleanupBlockedError",
    "LtkSkinCleanupBusyError",
    "LtkSkinCleanupError",
    "LtkSkinCleanupResult",
    "LtkSkinCleanupService",
]
