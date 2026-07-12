from __future__ import annotations

import hashlib
import json
import stat
import threading
import zipfile
from pathlib import Path

import pytest

from league_skin_manager import skin_installer as installer_module
from league_skin_manager.skin_installer import (
    ExtractionCancelled,
    FantomeIntegrityError,
    FantomeStructureError,
    UnsafeFantomeError,
    extract_fantome,
    git_blob_sha,
    inspect_extracted_mod,
    managed_directory_name,
    sha256_file,
    validate_extracted_mod,
    validate_fantome,
)


def create_fantome(
    path: Path,
    *,
    extra_entries: dict[str, bytes] | None = None,
    include_metadata: bool = True,
    include_wad: bool = True,
) -> Path:
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        if include_metadata:
            archive.writestr("META/info.json", json.dumps({"Name": "Test skin"}))
        if include_wad:
            archive.writestr("WAD/Test.wad.client", b"wad-data")
        for name, value in (extra_entries or {}).items():
            archive.writestr(name, value)
    return path


def test_validate_and_extract_fantome(tmp_path: Path) -> None:
    archive = create_fantome(tmp_path / "skin.fantome")
    digest = git_blob_sha(archive)

    validated = validate_fantome(
        archive,
        expected_size=archive.stat().st_size,
        expected_sha=digest,
    )
    destination = tmp_path / "installed"
    extract_fantome(
        archive,
        destination,
        expected_size=archive.stat().st_size,
        expected_sha=digest,
    )

    assert validated.wad_files == ("WAD/Test.wad.client",)
    assert (destination / "WAD" / "Test.wad.client").read_bytes() == b"wad-data"
    assert validate_extracted_mod(destination)


def test_accepts_sha256_and_rejects_bad_integrity_metadata(tmp_path: Path) -> None:
    archive = create_fantome(tmp_path / "skin.fantome")
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()

    assert sha256_file(archive) == digest
    validate_fantome(archive, expected_sha=digest)
    with pytest.raises(FantomeIntegrityError, match="size mismatch"):
        validate_fantome(archive, expected_size=archive.stat().st_size + 1)
    with pytest.raises(FantomeIntegrityError, match="hexadecimal"):
        validate_fantome(archive, expected_sha="not-a-digest")
    with pytest.raises(FantomeIntegrityError, match="must be"):
        validate_fantome(archive, expected_sha="a" * 48)


def test_archive_limits_and_bad_zip_fail_closed(tmp_path: Path) -> None:
    archive = create_fantome(tmp_path / "skin.fantome")

    with pytest.raises(ValueError, match="limits"):
        validate_fantome(archive, max_members=0)
    with pytest.raises(UnsafeFantomeError, match="too many"):
        validate_fantome(archive, max_members=1)
    with pytest.raises(UnsafeFantomeError, match="uncompressed-size"):
        validate_fantome(archive, max_uncompressed_bytes=1)
    with pytest.raises(UnsafeFantomeError, match="compressed-size"):
        validate_fantome(archive, max_compressed_bytes=archive.stat().st_size - 1)

    not_zip = tmp_path / "not-a-zip.fantome"
    not_zip.write_bytes(b"not a zip")
    with pytest.raises(FantomeStructureError, match="valid ZIP"):
        validate_fantome(not_zip)


def test_extract_reports_progress_and_refuses_existing_destination(tmp_path: Path) -> None:
    archive = tmp_path / "skin.fantome"
    with zipfile.ZipFile(archive, "w") as output:
        output.writestr("META/", b"")
        output.writestr("META/info.json", "{}")
        output.writestr("WAD/", b"")
        output.writestr("WAD/Test.wad.client", b"wad")
    progress: list[tuple[int, int]] = []
    destination = tmp_path / "installed"

    extract_fantome(
        archive,
        destination,
        chunk_progress=lambda done, total: progress.append((done, total)),
    )

    assert progress[-1][0] == progress[-1][1]
    with pytest.raises(FileExistsError):
        extract_fantome(archive, destination)


@pytest.mark.parametrize(
    "unsafe_path",
    [
        "../outside.txt",
        "WAD/../../outside.txt",
        r"..\outside.txt",
        "C:/outside.txt",
        "WAD/file.txt:stream",
        "WAD/CON.txt",
    ],
)
def test_rejects_unsafe_archive_paths(tmp_path: Path, unsafe_path: str) -> None:
    archive = create_fantome(
        tmp_path / "unsafe.fantome",
        extra_entries={unsafe_path: b"unsafe"},
    )

    with pytest.raises(UnsafeFantomeError):
        validate_fantome(archive)

    assert not (tmp_path / "outside.txt").exists()


def test_rejects_symlink_member(tmp_path: Path) -> None:
    archive_path = tmp_path / "symlink.fantome"
    with zipfile.ZipFile(archive_path, "w") as archive:
        archive.writestr("META/info.json", "{}")
        archive.writestr("WAD/Test.wad.client", b"wad")
        link = zipfile.ZipInfo("WAD/link")
        link.create_system = 3
        link.external_attr = (stat.S_IFLNK | 0o777) << 16
        archive.writestr(link, "../outside")

    with pytest.raises(UnsafeFantomeError, match="symlink"):
        validate_fantome(archive_path)


@pytest.mark.parametrize(
    ("include_metadata", "include_wad", "message"),
    [
        (False, True, "META/info.json"),
        (True, False, "WAD"),
    ],
)
def test_requires_fantome_structure(
    tmp_path: Path,
    include_metadata: bool,
    include_wad: bool,
    message: str,
) -> None:
    archive = create_fantome(
        tmp_path / "invalid.fantome",
        include_metadata=include_metadata,
        include_wad=include_wad,
    )

    with pytest.raises(FantomeStructureError, match=message):
        validate_fantome(archive)


def test_rejects_invalid_metadata_and_digest(tmp_path: Path) -> None:
    archive = tmp_path / "invalid.fantome"
    with zipfile.ZipFile(archive, "w") as output:
        output.writestr("META/info.json", "not-json")
        output.writestr("WAD/Test.wad.client", b"wad")

    with pytest.raises(FantomeStructureError, match="JSON"):
        validate_fantome(archive)
    with pytest.raises(FantomeIntegrityError, match="digest mismatch"):
        validate_fantome(archive, expected_sha="0" * 40)


def test_rejects_oversized_metadata_before_reading_it(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    archive = create_fantome(tmp_path / "oversized-metadata.fantome")
    monkeypatch.setattr(installer_module, "DEFAULT_MAX_METADATA_BYTES", 1)

    with pytest.raises(UnsafeFantomeError, match="metadata"):
        validate_fantome(archive)


def test_cancelled_extraction_leaves_no_destination(tmp_path: Path) -> None:
    archive = create_fantome(tmp_path / "skin.fantome")
    cancelled = threading.Event()
    cancelled.set()
    destination = tmp_path / "cancelled"

    with pytest.raises(ExtractionCancelled):
        extract_fantome(archive, destination, cancel_event=cancelled)

    assert not destination.exists()


def test_managed_directory_name_is_stable_and_safe() -> None:
    first = managed_directory_name(
        "Kai'Sa",
        "K/DA ALL OUT: Prestige",
        "skins/Kai'Sa/K_DA ALL OUT Prestige.fantome",
    )
    second = managed_directory_name(
        "Kai'Sa",
        "K/DA ALL OUT: Prestige",
        "skins/Kai'Sa/K_DA ALL OUT Prestige.fantome",
    )

    assert first == second
    assert first.startswith("lsmvn--kai-sa--k-da-all-out-prestige--")
    assert "/" not in first and "\\" not in first and ":" not in first


def test_extracted_fingerprint_detects_same_shape_content_changes(tmp_path: Path) -> None:
    archive = create_fantome(tmp_path / "skin.fantome")
    destination = tmp_path / "installed"
    extract_fantome(archive, destination)

    original = inspect_extracted_mod(destination)
    assert original is not None
    wad = destination / "WAD" / "Test.wad.client"
    wad.write_bytes(b"evil-wad")
    changed = inspect_extracted_mod(destination)

    assert changed is not None
    assert changed.sha256 != original.sha256
    assert not validate_extracted_mod(destination, expected_sha256=original.sha256)


def test_extracted_fingerprint_rejects_reparse_points(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    archive = create_fantome(tmp_path / "skin.fantome")
    destination = tmp_path / "installed"
    extract_fantome(archive, destination)
    monkeypatch.setattr(installer_module, "_is_reparse_stat", lambda _value: True)

    assert inspect_extracted_mod(destination) is None
    assert not validate_extracted_mod(destination)
