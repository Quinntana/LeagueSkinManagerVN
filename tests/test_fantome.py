"""Tests for fantome package validation."""

from __future__ import annotations

import json
import stat
import zipfile
from pathlib import Path

import pytest

from league_skin_manager.fantome import (
    FantomeIntegrityError,
    FantomeStructureError,
    UnsafeFantomeError,
    validate_fantome,
    verify_artifact,
)
from league_skin_manager.hashing import git_blob_sha, sha256_file

GOOD_METADATA = {
    "Name": "Bewitching Senna",
    "Author": "Sunshine Builder",
    "Version": "1.0.0",
    "Description": "Auto-built by Sunshine Builder.",
}


def build_fantome(
    path: Path,
    *,
    metadata: dict[str, object] | None | str = None,
    wad: bool = True,
    extra: dict[str, bytes] | None = None,
) -> Path:
    """Write a minimal but structurally valid fantome, with opt-out defects."""

    with zipfile.ZipFile(path, "w") as archive:
        if metadata is not None or metadata == "":
            payload = metadata if isinstance(metadata, str) else json.dumps(metadata)
            archive.writestr("META/info.json", payload)
        if wad:
            archive.writestr("WAD/Senna.wad.client", b"\x00wad-bytes")
        for name, blob in (extra or {}).items():
            archive.writestr(name, blob)
    return path


def test_a_well_formed_package_validates(tmp_path: Path) -> None:
    target = build_fantome(tmp_path / "ok.fantome", metadata=GOOD_METADATA)
    result = validate_fantome(target)
    assert result.name == "Bewitching Senna"
    assert result.author == "Sunshine Builder"
    assert result.version == "1.0.0"
    assert result.wad_files == ("WAD/Senna.wad.client",)


@pytest.mark.parametrize("field", ["Name", "Author", "Version", "Description"])
def test_each_required_metadata_field_is_enforced(tmp_path: Path, field: str) -> None:
    metadata = {key: value for key, value in GOOD_METADATA.items() if key != field}
    target = build_fantome(tmp_path / "missing.fantome", metadata=metadata)
    with pytest.raises(FantomeStructureError, match=field):
        validate_fantome(target)


def test_a_non_string_metadata_value_is_rejected(tmp_path: Path) -> None:
    metadata = dict(GOOD_METADATA) | {"Version": 1.0}
    target = build_fantome(tmp_path / "typed.fantome", metadata=metadata)
    with pytest.raises(FantomeStructureError, match="Version"):
        validate_fantome(target)


def test_missing_metadata_member_is_rejected(tmp_path: Path) -> None:
    target = build_fantome(tmp_path / "nometa.fantome", metadata=None)
    with pytest.raises(FantomeStructureError, match="META/info.json"):
        validate_fantome(target)


def test_malformed_metadata_json_is_rejected(tmp_path: Path) -> None:
    target = build_fantome(tmp_path / "badjson.fantome", metadata="{not json")
    with pytest.raises(FantomeStructureError, match="valid UTF-8 JSON"):
        validate_fantome(target)


def test_metadata_that_is_not_an_object_is_rejected(tmp_path: Path) -> None:
    target = build_fantome(tmp_path / "list.fantome", metadata="[1, 2, 3]")
    with pytest.raises(FantomeStructureError, match="JSON object"):
        validate_fantome(target)


def test_a_package_without_a_wad_is_rejected(tmp_path: Path) -> None:
    target = build_fantome(tmp_path / "nowad.fantome", metadata=GOOD_METADATA, wad=False)
    with pytest.raises(FantomeStructureError, match="WAD"):
        validate_fantome(target)


def test_an_empty_archive_is_rejected(tmp_path: Path) -> None:
    target = tmp_path / "empty.fantome"
    with zipfile.ZipFile(target, "w"):
        pass
    with pytest.raises(FantomeStructureError, match="empty"):
        validate_fantome(target)


def test_a_non_zip_file_is_rejected(tmp_path: Path) -> None:
    target = tmp_path / "text.fantome"
    target.write_bytes(b"this is not a zip archive")
    with pytest.raises(FantomeStructureError, match="not a valid ZIP"):
        validate_fantome(target)


@pytest.mark.parametrize(
    "member",
    [
        "../escape.txt",
        "WAD/../../escape.txt",
        "/absolute.txt",
        "C:/drive.txt",
        "CON",
        "NUL.txt",
        "trailing ",
        "trailing.",
    ],
)
def test_unsafe_member_paths_are_rejected(tmp_path: Path, member: str) -> None:
    target = build_fantome(
        tmp_path / "unsafe.fantome", metadata=GOOD_METADATA, extra={member: b"x"}
    )
    with pytest.raises(UnsafeFantomeError):
        validate_fantome(target)


def test_backslash_traversal_is_rejected(tmp_path: Path) -> None:
    target = tmp_path / "backslash.fantome"
    with zipfile.ZipFile(target, "w") as archive:
        archive.writestr("META/info.json", json.dumps(GOOD_METADATA))
        archive.writestr("WAD/Senna.wad.client", b"\x00")
        archive.writestr("..\\escape.txt", b"x")
    with pytest.raises(UnsafeFantomeError, match="traversal"):
        validate_fantome(target)


def test_a_symlink_member_is_rejected(tmp_path: Path) -> None:
    target = tmp_path / "symlink.fantome"
    with zipfile.ZipFile(target, "w") as archive:
        archive.writestr("META/info.json", json.dumps(GOOD_METADATA))
        archive.writestr("WAD/Senna.wad.client", b"\x00")
        info = zipfile.ZipInfo("WAD/link.wad.client")
        info.external_attr = (stat.S_IFLNK | 0o777) << 16
        archive.writestr(info, "target")
    with pytest.raises(UnsafeFantomeError, match="symlink"):
        validate_fantome(target)


def test_duplicate_windows_paths_are_rejected(tmp_path: Path) -> None:
    target = tmp_path / "dupe.fantome"
    with zipfile.ZipFile(target, "w") as archive:
        archive.writestr("META/info.json", json.dumps(GOOD_METADATA))
        archive.writestr("WAD/Senna.wad.client", b"\x00")
        archive.writestr("WAD/SENNA.WAD.CLIENT", b"\x00")
    with pytest.raises(UnsafeFantomeError, match="duplicate"):
        validate_fantome(target)


def test_size_mismatch_is_rejected(tmp_path: Path) -> None:
    target = build_fantome(tmp_path / "size.fantome", metadata=GOOD_METADATA)
    with pytest.raises(FantomeIntegrityError, match="size mismatch"):
        validate_fantome(target, expected_size=target.stat().st_size + 1)


def test_a_matching_git_blob_sha_is_accepted(tmp_path: Path) -> None:
    target = build_fantome(tmp_path / "sha.fantome", metadata=GOOD_METADATA)
    validate_fantome(target, expected_size=target.stat().st_size, expected_sha=git_blob_sha(target))


def test_a_matching_sha256_is_accepted(tmp_path: Path) -> None:
    target = build_fantome(tmp_path / "sha256.fantome", metadata=GOOD_METADATA)
    validate_fantome(target, expected_sha=sha256_file(target))


def test_a_mismatched_digest_is_rejected(tmp_path: Path) -> None:
    target = build_fantome(tmp_path / "bad.fantome", metadata=GOOD_METADATA)
    with pytest.raises(FantomeIntegrityError, match="digest mismatch"):
        validate_fantome(target, expected_sha="0" * 40)


def test_a_digest_of_unknown_length_is_rejected(tmp_path: Path) -> None:
    target = build_fantome(tmp_path / "len.fantome", metadata=GOOD_METADATA)
    with pytest.raises(FantomeIntegrityError, match="Git blob SHA-1 or SHA-256"):
        validate_fantome(target, expected_sha="abc123")


def test_a_non_hexadecimal_digest_is_rejected(tmp_path: Path) -> None:
    target = build_fantome(tmp_path / "hex.fantome", metadata=GOOD_METADATA)
    with pytest.raises(FantomeIntegrityError, match="hexadecimal"):
        validate_fantome(target, expected_sha="z" * 40)


def test_verify_artifact_rejects_a_missing_file(tmp_path: Path) -> None:
    with pytest.raises(FantomeIntegrityError, match="does not exist"):
        verify_artifact(tmp_path / "absent", expected_size=None, expected_sha=None)
