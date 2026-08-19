"""Tests for wiping and reseeding LTK's skin library.

These encode the contract measured against LTK Manager v1.13.0: the write
surface is exactly two directories, and everything else under LTK's data root
is left for LTK to repair on its next start.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from league_skin_manager.seed import ARCHIVES, MODS, SeedError, seed_library


def make_storage(root: Path, *, archives: int = 0, mods: int = 0) -> Path:
    """Build an LTK-shaped storage directory with existing content."""

    (root / ARCHIVES).mkdir(parents=True, exist_ok=True)
    (root / MODS).mkdir(parents=True, exist_ok=True)
    for index in range(archives):
        (root / ARCHIVES / f"existing-{index}.fantome").write_bytes(b"old")
    for index in range(mods):
        mod = root / MODS / f"existing-{index}"
        mod.mkdir()
        (mod / "mod.config.json").write_text("{}", encoding="utf-8")
    return root


def make_packages(root: Path, count: int) -> list[Path]:
    root.mkdir(parents=True, exist_ok=True)
    packages = []
    for index in range(count):
        target = root / f"{index:040d}.fantome"
        target.write_bytes(b"package-%d" % index)
        packages.append(target)
    return packages


def test_seeding_an_empty_storage(tmp_path: Path) -> None:
    storage = make_storage(tmp_path / "ltk")
    packages = make_packages(tmp_path / "cache", 5)

    result = seed_library(storage, packages)

    assert result.seeded == 5
    assert result.removed_archives == 0
    assert len(list((storage / ARCHIVES).iterdir())) == 5


def test_existing_library_is_wiped_before_seeding(tmp_path: Path) -> None:
    storage = make_storage(tmp_path / "ltk", archives=12, mods=12)
    packages = make_packages(tmp_path / "cache", 3)

    result = seed_library(storage, packages)

    assert result.removed_archives == 12
    assert result.removed_mods == 12
    assert result.seeded == 3
    assert len(list((storage / ARCHIVES).iterdir())) == 3
    assert list((storage / MODS).iterdir()) == []


def test_hand_imported_packages_are_removed_too(tmp_path: Path) -> None:
    """The wipe is indiscriminate; a clean base is the point."""

    storage = make_storage(tmp_path / "ltk")
    (storage / ARCHIVES / "my-own-import.fantome").write_bytes(b"mine")
    packages = make_packages(tmp_path / "cache", 2)

    seed_library(storage, packages)

    names = {entry.name for entry in (storage / ARCHIVES).iterdir()}
    assert "my-own-import.fantome" not in names
    assert len(names) == 2


def test_ltk_owned_files_are_never_touched(tmp_path: Path) -> None:
    """library.json, wad-reports.json, settings.json and profiles/ are LTK's."""

    storage = make_storage(tmp_path / "ltk", archives=4, mods=4)
    library = storage / "library.json"
    library.write_text(json.dumps({"mods": [{"id": "stale"}], "version": 1}), encoding="utf-8")
    settings = storage / "settings.json"
    settings.write_text(json.dumps({"leaguePath": "C:/Games"}), encoding="utf-8")
    reports = storage / "wad-reports.json"
    reports.write_text(json.dumps({"reports": {}}), encoding="utf-8")
    profiles = storage / "profiles" / "default"
    profiles.mkdir(parents=True)
    (profiles / "overlay.json").write_text("{}", encoding="utf-8")

    seed_library(storage, make_packages(tmp_path / "cache", 2))

    assert json.loads(library.read_text(encoding="utf-8"))["mods"] == [{"id": "stale"}]
    assert json.loads(settings.read_text(encoding="utf-8"))["leaguePath"] == "C:/Games"
    assert json.loads(reports.read_text(encoding="utf-8")) == {"reports": {}}
    assert (profiles / "overlay.json").is_file()


def test_seeded_files_carry_no_part_suffix(tmp_path: Path) -> None:
    """A half-copied file must never look adoptable to LTK's watcher."""

    storage = make_storage(tmp_path / "ltk")
    seed_library(storage, make_packages(tmp_path / "cache", 4))

    names = [entry.name for entry in (storage / ARCHIVES).iterdir()]
    assert all(name.endswith(".fantome") for name in names)
    assert not any(name.endswith(".part") for name in names)


def test_seeded_content_matches_the_source(tmp_path: Path) -> None:
    storage = make_storage(tmp_path / "ltk")
    packages = make_packages(tmp_path / "cache", 3)

    seed_library(storage, packages)

    seeded = sorted((storage / ARCHIVES).iterdir())
    assert [entry.read_bytes() for entry in seeded] == [p.read_bytes() for p in packages]


def test_seeded_names_are_unique(tmp_path: Path) -> None:
    storage = make_storage(tmp_path / "ltk")
    seed_library(storage, make_packages(tmp_path / "cache", 50))
    names = [entry.name for entry in (storage / ARCHIVES).iterdir()]
    assert len(names) == len(set(names)) == 50


def test_missing_directories_are_created(tmp_path: Path) -> None:
    storage = tmp_path / "never-run"
    result = seed_library(storage, make_packages(tmp_path / "cache", 2))
    assert result.seeded == 2
    assert (storage / ARCHIVES).is_dir()
    assert (storage / MODS).is_dir()


def test_seeding_nothing_still_wipes(tmp_path: Path) -> None:
    storage = make_storage(tmp_path / "ltk", archives=7, mods=7)
    result = seed_library(storage, [])
    assert result.removed_archives == 7
    assert result.seeded == 0
    assert list((storage / ARCHIVES).iterdir()) == []


def test_a_file_where_storage_should_be_is_rejected(tmp_path: Path) -> None:
    storage = tmp_path / "not-a-dir"
    storage.write_text("i am a file", encoding="utf-8")
    with pytest.raises(SeedError):
        seed_library(storage, make_packages(tmp_path / "cache", 1))


def test_a_missing_source_package_raises(tmp_path: Path) -> None:
    storage = make_storage(tmp_path / "ltk")
    with pytest.raises(SeedError, match="stage package"):
        seed_library(storage, [tmp_path / "cache" / "absent.fantome"])


def test_reseeding_is_idempotent(tmp_path: Path) -> None:
    """A second pass over the same set converges to the same library."""

    storage = make_storage(tmp_path / "ltk")
    packages = make_packages(tmp_path / "cache", 6)

    seed_library(storage, packages)
    first = sorted(entry.name for entry in (storage / ARCHIVES).iterdir())
    second_result = seed_library(storage, packages)
    second = sorted(entry.name for entry in (storage / ARCHIVES).iterdir())

    assert first == second
    assert second_result.removed_archives == 6
    assert second_result.seeded == 6
