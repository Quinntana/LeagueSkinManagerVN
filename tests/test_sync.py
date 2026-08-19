"""Tests for the synchronization use case."""

from __future__ import annotations

import json
import zipfile
from pathlib import Path
from typing import Any

import pytest

from league_skin_manager.cache import PackageCache
from league_skin_manager.github import SkinAsset, SkinManifest
from league_skin_manager.hashing import git_blob_sha
from league_skin_manager.seed import ARCHIVES
from league_skin_manager.settings import Settings
from league_skin_manager.sync import SyncOutcome, synchronize

COMMIT = "a" * 40
NEXT_COMMIT = "b" * 40

GOOD_METADATA = {
    "Name": "Test Skin",
    "Author": "Tester",
    "Version": "1.0.0",
    "Description": "For tests.",
}


def build_package(
    path: Path, *, metadata: dict[str, Any] | None = None, payload: bytes = b"wad"
) -> Path:
    """Build a package whose bytes are unique per payload.

    Real skins differ in content, so they differ in blob SHA. Identical
    fixtures would collapse to one cache entry and hide real behaviour.
    """

    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("META/info.json", json.dumps(metadata or GOOD_METADATA))
        archive.writestr("WAD/Test.wad.client", b"\x00" + payload)
    return path


class FakeSource:
    """A source backed by real .fantome files on disk."""

    def __init__(self, tmp_path: Path, count: int, *, commit: str = COMMIT, broken: int = 0):
        self.commit = commit
        self.origin = tmp_path / "origin"
        self.origin.mkdir(parents=True, exist_ok=True)
        self.downloads: list[str] = []
        self.assets: list[SkinAsset] = []
        for index in range(count):
            path = self.origin / f"skin{index}.fantome"
            metadata = dict(GOOD_METADATA, Name=f"Skin {index}")
            if index < broken:
                metadata.pop("Description")
            build_package(path, metadata=metadata, payload=f"wad-{index}".encode())
            self.assets.append(
                SkinAsset(
                    champion=f"Champ{index}",
                    name=f"Skin{index}",
                    path=f"skins/Champ{index}/Skin{index}.fantome",
                    size=path.stat().st_size,
                    sha=git_blob_sha(path),
                )
            )

    def head_commit(self) -> str:
        return self.commit

    def fetch_manifest(self, commit: str | None = None) -> SkinManifest:
        return SkinManifest(commit or self.commit, "16.15.1", tuple(self.assets))

    def download(
        self, asset: SkinAsset, destination: Path, commit: str, cancel: Any = None
    ) -> None:
        self.downloads.append(asset.path)
        source = self.origin / f"{asset.name.lower()}.fantome"
        destination.write_bytes(source.read_bytes())


def run(tmp_path: Path, source: FakeSource, settings: Settings | None = None) -> Any:
    cache = PackageCache(tmp_path / "cache")
    storage = tmp_path / "ltk"
    return synchronize(
        source=source,
        cache=cache,
        settings=settings or Settings(),
        storage_dir=storage,
        workers=2,
    )


def test_a_first_sync_downloads_and_seeds(tmp_path: Path) -> None:
    source = FakeSource(tmp_path, 5)
    result, updated = run(tmp_path, source)

    assert result.outcome == SyncOutcome.UPDATED
    assert result.seeded == 5
    assert result.downloaded == 5
    assert updated.commit == COMMIT
    assert updated.patch == "16.15.1"
    assert updated.skins == 5
    assert len(list((tmp_path / "ltk" / ARCHIVES).iterdir())) == 5


def test_an_unchanged_commit_does_nothing(tmp_path: Path) -> None:
    """The common launch: one request, then stop."""

    source = FakeSource(tmp_path, 5)
    settings = Settings(commit=COMMIT, patch="16.15.1", skins=5)

    result, updated = run(tmp_path, source, settings)

    assert result.outcome == SyncOutcome.UP_TO_DATE
    assert source.downloads == []
    assert updated is settings
    assert not (tmp_path / "ltk").exists()


def test_the_commit_marker_is_only_returned_on_success(tmp_path: Path) -> None:
    source = FakeSource(tmp_path, 3)
    _result, updated = run(tmp_path, source)
    assert updated.commit == COMMIT
    assert updated.synced_at is not None


def test_a_second_sync_reuses_the_cache(tmp_path: Path) -> None:
    source = FakeSource(tmp_path, 4)
    _first, updated = run(tmp_path, source)
    downloads_after_first = len(source.downloads)

    source.commit = NEXT_COMMIT
    result, _ = run(tmp_path, source, updated)

    assert downloads_after_first == 4
    assert len(source.downloads) == 4, "nothing should be re-downloaded"
    assert result.downloaded == 0
    assert result.seeded == 4


def test_invalid_packages_are_rejected_and_not_seeded(tmp_path: Path) -> None:
    """Mirrors the 5 real upstream packages missing Description."""

    source = FakeSource(tmp_path, 6, broken=2)
    result, updated = run(tmp_path, source)

    assert result.rejected == 2
    assert result.seeded == 4
    assert updated.skins == 4
    assert len(list((tmp_path / "ltk" / ARCHIVES).iterdir())) == 4


def test_the_library_is_wiped_before_reseeding(tmp_path: Path) -> None:
    source = FakeSource(tmp_path, 3)
    archives = tmp_path / "ltk" / ARCHIVES
    archives.mkdir(parents=True)
    (archives / "leftover.fantome").write_bytes(b"stale")

    run(tmp_path, source)

    names = {entry.name for entry in archives.iterdir()}
    assert "leftover.fantome" not in names
    assert len(names) == 3


def test_the_cache_is_pruned_of_dropped_skins(tmp_path: Path) -> None:
    source = FakeSource(tmp_path, 5)
    _first, updated = run(tmp_path, source)
    cache_dir = tmp_path / "cache"
    assert len(list(cache_dir.iterdir())) == 5

    source.assets = source.assets[:2]
    source.commit = NEXT_COMMIT
    run(tmp_path, source, updated)

    assert len(list(cache_dir.iterdir())) == 2


def test_a_sync_with_nothing_usable_raises(tmp_path: Path) -> None:
    source = FakeSource(tmp_path, 3, broken=3)
    with pytest.raises(RuntimeError, match="No usable skin packages"):
        run(tmp_path, source)


def test_progress_is_reported_per_download(tmp_path: Path) -> None:
    source = FakeSource(tmp_path, 4)
    seen: list[tuple[int, int]] = []
    synchronize(
        source=source,
        cache=PackageCache(tmp_path / "cache"),
        settings=Settings(),
        storage_dir=tmp_path / "ltk",
        workers=1,
        on_progress=lambda done, total: seen.append((done, total)),
    )
    assert seen == [(1, 4), (2, 4), (3, 4), (4, 4)]


def test_a_repeat_sync_converges(tmp_path: Path) -> None:
    """Wipe-and-reseed is idempotent from any starting state."""

    source = FakeSource(tmp_path, 4)
    _first, updated = run(tmp_path, source)
    first = sorted(entry.name for entry in (tmp_path / "ltk" / ARCHIVES).iterdir())

    source.commit = NEXT_COMMIT
    run(tmp_path, source, updated)
    second = sorted(entry.name for entry in (tmp_path / "ltk" / ARCHIVES).iterdir())

    assert first == second
