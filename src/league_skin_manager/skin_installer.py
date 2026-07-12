"""Validation and safe extraction for CSLOL ``.fantome`` packages.

The source repositories are outside the application's trust boundary.  A
fantome is a ZIP file, so treating it as a normal archive would expose the
install directory to path traversal, symlink, duplicate-path, and zip-bomb
attacks.  This module validates the complete archive before creating output
and then extracts entries manually.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import unicodedata
import zipfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Protocol

MANAGED_DIRECTORY_PREFIX = "lsmvn--"
DEFAULT_MAX_MEMBERS = 10_000
DEFAULT_MAX_COMPRESSED_BYTES = 64 * 1024 * 1024
DEFAULT_MAX_UNCOMPRESSED_BYTES = 256 * 1024 * 1024
DEFAULT_MAX_METADATA_BYTES = 1024 * 1024
_COPY_CHUNK_SIZE = 1024 * 1024
_WINDOWS_RESERVED_NAMES = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{number}" for number in range(1, 10)),
    *(f"LPT{number}" for number in range(1, 10)),
}


class FantomeError(ValueError):
    """Base class for an invalid or unsafe fantome archive."""


class FantomeIntegrityError(FantomeError):
    """The artifact bytes do not match the source manifest."""


class UnsafeFantomeError(FantomeError):
    """The archive contains a path or file type unsafe to extract."""


class FantomeStructureError(FantomeError):
    """The archive is safe as a ZIP but is not a usable CSLOL mod."""


class ExtractionCancelled(RuntimeError):
    """Extraction was cancelled before it completed."""


class CancelSignal(Protocol):
    def is_set(self) -> bool:
        """Return whether cancellation was requested."""


@dataclass(frozen=True, slots=True)
class ValidatedFantome:
    """Metadata retained from a successful archive validation."""

    members: tuple[zipfile.ZipInfo, ...]
    compressed_bytes: int
    uncompressed_bytes: int
    wad_files: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ExtractedModFingerprint:
    """Durable identity and resource usage of an extracted managed mod."""

    sha256: str
    uncompressed_bytes: int
    file_count: int


def git_blob_sha(path: Path) -> str:
    """Return the Git object SHA-1 for *path* without loading it all at once."""

    size = path.stat().st_size
    digest = hashlib.sha1(usedforsecurity=False)
    digest.update(f"blob {size}\0".encode())
    with path.open("rb") as stream:
        while chunk := stream.read(_COPY_CHUNK_SIZE):
            digest.update(chunk)
    return digest.hexdigest()


def sha256_file(path: Path) -> str:
    """Return the SHA-256 digest for *path*."""

    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(_COPY_CHUNK_SIZE):
            digest.update(chunk)
    return digest.hexdigest()


def verify_artifact(
    path: Path,
    *,
    expected_size: int | None,
    expected_sha: str | None,
) -> None:
    """Verify artifact size and digest.

    GitHub tree entries expose a 40-character Git blob SHA rather than a raw
    file SHA-1, while locally generated catalogs may provide SHA-256.  Those
    are the only accepted formats so an unrecognised digest cannot silently
    disable integrity checking.
    """

    if not path.is_file() or path.is_symlink():
        raise FantomeIntegrityError(f"Artifact does not exist: {path}")

    actual_size = path.stat().st_size
    if expected_size is not None and expected_size > 0 and actual_size != expected_size:
        raise FantomeIntegrityError(
            f"Artifact size mismatch: expected {expected_size}, got {actual_size}"
        )

    if expected_sha is None:
        return
    digest = expected_sha.strip().lower()
    if not re.fullmatch(r"[0-9a-f]+", digest):
        raise FantomeIntegrityError("Artifact digest is not hexadecimal")
    if len(digest) == 40:
        actual_digest = git_blob_sha(path)
    elif len(digest) == 64:
        actual_digest = sha256_file(path)
    else:
        raise FantomeIntegrityError("Artifact digest must be a Git blob SHA-1 or SHA-256")
    if actual_digest != digest:
        raise FantomeIntegrityError(
            f"Artifact digest mismatch: expected {digest}, got {actual_digest}"
        )


def managed_directory_name(champion: str, name: str, source_path: str) -> str:
    """Create a stable, collision-resistant directory name for a managed mod."""

    if not source_path or "\x00" in source_path:
        raise ValueError("source_path must be a non-empty path")
    champion_slug = _slug(champion, fallback="champion")
    name_slug = _slug(name, fallback="skin")
    identity = hashlib.sha256(source_path.encode("utf-8")).hexdigest()[:12]
    # Keep well below legacy Windows MAX_PATH after adding the install root.
    return f"{MANAGED_DIRECTORY_PREFIX}{champion_slug[:36]}--{name_slug[:60]}--{identity}"


def validate_fantome(
    archive_path: Path,
    *,
    expected_size: int | None = None,
    expected_sha: str | None = None,
    max_members: int = DEFAULT_MAX_MEMBERS,
    max_compressed_bytes: int = DEFAULT_MAX_COMPRESSED_BYTES,
    max_uncompressed_bytes: int = DEFAULT_MAX_UNCOMPRESSED_BYTES,
) -> ValidatedFantome:
    """Validate an entire fantome archive without creating output."""

    verify_artifact(
        archive_path,
        expected_size=expected_size,
        expected_sha=expected_sha,
    )
    if max_members < 1 or max_compressed_bytes < 1 or max_uncompressed_bytes < 1:
        raise ValueError("Archive safety limits must be positive")
    compressed_bytes = archive_path.stat().st_size
    if compressed_bytes > max_compressed_bytes:
        raise UnsafeFantomeError(
            f"Fantome exceeds the compressed-size safety limit "
            f"({compressed_bytes} > {max_compressed_bytes})"
        )

    try:
        with zipfile.ZipFile(archive_path) as archive:
            members = tuple(archive.infolist())
            if not members:
                raise FantomeStructureError("Fantome archive is empty")
            if len(members) > max_members:
                raise UnsafeFantomeError(
                    f"Fantome contains too many entries ({len(members)} > {max_members})"
                )

            seen_paths: set[str] = set()
            total_uncompressed = 0
            wad_files: list[str] = []
            info_member: zipfile.ZipInfo | None = None

            for member in members:
                normalized = _safe_member_path(member)
                collision_key = normalized.casefold()
                if collision_key in seen_paths:
                    raise UnsafeFantomeError(
                        f"Fantome contains duplicate Windows path: {normalized}"
                    )
                seen_paths.add(collision_key)

                if member.flag_bits & 0x1:
                    raise UnsafeFantomeError(f"Encrypted ZIP member is unsupported: {normalized}")
                total_uncompressed += member.file_size
                if total_uncompressed > max_uncompressed_bytes:
                    raise UnsafeFantomeError("Fantome exceeds the uncompressed-size safety limit")

                if not member.is_dir():
                    if normalized == "META/info.json":
                        info_member = member
                    if normalized.startswith("WAD/"):
                        wad_files.append(normalized)

            if info_member is None:
                raise FantomeStructureError("Fantome is missing META/info.json")
            if not wad_files:
                raise FantomeStructureError("Fantome does not contain a WAD file")
            if info_member.file_size > DEFAULT_MAX_METADATA_BYTES:
                raise UnsafeFantomeError("Fantome metadata exceeds the size safety limit")

            try:
                metadata = json.loads(archive.read(info_member))
            except (json.JSONDecodeError, UnicodeDecodeError) as error:
                raise FantomeStructureError(
                    "Fantome META/info.json is not valid UTF-8 JSON"
                ) from error
            if not isinstance(metadata, dict):
                raise FantomeStructureError("Fantome metadata must be a JSON object")

            corrupt_member = archive.testzip()
            if corrupt_member is not None:
                raise FantomeIntegrityError(
                    f"Fantome member failed CRC validation: {corrupt_member}"
                )
    except zipfile.BadZipFile as error:
        raise FantomeStructureError("Artifact is not a valid ZIP/fantome archive") from error

    return ValidatedFantome(
        members=members,
        compressed_bytes=compressed_bytes,
        uncompressed_bytes=total_uncompressed,
        wad_files=tuple(wad_files),
    )


def extract_fantome(
    archive_path: Path,
    destination: Path,
    *,
    expected_size: int | None = None,
    expected_sha: str | None = None,
    cancel_event: CancelSignal | None = None,
    chunk_progress: Callable[[int, int], None] | None = None,
    max_compressed_bytes: int = DEFAULT_MAX_COMPRESSED_BYTES,
    max_uncompressed_bytes: int = DEFAULT_MAX_UNCOMPRESSED_BYTES,
) -> ValidatedFantome:
    """Validate and safely extract *archive_path* into a new *destination*.

    The destination must not already exist.  If extraction fails or is
    cancelled, all output from this call is removed.
    """

    validated = validate_fantome(
        archive_path,
        expected_size=expected_size,
        expected_sha=expected_sha,
        max_compressed_bytes=max_compressed_bytes,
        max_uncompressed_bytes=max_uncompressed_bytes,
    )
    if destination.exists() or destination.is_symlink():
        raise FileExistsError(f"Extraction destination already exists: {destination}")

    written = 0
    destination.mkdir(parents=True)
    try:
        with zipfile.ZipFile(archive_path) as archive:
            for member in validated.members:
                _raise_if_cancelled(cancel_event)
                normalized = _safe_member_path(member)
                relative = PurePosixPath(normalized)
                output = destination.joinpath(*relative.parts)
                if member.is_dir():
                    output.mkdir(parents=True, exist_ok=True)
                    continue

                output.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(member) as source, output.open("xb") as target:
                    while chunk := source.read(_COPY_CHUNK_SIZE):
                        _raise_if_cancelled(cancel_event)
                        target.write(chunk)
                        written += len(chunk)
                        if chunk_progress is not None:
                            chunk_progress(written, validated.uncompressed_bytes)
    except BaseException:
        shutil.rmtree(destination, ignore_errors=True)
        raise

    return validated


def inspect_extracted_mod(
    directory: Path,
    *,
    max_uncompressed_bytes: int = DEFAULT_MAX_UNCOMPRESSED_BYTES,
) -> ExtractedModFingerprint | None:
    """Fingerprint a safe extracted tree without following reparse points.

    The digest covers every relative directory name and every regular file's
    path, size, and SHA-256.  Returning ``None`` means the live directory is
    unsafe, structurally invalid, over budget, or changed while being read.
    """

    if max_uncompressed_bytes < 1:
        raise ValueError("Extracted-mod safety limit must be positive")
    root = Path(directory)
    try:
        root_stat = os.stat(root, follow_symlinks=False)
        if not stat.S_ISDIR(root_stat.st_mode) or _is_reparse_stat(root_stat):
            return None

        pending: list[tuple[Path, PurePosixPath]] = [(root, PurePosixPath())]
        records: list[tuple[str, str, int, bytes]] = []
        metadata_bytes: bytes | None = None
        has_wad_file = False
        total_bytes = 0
        file_count = 0

        while pending:
            parent, relative_parent = pending.pop()
            with os.scandir(parent) as iterator:
                children = sorted(iterator, key=lambda entry: (entry.name.casefold(), entry.name))
            for child in children:
                if not _safe_existing_component(child.name):
                    return None
                relative = relative_parent / child.name
                relative_name = relative.as_posix()
                child_stat = child.stat(follow_symlinks=False)
                if child.is_symlink() or _is_reparse_stat(child_stat):
                    return None
                child_path = Path(child.path)
                if stat.S_ISDIR(child_stat.st_mode):
                    records.append(("D", relative_name, 0, b""))
                    pending.append((child_path, relative))
                    continue
                if not stat.S_ISREG(child_stat.st_mode):
                    return None

                total_bytes += child_stat.st_size
                file_count += 1
                if total_bytes > max_uncompressed_bytes:
                    return None
                capture = relative_name == "META/info.json"
                if capture and child_stat.st_size > DEFAULT_MAX_METADATA_BYTES:
                    return None
                file_digest, captured = _stable_file_digest(
                    child_path,
                    child_stat,
                    capture=capture,
                )
                if capture:
                    metadata_bytes = captured
                if len(relative.parts) > 1 and relative.parts[0] == "WAD":
                    has_wad_file = True
                records.append(("F", relative_name, child_stat.st_size, file_digest))

        if metadata_bytes is None or not has_wad_file:
            return None
        try:
            metadata = json.loads(metadata_bytes.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return None
        if not isinstance(metadata, dict):
            return None

        digest = hashlib.sha256()
        for kind, relative_name, size, content_digest in sorted(
            records,
            key=lambda record: (record[1].casefold(), record[1], record[0]),
        ):
            encoded_name = relative_name.encode("utf-8")
            digest.update(kind.encode("ascii"))
            digest.update(len(encoded_name).to_bytes(4, "big"))
            digest.update(encoded_name)
            digest.update(size.to_bytes(8, "big"))
            digest.update(content_digest)
        return ExtractedModFingerprint(
            sha256=digest.hexdigest(),
            uncompressed_bytes=total_bytes,
            file_count=file_count,
        )
    except (OSError, ValueError):
        return None


def validate_extracted_mod(directory: Path, *, expected_sha256: str | None = None) -> bool:
    """Return whether an existing managed directory is safe and authentic."""

    fingerprint = inspect_extracted_mod(directory)
    if fingerprint is None:
        return False
    return expected_sha256 is None or fingerprint.sha256 == expected_sha256.lower()


def _stable_file_digest(
    path: Path,
    before: os.stat_result,
    *,
    capture: bool,
) -> tuple[bytes, bytes | None]:
    digest = hashlib.sha256()
    captured = bytearray() if capture else None
    with path.open("rb") as stream:
        while chunk := stream.read(_COPY_CHUNK_SIZE):
            digest.update(chunk)
            if captured is not None:
                captured.extend(chunk)
    after = os.stat(path, follow_symlinks=False)
    if _is_reparse_stat(after) or not _same_file_stat(before, after):
        raise OSError(f"Managed file changed while being inspected: {path}")
    return digest.digest(), bytes(captured) if captured is not None else None


def _same_file_stat(before: os.stat_result, after: os.stat_result) -> bool:
    if (
        before.st_mode != after.st_mode
        or before.st_size != after.st_size
        or before.st_mtime_ns != after.st_mtime_ns
    ):
        return False
    # Windows DirEntry may report zero device/inode values while os.stat opens
    # the file and returns real identifiers.  Compare them only when both calls
    # supplied meaningful values.
    if before.st_dev and after.st_dev and before.st_dev != after.st_dev:
        return False
    return not (before.st_ino and after.st_ino and before.st_ino != after.st_ino)


def _is_reparse_stat(value: os.stat_result) -> bool:
    attributes = int(getattr(value, "st_file_attributes", 0))
    reparse_flag = int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))
    return bool(attributes & reparse_flag)


def _safe_existing_component(component: str) -> bool:
    if not component or component in {".", ".."} or "\x00" in component:
        return False
    if ":" in component or component.endswith((" ", ".")):
        return False
    return component.split(".", 1)[0].upper() not in _WINDOWS_RESERVED_NAMES


def _safe_member_path(member: zipfile.ZipInfo) -> str:
    raw_name = member.filename
    if not raw_name or "\x00" in raw_name:
        raise UnsafeFantomeError("Fantome contains an empty or NUL path")
    normalized = raw_name.replace("\\", "/")
    if normalized.startswith("/") or normalized.startswith("//"):
        raise UnsafeFantomeError(f"Fantome contains an absolute path: {raw_name}")

    path = PurePosixPath(normalized)
    if path.is_absolute() or not path.parts:
        raise UnsafeFantomeError(f"Fantome contains an invalid path: {raw_name}")
    for index, component in enumerate(path.parts):
        if component in {"", ".", ".."}:
            raise UnsafeFantomeError(f"Fantome contains path traversal: {raw_name}")
        if ":" in component:
            raise UnsafeFantomeError(f"Fantome contains a Windows drive/ADS path: {raw_name}")
        if component.endswith((" ", ".")):
            raise UnsafeFantomeError(f"Fantome contains an ambiguous Windows path: {raw_name}")
        stem = component.split(".", 1)[0].upper()
        if stem in _WINDOWS_RESERVED_NAMES:
            raise UnsafeFantomeError(f"Fantome contains a reserved Windows path: {raw_name}")
        if index == 0 and re.fullmatch(r"[A-Za-z]:", component):
            raise UnsafeFantomeError(f"Fantome contains a drive path: {raw_name}")

    unix_mode = member.external_attr >> 16
    file_type = stat.S_IFMT(unix_mode)
    if file_type == stat.S_IFLNK:
        raise UnsafeFantomeError(f"Fantome contains a symlink: {raw_name}")
    if file_type not in {0, stat.S_IFREG, stat.S_IFDIR}:
        raise UnsafeFantomeError(f"Fantome contains a special file: {raw_name}")

    # zipfile preserves a trailing slash on directory entries.  Removing it
    # makes collision and extraction checks deterministic.
    return "/".join(path.parts)


def _slug(value: str, *, fallback: str) -> str:
    normalized = unicodedata.normalize("NFKD", value or "")
    ascii_value = normalized.encode("ascii", "ignore").decode("ascii").lower()
    result = re.sub(r"[^a-z0-9]+", "-", ascii_value).strip("-")
    return result or fallback


def _raise_if_cancelled(cancel_event: CancelSignal | None) -> None:
    if cancel_event is not None and cancel_event.is_set():
        raise ExtractionCancelled("Fantome extraction was cancelled")
