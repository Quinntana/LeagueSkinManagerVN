"""Tests for the shared digest and filesystem-safety helpers."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

import pytest

from league_skin_manager.hashing import (
    git_blob_sha,
    is_real_directory,
    is_real_file,
    is_reparse_stat,
    sha256_file,
)

# The Git object hash of empty content is a fixed, published constant.
EMPTY_BLOB_SHA = "e69de29bb2d1d6434b8b29ae775ad8c2e48c5391"


def test_git_blob_sha_matches_the_published_empty_blob(tmp_path: Path) -> None:
    empty = tmp_path / "empty"
    empty.write_bytes(b"")
    assert git_blob_sha(empty) == EMPTY_BLOB_SHA


def test_git_blob_sha_matches_the_git_object_format(tmp_path: Path) -> None:
    payload = b"hello fantome\n"
    target = tmp_path / "payload"
    target.write_bytes(payload)
    expected = hashlib.sha1(
        b"blob %d\0%s" % (len(payload), payload), usedforsecurity=False
    ).hexdigest()
    assert git_blob_sha(target) == expected


def test_git_blob_sha_streams_content_larger_than_one_chunk(tmp_path: Path) -> None:
    payload = os.urandom(3 * 1024 * 1024)
    target = tmp_path / "large"
    target.write_bytes(payload)
    expected = hashlib.sha1(
        b"blob %d\0%s" % (len(payload), payload), usedforsecurity=False
    ).hexdigest()
    assert git_blob_sha(target) == expected


def test_sha256_file_matches_hashlib(tmp_path: Path) -> None:
    payload = os.urandom(2 * 1024 * 1024 + 17)
    target = tmp_path / "blob"
    target.write_bytes(payload)
    assert sha256_file(target) == hashlib.sha256(payload).hexdigest()


def test_is_real_file_accepts_a_regular_file(tmp_path: Path) -> None:
    target = tmp_path / "regular"
    target.write_text("x", encoding="utf-8")
    assert is_real_file(target)
    assert not is_real_directory(target)


def test_is_real_directory_accepts_a_regular_directory(tmp_path: Path) -> None:
    target = tmp_path / "dir"
    target.mkdir()
    assert is_real_directory(target)
    assert not is_real_file(target)


def test_missing_paths_are_neither_file_nor_directory(tmp_path: Path) -> None:
    missing = tmp_path / "absent"
    assert not is_real_file(missing)
    assert not is_real_directory(missing)


def test_plain_stat_is_not_reported_as_a_reparse_point(tmp_path: Path) -> None:
    target = tmp_path / "regular"
    target.write_text("x", encoding="utf-8")
    assert not is_reparse_stat(os.stat(target, follow_symlinks=False))


@pytest.mark.skipif(os.name != "nt", reason="reparse points are a Windows concept")
def test_a_directory_junction_is_rejected(tmp_path: Path) -> None:
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    try:
        os.symlink(real, link, target_is_directory=True)
    except (OSError, NotImplementedError, AttributeError):  # pragma: no cover
        pytest.skip("creating symbolic links requires privileges this session lacks")
    assert is_reparse_stat(os.stat(link, follow_symlinks=False))
    assert not is_real_directory(link)
