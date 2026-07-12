"""Verified, staged, and crash-recoverable CSLOL Manager updates."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
import stat
import subprocess
import tempfile
import time
import zipfile
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from enum import Enum
from pathlib import Path, PurePosixPath, PureWindowsPath
from threading import Event
from urllib.parse import quote, urlparse

import requests

from .atomic import atomic_write_json, atomic_write_text

OFFICIAL_RELEASES_PAGE = "https://github.com/LeagueToolkit/cslol-manager/releases"
UPDATE_TRANSACTION_PREFIX = ".manager-update-"
UPDATE_JOURNAL_SCHEMA = 1
MAX_ARCHIVE_MEMBERS = 20_000
MAX_ARCHIVE_MEMBER_BYTES = 256 * 1024 * 1024
MAX_ARCHIVE_UNCOMPRESSED_BYTES = 768 * 1024 * 1024
_COPY_CHUNK_SIZE = 1024 * 1024
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_WINDOWS_RESERVED_NAMES = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{number}" for number in range(1, 10)),
    *(f"LPT{number}" for number in range(1, 10)),
}

ReleaseIdentity = tuple[str, str, int]

# The upstream Windows SFX is currently not Authenticode signed.  This exact
# artifact was independently fetched and inspected on 2026-07-13.  Unknown
# releases are intentionally not downloaded or executed; adding another entry
# requires a fresh source/signature review and an application release.
TRUSTED_RELEASE_ASSETS: Mapping[ReleaseIdentity, str] = {
    (
        "2026-04-15-23f2308",
        "cslol-manager-windows.exe",
        37_329_409,
    ): "f528db8cf63ebd580886c747bff7ca2de69644307724738eea3de22ce8ea04ac",
}


class ManagerUpdateStatus(str, Enum):
    CURRENT = "current"
    UPDATED = "updated"
    DEFERRED_RUNNING = "deferred_running"
    CANCELLED = "cancelled"


class ManagerUpdateError(RuntimeError):
    """Base error for a manager update that did not alter the live install."""


class UntrustedReleaseError(ManagerUpdateError):
    """The latest network asset is not in this application build's allowlist."""


class ManagerTransactionError(ManagerUpdateError):
    """A manager update transaction or recovery could not complete safely."""


@dataclass(frozen=True, slots=True)
class ReleaseAsset:
    name: str
    url: str
    size: int


@dataclass(frozen=True, slots=True)
class ManagerRelease:
    version: str
    asset: ReleaseAsset


class ManagerReleaseClient:
    def __init__(
        self,
        releases_url: str,
        logger: logging.Logger,
        session: requests.Session | None = None,
        timeout: float = 15.0,
    ) -> None:
        self.releases_url = releases_url
        self.logger = logger
        self._owns_session = session is None
        self.session = session or requests.Session()
        self.timeout = timeout

    def close(self) -> None:
        if self._owns_session:
            self.session.close()

    def latest(self) -> ManagerRelease:
        response = self.session.get(self.releases_url, timeout=self.timeout)
        try:
            response.raise_for_status()
            payload = response.json()
        finally:
            close = getattr(response, "close", None)
            if callable(close):
                close()
        if not isinstance(payload, dict) or not isinstance(payload.get("tag_name"), str):
            raise ValueError("GitHub returned invalid CSLOL release metadata")
        assets = payload.get("assets")
        if not isinstance(assets, list):
            raise ValueError("CSLOL release did not contain assets")

        candidates: list[ReleaseAsset] = []
        for value in assets:
            if not isinstance(value, dict):
                continue
            name = value.get("name")
            url = value.get("browser_download_url")
            size = value.get("size")
            if not isinstance(name, str) or not isinstance(url, str):
                continue
            if not isinstance(size, int) or isinstance(size, bool) or size <= 0:
                continue
            lowered = name.casefold()
            if lowered.endswith(".zip") or (lowered.endswith(".exe") and "manager" in lowered):
                candidates.append(ReleaseAsset(name, url, size))
        if not candidates:
            raise ValueError("No supported Windows CSLOL Manager asset was published")
        candidates.sort(key=lambda asset: (not asset.name.casefold().endswith(".zip"), asset.name))
        return ManagerRelease(payload["tag_name"], candidates[0])

    def download(self, asset: ReleaseAsset, destination: Path, cancel_event: Event) -> Path:
        host = urlparse(asset.url).hostname
        if host not in {"github.com", "objects.githubusercontent.com"}:
            raise ValueError(f"Unexpected CSLOL download host: {host}")
        destination.parent.mkdir(parents=True, exist_ok=True)
        partial = destination.with_name(f".{destination.name}.part")
        partial.unlink(missing_ok=True)
        try:
            with self.session.get(
                asset.url,
                stream=True,
                timeout=(10, 60),
            ) as response:
                response.raise_for_status()
                written = 0
                with partial.open("xb") as output:
                    for chunk in response.iter_content(chunk_size=1024 * 256):
                        if cancel_event.is_set():
                            raise InterruptedError("Manager download cancelled")
                        if not chunk:
                            continue
                        output.write(chunk)
                        written += len(chunk)
                        if written > asset.size:
                            raise OSError(f"CSLOL asset exceeded its expected {asset.size} bytes")
                    output.flush()
                    os.fsync(output.fileno())
            if written != asset.size:
                raise OSError(
                    f"CSLOL asset size mismatch: expected {asset.size}, received {written}"
                )
            os.replace(partial, destination)
            return destination
        finally:
            partial.unlink(missing_ok=True)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(_COPY_CHUNK_SIZE):
            digest.update(chunk)
    return digest.hexdigest()


def _safe_extract(
    archive: zipfile.ZipFile,
    destination: Path,
    cancel_event: Event | None = None,
) -> None:
    """Validate a complete ZIP, then manually extract it into a staging directory."""

    if destination.is_symlink() or (destination.exists() and not destination.is_dir()):
        raise ValueError("CSLOL extraction destination must be a real directory")
    members = tuple(archive.infolist())
    if not members:
        raise ValueError("CSLOL archive is empty")
    if len(members) > MAX_ARCHIVE_MEMBERS:
        raise ValueError("CSLOL archive contains too many members")

    normalized_paths: dict[zipfile.ZipInfo, str] = {}
    seen: set[str] = set()
    total_uncompressed = 0
    for member in members:
        normalized = _safe_archive_member_path(member)
        collision_key = normalized.casefold()
        if collision_key in seen:
            raise ValueError(f"Duplicate Windows path in CSLOL archive: {normalized}")
        seen.add(collision_key)
        normalized_paths[member] = normalized
        if member.flag_bits & 0x1:
            raise ValueError(f"Encrypted CSLOL archive member is not allowed: {normalized}")
        if member.file_size > MAX_ARCHIVE_MEMBER_BYTES:
            raise ValueError(f"CSLOL archive member is too large: {normalized}")
        total_uncompressed += member.file_size
        if total_uncompressed > MAX_ARCHIVE_UNCOMPRESSED_BYTES:
            raise ValueError("CSLOL archive exceeds the expanded-size limit")

    corrupt_member = archive.testzip()
    if corrupt_member is not None:
        raise ValueError(f"CSLOL archive member failed CRC validation: {corrupt_member}")
    if cancel_event is not None and cancel_event.is_set():
        raise InterruptedError("Manager extraction cancelled")

    destination.mkdir(parents=True, exist_ok=True)
    for member in members:
        if cancel_event is not None and cancel_event.is_set():
            raise InterruptedError("Manager extraction cancelled")
        relative = PurePosixPath(normalized_paths[member])
        output = destination.joinpath(*relative.parts)
        if member.is_dir():
            output.mkdir(parents=True, exist_ok=True)
            continue
        output.parent.mkdir(parents=True, exist_ok=True)
        with archive.open(member) as source, output.open("xb") as target:
            while chunk := source.read(_COPY_CHUNK_SIZE):
                if cancel_event is not None and cancel_event.is_set():
                    raise InterruptedError("Manager extraction cancelled")
                target.write(chunk)


def _safe_archive_member_path(member: zipfile.ZipInfo) -> str:
    raw_name = member.filename
    if not raw_name or "\x00" in raw_name or "\\" in raw_name:
        raise ValueError(f"Unsafe path in CSLOL archive: {raw_name!r}")
    if raw_name.startswith(("/", "//")):
        raise ValueError(f"Unsafe path in CSLOL archive: {raw_name}")
    windows_path = PureWindowsPath(raw_name)
    if windows_path.is_absolute() or windows_path.drive:
        raise ValueError(f"Unsafe path in CSLOL archive: {raw_name}")

    raw_parts = raw_name.rstrip("/").split("/")
    if not raw_parts or any(part in {"", ".", ".."} for part in raw_parts):
        raise ValueError(f"Unsafe path in CSLOL archive: {raw_name}")
    for component in raw_parts:
        if ":" in component or component.endswith((" ", ".")):
            raise ValueError(f"Unsafe Windows path in CSLOL archive: {raw_name}")
        if component.split(".", 1)[0].upper() in _WINDOWS_RESERVED_NAMES:
            raise ValueError(f"Reserved Windows path in CSLOL archive: {raw_name}")

    unix_mode = member.external_attr >> 16
    file_type = stat.S_IFMT(unix_mode)
    if file_type == stat.S_IFLNK:
        raise ValueError(f"Symlink is not allowed in CSLOL archive: {raw_name}")
    if file_type not in {0, stat.S_IFREG, stat.S_IFDIR}:
        raise ValueError(f"Special file is not allowed in CSLOL archive: {raw_name}")
    return "/".join(raw_parts)


@dataclass(frozen=True, slots=True)
class _UpdateJournal:
    transaction_id: str
    transaction_root: Path
    version: str
    old_names: tuple[str, ...]
    new_names: tuple[str, ...]

    def to_json(self) -> dict[str, object]:
        return {
            "schema_version": UPDATE_JOURNAL_SCHEMA,
            "transaction_id": self.transaction_id,
            "transaction_root": str(self.transaction_root),
            "version": self.version,
            "old_names": list(self.old_names),
            "new_names": list(self.new_names),
        }


class ManagerUpdater:
    PRESERVED_NAMES = {"installed", "profiles"}

    def __init__(
        self,
        client: ManagerReleaseClient,
        manager_dir: Path,
        version_file: Path,
        is_manager_running: Callable[[], bool],
        logger: logging.Logger,
        *,
        trusted_assets: Mapping[ReleaseIdentity, str] = TRUSTED_RELEASE_ASSETS,
        extraction_timeout_seconds: float = 180.0,
    ) -> None:
        if extraction_timeout_seconds <= 0:
            raise ValueError("extraction_timeout_seconds must be positive")
        normalized_trust: dict[ReleaseIdentity, str] = {}
        for identity, digest in trusted_assets.items():
            normalized_digest = digest.casefold()
            if not _SHA256_PATTERN.fullmatch(normalized_digest):
                raise ValueError(f"Invalid trusted SHA-256 for {identity!r}")
            normalized_trust[identity] = normalized_digest
        self.client = client
        self.manager_dir = Path(manager_dir).resolve()
        self.version_file = Path(version_file).resolve()
        if self.version_file.parent != self.manager_dir or not _safe_direct_windows_name(
            self.version_file.name
        ):
            raise ValueError("Manager version marker must be a safe direct child of manager_dir")
        self.is_manager_running = is_manager_running
        self.logger = logger
        self.trusted_assets = normalized_trust
        self.extraction_timeout_seconds = extraction_timeout_seconds
        self.journal_path = (
            self.manager_dir.parent / f".{self.manager_dir.name}.update-transaction.json"
        )

    def _installed_version(self) -> str | None:
        try:
            return self.version_file.read_text(encoding="utf-8").strip() or None
        except OSError:
            return None

    def update(self, cancel_event: Event) -> ManagerUpdateStatus:
        self.manager_dir.mkdir(parents=True, exist_ok=True)
        self.recover()
        release = self.client.latest()
        manager_exe = self.manager_dir / "cslol-manager.exe"
        if self._installed_version() == release.version and manager_exe.is_file():
            return ManagerUpdateStatus.CURRENT
        if self.is_manager_running():
            return ManagerUpdateStatus.DEFERRED_RUNNING
        if cancel_event.is_set():
            return ManagerUpdateStatus.CANCELLED

        expected_sha = self._trusted_digest(release)
        transaction_root = Path(
            tempfile.mkdtemp(prefix=UPDATE_TRANSACTION_PREFIX, dir=self.manager_dir.parent)
        )
        try:
            asset_path = self.client.download(
                release.asset,
                transaction_root / release.asset.name,
                cancel_event,
            )
            if cancel_event.is_set():
                return ManagerUpdateStatus.CANCELLED
            actual_sha = _sha256_file(asset_path)
            if actual_sha != expected_sha:
                raise UntrustedReleaseError(
                    f"CSLOL asset SHA-256 mismatch for {release.asset.name}; "
                    "the live manager was not changed"
                )

            staged = transaction_root / "staged"
            staged.mkdir()
            if asset_path.suffix.casefold() == ".zip":
                with zipfile.ZipFile(asset_path) as archive:
                    _safe_extract(archive, staged, cancel_event)
            elif asset_path.suffix.casefold() == ".exe":
                self._extract_trusted_sfx(asset_path, staged, cancel_event)
            else:
                raise UntrustedReleaseError("Trusted manager asset has an unsupported file type")

            if cancel_event.is_set():
                return ManagerUpdateStatus.CANCELLED
            executables = list(staged.rglob("cslol-manager.exe"))
            if len(executables) != 1:
                raise ValueError("Staged CSLOL update did not contain one manager executable")
            source_root = executables[0].parent
            self._commit(source_root, transaction_root, release.version)
            self.logger.info("CSLOL Manager updated to %s", release.version)
            return ManagerUpdateStatus.UPDATED
        finally:
            if not self.journal_path.exists():
                shutil.rmtree(transaction_root, ignore_errors=True)

    def recover(self) -> bool:
        """Recover or finish cleanup for an interrupted manager replacement."""

        if not self.journal_path.exists():
            return False
        journal = self._load_journal()
        if (
            self._installed_version() == journal.version
            and (self.manager_dir / "cslol-manager.exe").is_file()
        ):
            self._cleanup_transaction(journal)
            return True
        self._rollback(journal)
        return True

    def _trusted_digest(self, release: ManagerRelease) -> str:
        identity = (release.version, release.asset.name, release.asset.size)
        expected = self.trusted_assets.get(identity)
        if expected is not None:
            return expected
        release_url = f"{OFFICIAL_RELEASES_PAGE}/tag/{quote(release.version, safe='')}"
        message = (
            "Automatic CSLOL Manager update is disabled for the unreviewed asset "
            f"{release.asset.name!r} ({release.version}). Existing files were not changed. "
            f"Install a reviewed release manually from {release_url}, or update this application "
            "to obtain a newer trusted checksum."
        )
        self.logger.error(message)
        raise UntrustedReleaseError(message)

    def _extract_trusted_sfx(
        self,
        asset_path: Path,
        staged: Path,
        cancel_event: Event,
    ) -> None:
        creation_flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        process = subprocess.Popen(  # noqa: S603 - exact bytes are SHA-256 allowlisted
            [str(asset_path), "-y", f"-o{staged.resolve()}"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=creation_flags,
        )
        deadline = time.monotonic() + self.extraction_timeout_seconds
        while process.poll() is None:
            if cancel_event.wait(0.1):
                _stop_process(process)
                raise InterruptedError("Manager extraction cancelled")
            if time.monotonic() >= deadline:
                _stop_process(process)
                raise TimeoutError("CSLOL self-extractor timed out")
        if process.returncode != 0:
            raise OSError(f"CSLOL self-extractor failed with code {process.returncode}")

    def _commit(self, source_root: Path, transaction_root: Path, version: str) -> None:
        backup = transaction_root / "backup"
        backup.mkdir()
        reserved_source_names = self.PRESERVED_NAMES | {self.version_file.name.casefold()}
        old_entries = tuple(
            entry
            for entry in self.manager_dir.iterdir()
            if entry.name.casefold() not in self.PRESERVED_NAMES
        )
        new_entries = tuple(
            entry
            for entry in source_root.iterdir()
            if entry.name.casefold() not in reserved_source_names
        )
        _validate_unique_names(new_entries)
        if not any(entry.name.casefold() == "cslol-manager.exe" for entry in new_entries):
            raise ManagerTransactionError("Staged manager root has no cslol-manager.exe")

        journal = _UpdateJournal(
            transaction_id=os.urandom(16).hex(),
            transaction_root=transaction_root,
            version=version,
            old_names=tuple(entry.name for entry in old_entries),
            new_names=tuple(entry.name for entry in new_entries),
        )
        atomic_write_json(self.journal_path, journal.to_json())
        state_committed = False
        try:
            for existing in old_entries:
                os.replace(existing, backup / existing.name)
            for source in new_entries:
                os.replace(source, self.manager_dir / source.name)
            atomic_write_text(self.version_file, version)
            state_committed = True
        except BaseException as error:
            try:
                self._rollback(journal)
            except BaseException as rollback_error:
                raise ManagerTransactionError(
                    "Manager replacement failed and rollback is incomplete; "
                    "the recovery journal was preserved"
                ) from rollback_error
            raise ManagerTransactionError(
                "Manager replacement failed; the previous install was restored"
            ) from error
        finally:
            if state_committed:
                self._cleanup_transaction(journal)

    def _rollback(self, journal: _UpdateJournal) -> None:
        backup = journal.transaction_root / "backup"
        if not journal.transaction_root.is_dir() or not backup.is_dir():
            raise ManagerTransactionError(
                "Manager rollback data is missing; refusing destructive recovery"
            )

        # Validate that every original is still either live or backed up before
        # removing any partially installed new files.
        for name in journal.old_names:
            if not _path_exists(backup / name) and not _path_exists(self.manager_dir / name):
                raise ManagerTransactionError(
                    f"Manager rollback cannot locate original file: {name}"
                )

        try:
            old_name_keys = {name.casefold() for name in journal.old_names}
            for name in journal.new_names:
                live = self.manager_dir / name
                matching_backup = _find_casefold(backup, name)
                if matching_backup is not None or name.casefold() not in old_name_keys:
                    _remove_path(live)
            for name in journal.old_names:
                backup_path = backup / name
                if not (backup_path.exists() or backup_path.is_symlink()):
                    continue
                live = self.manager_dir / name
                _remove_path(live)
                os.replace(backup_path, live)
        except BaseException as error:
            raise ManagerTransactionError(
                "Could not restore the previous manager install"
            ) from error
        self._cleanup_transaction(journal)

    def _cleanup_transaction(self, journal: _UpdateJournal) -> None:
        try:
            shutil.rmtree(journal.transaction_root)
        except FileNotFoundError:
            pass
        except OSError:
            return
        try:
            self.journal_path.unlink(missing_ok=True)
        except OSError:
            return

    def _load_journal(self) -> _UpdateJournal:
        try:
            raw = json.loads(self.journal_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ManagerTransactionError("Manager update journal is unreadable") from error
        if not isinstance(raw, dict) or raw.get("schema_version") != UPDATE_JOURNAL_SCHEMA:
            raise ManagerTransactionError("Manager update journal has an unsupported schema")
        transaction_id = _required_string(raw, "transaction_id")
        version = _required_string(raw, "version")
        root = Path(_required_string(raw, "transaction_root")).resolve()
        if root.parent != self.manager_dir.parent or not root.name.startswith(
            UPDATE_TRANSACTION_PREFIX
        ):
            raise ManagerTransactionError("Manager update journal references an unsafe directory")
        old_names = _safe_name_tuple(raw.get("old_names"), allow_version=True, updater=self)
        new_names = _safe_name_tuple(raw.get("new_names"), allow_version=False, updater=self)
        return _UpdateJournal(
            transaction_id=transaction_id,
            transaction_root=root,
            version=version,
            old_names=old_names,
            new_names=new_names,
        )


def _validate_unique_names(entries: Sequence[Path]) -> None:
    seen: set[str] = set()
    for entry in entries:
        key = entry.name.casefold()
        if key in seen:
            raise ManagerTransactionError(f"Staged manager contains duplicate name: {entry.name}")
        seen.add(key)


def _safe_name_tuple(
    value: object,
    *,
    allow_version: bool,
    updater: ManagerUpdater,
) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ManagerTransactionError("Manager update journal contains an invalid name list")
    names = tuple(value)
    if len({name.casefold() for name in names}) != len(names):
        raise ManagerTransactionError("Manager update journal contains duplicate names")
    reserved = updater.PRESERVED_NAMES
    for name in names:
        if not _safe_direct_windows_name(name) or name.casefold() in reserved:
            raise ManagerTransactionError("Manager update journal contains an unsafe name")
        if not allow_version and name.casefold() == updater.version_file.name.casefold():
            raise ManagerTransactionError("Manager package cannot replace the version marker")
    return names


def _required_string(value: Mapping[str, object], key: str) -> str:
    item = value.get(key)
    if not isinstance(item, str) or not item:
        raise ManagerTransactionError(f"Manager update journal field {key!r} is invalid")
    return item


def _find_casefold(directory: Path, name: str) -> Path | None:
    wanted = name.casefold()
    try:
        return next(
            (entry for entry in directory.iterdir() if entry.name.casefold() == wanted), None
        )
    except FileNotFoundError:
        return None


def _safe_direct_windows_name(name: str) -> bool:
    if (
        not name
        or name in {".", ".."}
        or "\x00" in name
        or "/" in name
        or "\\" in name
        or ":" in name
        or name.endswith((" ", "."))
    ):
        return False
    return name.split(".", 1)[0].upper() not in _WINDOWS_RESERVED_NAMES


def _path_exists(path: Path) -> bool:
    return path.exists() or path.is_symlink()


def _remove_path(path: Path) -> None:
    if path.is_symlink() or path.is_file():
        path.unlink(missing_ok=True)
    elif path.is_dir():
        shutil.rmtree(path)


def _stop_process(process: subprocess.Popen[bytes]) -> None:
    process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


__all__ = [
    "ManagerRelease",
    "ManagerReleaseClient",
    "ManagerTransactionError",
    "ManagerUpdater",
    "ManagerUpdateError",
    "ManagerUpdateStatus",
    "ReleaseAsset",
    "TRUSTED_RELEASE_ASSETS",
    "UntrustedReleaseError",
]
