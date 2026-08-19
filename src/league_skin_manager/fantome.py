"""Validation for ``.fantome`` skin packages.

The skin source is outside the application's trust boundary, and a fantome is
an ordinary ZIP file.  Handing one to LTK without inspection would let a
malicious or malformed package attack whatever unpacks it, so every archive is
checked before it is admitted to the cache.

This module validates only.  Nothing here extracts: LTK owns unpacking, and
the previous design's extraction pipeline existed solely to populate CSLOL's
``installed`` tree, which no longer exists.

Per-archive safety limits are retained because they are a single comparison
each; the aggregate free-space and whole-transaction gates of the previous
design are not, since there is no transaction to protect any more.
"""

from __future__ import annotations

import json
import re
import stat
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from .hashing import git_blob_sha, is_real_file, sha256_file

MAX_MEMBERS = 10_000
MAX_COMPRESSED_BYTES = 64 * 1024 * 1024
MAX_UNCOMPRESSED_BYTES = 256 * 1024 * 1024
MAX_METADATA_BYTES = 1024 * 1024

# LTK rejects a package whose META/info.json is missing any of these, logging
# "Skipping invalid archive" and moving on.  Requiring them here means a
# package that would be silently dropped on import never reaches the cache, so
# the count this application reports is the count LTK will actually hold.
REQUIRED_METADATA_FIELDS = ("Name", "Author", "Version", "Description")

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
    """The archive contains a path or file type unsafe to unpack."""


class FantomeStructureError(FantomeError):
    """The archive is a safe ZIP but is not a usable skin package."""


@dataclass(frozen=True, slots=True)
class ValidatedFantome:
    """What a successful validation learned about a package."""

    name: str
    author: str
    version: str
    compressed_bytes: int
    uncompressed_bytes: int
    wad_files: tuple[str, ...]


def verify_artifact(
    path: Path,
    *,
    expected_size: int | None,
    expected_sha: str | None,
) -> None:
    """Verify a downloaded artifact's size and digest.

    GitHub tree entries publish a 40-character Git blob SHA-1; a 64-character
    digest is treated as SHA-256.  Any other length is an error rather than a
    silent skip, so an unrecognised digest cannot quietly disable the check.
    """

    if not is_real_file(path):
        raise FantomeIntegrityError(f"Artifact does not exist or is not a regular file: {path}")

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


def validate_fantome(
    archive_path: Path,
    *,
    expected_size: int | None = None,
    expected_sha: str | None = None,
) -> ValidatedFantome:
    """Validate a complete fantome archive, or raise :class:`FantomeError`."""

    verify_artifact(archive_path, expected_size=expected_size, expected_sha=expected_sha)

    compressed_bytes = archive_path.stat().st_size
    if compressed_bytes > MAX_COMPRESSED_BYTES:
        raise UnsafeFantomeError(
            f"Fantome exceeds the compressed-size limit ({compressed_bytes} > "
            f"{MAX_COMPRESSED_BYTES})"
        )

    try:
        with zipfile.ZipFile(archive_path) as archive:
            members = tuple(archive.infolist())
            if not members:
                raise FantomeStructureError("Fantome archive is empty")
            if len(members) > MAX_MEMBERS:
                raise UnsafeFantomeError(
                    f"Fantome contains too many entries ({len(members)} > {MAX_MEMBERS})"
                )

            seen: set[str] = set()
            uncompressed_bytes = 0
            wad_files: list[str] = []
            info_member: zipfile.ZipInfo | None = None

            for member in members:
                normalized = _safe_member_path(member)
                key = normalized.casefold()
                if key in seen:
                    raise UnsafeFantomeError(
                        f"Fantome contains duplicate Windows path: {normalized}"
                    )
                seen.add(key)

                if member.flag_bits & 0x1:
                    raise UnsafeFantomeError(f"Encrypted ZIP member is unsupported: {normalized}")
                uncompressed_bytes += member.file_size
                if uncompressed_bytes > MAX_UNCOMPRESSED_BYTES:
                    raise UnsafeFantomeError("Fantome exceeds the uncompressed-size limit")

                if not member.is_dir():
                    if normalized == "META/info.json":
                        info_member = member
                    elif normalized.startswith("WAD/"):
                        wad_files.append(normalized)

            if info_member is None:
                raise FantomeStructureError("Fantome is missing META/info.json")
            if not wad_files:
                raise FantomeStructureError("Fantome does not contain a WAD file")
            if info_member.file_size > MAX_METADATA_BYTES:
                raise UnsafeFantomeError("Fantome metadata exceeds the size limit")

            metadata = _read_metadata(archive, info_member)

            corrupt = archive.testzip()
            if corrupt is not None:
                raise FantomeIntegrityError(f"Fantome member failed CRC validation: {corrupt}")
    except zipfile.BadZipFile as error:
        raise FantomeStructureError("Artifact is not a valid ZIP/fantome archive") from error

    return ValidatedFantome(
        name=metadata["Name"],
        author=metadata["Author"],
        version=metadata["Version"],
        compressed_bytes=compressed_bytes,
        uncompressed_bytes=uncompressed_bytes,
        wad_files=tuple(wad_files),
    )


def _read_metadata(archive: zipfile.ZipFile, member: zipfile.ZipInfo) -> dict[str, str]:
    """Return the required META/info.json fields, or raise."""

    try:
        raw = archive.read(member).decode("utf-8-sig")
        parsed = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError) as error:
        raise FantomeStructureError("Fantome META/info.json is not valid UTF-8 JSON") from error
    if not isinstance(parsed, dict):
        raise FantomeStructureError("Fantome metadata must be a JSON object")

    result: dict[str, str] = {}
    for field in REQUIRED_METADATA_FIELDS:
        value = parsed.get(field)
        if not isinstance(value, str):
            raise FantomeStructureError(f"Fantome metadata is missing the {field} field")
        result[field] = value
    return result


def _safe_member_path(member: zipfile.ZipInfo) -> str:
    """Return a normalized member path, or raise if it is unsafe on Windows."""

    raw_name = member.filename
    if not raw_name or "\x00" in raw_name:
        raise UnsafeFantomeError("Fantome contains an empty or NUL path")
    normalized = raw_name.replace("\\", "/")
    if normalized.startswith("/"):
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
        if component.split(".", 1)[0].upper() in _WINDOWS_RESERVED_NAMES:
            raise UnsafeFantomeError(f"Fantome contains a reserved Windows path: {raw_name}")
        if index == 0 and re.fullmatch(r"[A-Za-z]:", component):
            raise UnsafeFantomeError(f"Fantome contains a drive path: {raw_name}")

    file_type = stat.S_IFMT(member.external_attr >> 16)
    if file_type == stat.S_IFLNK:
        raise UnsafeFantomeError(f"Fantome contains a symlink: {raw_name}")
    if file_type not in {0, stat.S_IFREG, stat.S_IFDIR}:
        raise UnsafeFantomeError(f"Fantome contains a special file: {raw_name}")

    # zipfile keeps a trailing slash on directory entries; dropping it makes
    # collision checks deterministic.
    return "/".join(path.parts)


__all__ = [
    "REQUIRED_METADATA_FIELDS",
    "FantomeError",
    "FantomeIntegrityError",
    "FantomeStructureError",
    "UnsafeFantomeError",
    "ValidatedFantome",
    "validate_fantome",
    "verify_artifact",
]
