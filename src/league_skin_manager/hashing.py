"""File digests and filesystem-safety predicates.

Pure domain helpers: no network, no application state, no knowledge of what a
skin is.  Everything above this module reaches for these rather than growing
its own copy, which is how five separate reparse-point checks and six digest
helpers accumulated in the previous design.
"""

from __future__ import annotations

import hashlib
import os
import stat
from pathlib import Path

CHUNK_BYTES = 1024 * 1024

_REPARSE_POINT = int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))


def git_blob_sha(path: Path) -> str:
    """Return the Git object SHA-1 for *path*, streamed rather than buffered.

    This is the identity GitHub publishes for every blob in a tree response, so
    it is what downloads are verified against and what names the package cache.
    """

    size = path.stat().st_size
    digest = hashlib.sha1(usedforsecurity=False)
    digest.update(f"blob {size}\0".encode())
    with path.open("rb") as stream:
        while chunk := stream.read(CHUNK_BYTES):
            digest.update(chunk)
    return digest.hexdigest()


def sha256_file(path: Path) -> str:
    """Return the SHA-256 digest for *path*, streamed rather than buffered."""

    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(CHUNK_BYTES):
            digest.update(chunk)
    return digest.hexdigest()


def is_reparse_stat(value: os.stat_result) -> bool:
    """Return whether a stat result describes a junction, symlink, or mount."""

    return bool(int(getattr(value, "st_file_attributes", 0)) & _REPARSE_POINT)


def is_real_file(path: Path) -> bool:
    """Return whether *path* is a regular file that is not a reparse point."""

    try:
        value = os.stat(path, follow_symlinks=False)
    except OSError:
        return False
    return stat.S_ISREG(value.st_mode) and not is_reparse_stat(value)


def is_real_directory(path: Path) -> bool:
    """Return whether *path* is a real directory that is not a reparse point."""

    try:
        value = os.stat(path, follow_symlinks=False)
    except OSError:
        return False
    return stat.S_ISDIR(value.st_mode) and not is_reparse_stat(value)


__all__ = [
    "CHUNK_BYTES",
    "git_blob_sha",
    "is_real_directory",
    "is_real_file",
    "is_reparse_stat",
    "sha256_file",
]
