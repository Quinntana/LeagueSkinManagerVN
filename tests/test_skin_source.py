from __future__ import annotations

import hashlib
import threading
from dataclasses import FrozenInstanceError
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit

import pytest
import requests

from league_skin_manager.skin_source import (
    MAXIMUM_ASSET_BYTES,
    AssetDownloadError,
    AssetIntegrityError,
    DownloadCancelledError,
    GitHubSkinSource,
    ManifestValidationError,
    SkinAsset,
    SkinManifest,
)

COMMIT_SHA = "a" * 40


def blob_sha(content: bytes) -> str:
    digest = hashlib.sha1()  # noqa: S324 - reproduces Git's blob object ID
    digest.update(b"blob " + str(len(content)).encode("ascii") + b"\0" + content)
    return digest.hexdigest()


class FakeResponse:
    def __init__(
        self,
        *,
        payload: Any = None,
        chunks: list[bytes] | None = None,
        status_code: int = 200,
    ) -> None:
        self.payload = payload
        self.chunks = chunks or []
        self.status_code = status_code
        self.closed = False

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise requests.HTTPError(f"status {self.status_code}")

    def json(self) -> Any:
        return self.payload

    def iter_content(self, chunk_size: int) -> Any:
        assert chunk_size > 0
        return iter(self.chunks)

    def close(self) -> None:
        self.closed = True


class FakeSession:
    def __init__(self, responses: list[Any]) -> None:
        self.responses = list(responses)
        self.calls = []
        self.closed = False

    def get(self, url: str, **kwargs: Any) -> FakeResponse:
        self.calls.append((url, kwargs))
        result = self.responses.pop(0)
        if isinstance(result, BaseException):
            raise result
        return result

    def close(self) -> None:
        self.closed = True


def ref_response() -> FakeResponse:
    return FakeResponse(payload={"object": {"type": "commit", "sha": COMMIT_SHA}})


def tree_entry(path: str, content: bytes = b"skin") -> dict:
    return {
        "path": path,
        "type": "blob",
        "size": len(content),
        "sha": blob_sha(content),
    }


def test_manifest_filters_exact_base_paths_and_encodes_raw_urls() -> None:
    valid_one = "skins/Kai'Sa/Arcade Kai'Sa & Prestige.fantome"
    valid_two = "skins/Bel'Veth/Café Bel'Veth.fantome"
    session = FakeSession(
        [
            ref_response(),
            FakeResponse(
                payload={
                    "truncated": False,
                    "tree": [
                        tree_entry(valid_one),
                        tree_entry("skins/Aatrox/Mecha Aatrox/Obsidian.fantome"),
                        tree_entry("skins/Aatrox/Mecha Aatrox.zip"),
                        tree_entry("wards/Aatrox.fantome"),
                        tree_entry("skins/Aatrox/Uppercase.FANTOME"),
                        tree_entry(valid_two),
                        {"path": "skins", "type": "tree", "sha": "b" * 40},
                    ],
                }
            ),
            FakeResponse(payload={"patch": "16.13.1", "champions": {}}),
        ]
    )
    source = GitHubSkinSource(session=session, sleeper=lambda _seconds: None)

    manifest = source.fetch_manifest(validate_sanity=False)

    assert tuple(manifest.__dataclass_fields__) == ("commit", "patch", "assets")
    assert tuple(manifest.assets[0].__dataclass_fields__) == (
        "champion",
        "name",
        "path",
        "size",
        "sha",
    )
    assert manifest.commit == COMMIT_SHA
    assert manifest.patch == "16.13.1"
    assert [asset.path for asset in manifest.assets] == [valid_two, valid_one]
    assert manifest.champions == ("Bel'Veth", "Kai'Sa")
    assert manifest.assets_for("kai'sa")[0].name == "Arcade Kai'Sa & Prestige"
    assert session.calls[1][0].endswith(COMMIT_SHA + "?recursive=1")
    assert session.calls[2][0].endswith("/" + COMMIT_SHA + "/index.json")

    with pytest.raises(FrozenInstanceError):
        manifest.assets[0].name = "changed"  # type: ignore[misc]


def test_default_manifest_sanity_rejects_a_partial_repository() -> None:
    session = FakeSession(
        [
            ref_response(),
            FakeResponse(
                payload={
                    "truncated": False,
                    "tree": [tree_entry("skins/Aatrox/Justicar Aatrox.fantome")],
                }
            ),
        ]
    )
    source = GitHubSkinSource(session=session, sleeper=lambda _seconds: None)

    with pytest.raises(ManifestValidationError, match="only 1 base skins"):
        source.fetch_manifest(include_patch=False)


def test_manifest_and_direct_download_enforce_compressed_resource_limits() -> None:
    oversized = SkinAsset(
        champion="Aatrox",
        name="Too Large",
        path="skins/Aatrox/Too Large.fantome",
        size=MAXIMUM_ASSET_BYTES + 1,
        sha="b" * 40,
    )
    manifest = SkinManifest(commit=COMMIT_SHA, patch="16.13.1", assets=(oversized,))

    with pytest.raises(ManifestValidationError, match="compressed bytes"):
        manifest.validate_sanity(minimum_assets=1, minimum_champions=1)
    with pytest.raises(ManifestValidationError, match="aggregate compressed"):
        manifest.validate_sanity(
            minimum_assets=1,
            minimum_champions=1,
            maximum_asset_bytes=oversized.size,
            maximum_manifest_bytes=oversized.size - 1,
        )

    source = GitHubSkinSource(session=FakeSession([]), sleeper=lambda _seconds: None)
    with pytest.raises(AssetDownloadError, match="safety limit"):
        source.download(oversized, Path("unused.fantome"))


def test_truncated_recursive_tree_is_always_rejected() -> None:
    session = FakeSession([ref_response(), FakeResponse(payload={"truncated": True, "tree": []})])
    source = GitHubSkinSource(session=session, sleeper=lambda _seconds: None)

    with pytest.raises(ManifestValidationError, match="truncated"):
        source.fetch_manifest(include_patch=False, validate_sanity=False)


def test_optional_index_failure_does_not_discard_tree_manifest() -> None:
    delays = []
    session = FakeSession(
        [
            ref_response(),
            FakeResponse(
                payload={
                    "truncated": False,
                    "tree": [tree_entry("skins/Aatrox/Justicar Aatrox.fantome")],
                }
            ),
            requests.ConnectionError("offline"),
            requests.ConnectionError("still offline"),
        ]
    )
    source = GitHubSkinSource(
        session=session,
        sleeper=delays.append,
        attempts=2,
        backoff_seconds=0.25,
    )

    manifest = source.fetch_manifest(validate_sanity=False)

    assert manifest.patch is None
    assert len(manifest.assets) == 1
    assert delays == [0.25]


def test_download_retries_short_content_then_atomically_replaces_file(tmp_path: Path) -> None:
    content = b"verified fantome bytes"
    path = "skins/Kai'Sa/Arcade Kai'Sa & Prestige.fantome"
    destination = tmp_path / "Justicar Aatrox.fantome"
    destination.write_bytes(b"existing-good-file")
    delays = []
    progress_updates = []
    session = FakeSession(
        [
            ref_response(),
            FakeResponse(payload={"truncated": False, "tree": [tree_entry(path, content)]}),
            FakeResponse(chunks=[content[:-2]]),
            FakeResponse(chunks=[content[:8], b"", content[8:]]),
        ]
    )
    source = GitHubSkinSource(
        session=session,
        sleeper=delays.append,
        attempts=2,
        backoff_seconds=0.5,
    )
    manifest = source.fetch_manifest(include_patch=False, validate_sanity=False)
    asset = manifest.assets[0]

    result = source.download(
        asset,
        destination,
        progress=lambda done, total: progress_updates.append((done, total)),
    )

    assert result == destination
    assert destination.read_bytes() == content
    assert delays == [0.5]
    assert len(session.calls) == 4
    assert all(call[1]["stream"] is True for call in session.calls[2:])
    encoded_url = session.calls[2][0]
    assert "%20" in encoded_url
    assert "%26" in encoded_url
    assert unquote(urlsplit(encoded_url).path).endswith("/" + path)
    assert progress_updates == [
        (0, len(content)),
        (len(content) - 2, len(content)),
        (0, len(content)),
        (8, len(content)),
        (len(content), len(content)),
    ]
    assert list(tmp_path.glob("*.part")) == []
    assert list(tmp_path.glob(".*.part")) == []


def test_download_hash_failure_preserves_existing_destination(tmp_path: Path) -> None:
    expected = b"expected"
    corrupt = b"corrupt!"
    path = "skins/Aatrox/Justicar Aatrox.fantome"
    destination = tmp_path / "skin.fantome"
    destination.write_bytes(b"keep me")
    session = FakeSession(
        [
            ref_response(),
            FakeResponse(payload={"truncated": False, "tree": [tree_entry(path, expected)]}),
            FakeResponse(chunks=[corrupt]),
        ]
    )
    source = GitHubSkinSource(
        session=session,
        sleeper=lambda _seconds: None,
        attempts=1,
    )
    asset = source.fetch_manifest(include_patch=False, validate_sanity=False).assets[0]

    with pytest.raises(AssetIntegrityError, match="Git blob SHA"):
        source.download(asset, destination)

    assert destination.read_bytes() == b"keep me"
    assert list(tmp_path.iterdir()) == [destination]


def test_download_cancellation_is_immediate_and_atomic(tmp_path: Path) -> None:
    content = b"first-second"
    path = "skins/Aatrox/Justicar Aatrox.fantome"
    session = FakeSession(
        [
            ref_response(),
            FakeResponse(payload={"truncated": False, "tree": [tree_entry(path, content)]}),
            FakeResponse(chunks=[b"first-", b"second"]),
        ]
    )
    source = GitHubSkinSource(session=session, sleeper=lambda _seconds: None, attempts=3)
    asset = source.fetch_manifest(include_patch=False, validate_sanity=False).assets[0]
    destination = tmp_path / "skin.fantome"
    destination.write_bytes(b"existing")
    cancelled = threading.Event()

    def cancel_after_first_chunk(done: int, _total: int) -> None:
        if done:
            cancelled.set()

    with pytest.raises(DownloadCancelledError):
        source.download(
            asset,
            destination,
            cancel_event=cancelled,
            progress=cancel_after_first_chunk,
        )

    assert destination.read_bytes() == b"existing"
    assert len(session.calls) == 3
    assert list(tmp_path.iterdir()) == [destination]


def test_owned_http_sessions_are_thread_local_and_closed_after_workers() -> None:
    created: list[FakeSession] = []
    created_lock = threading.Lock()

    def factory() -> FakeSession:
        session = FakeSession([])
        with created_lock:
            created.append(session)
        return session

    source = GitHubSkinSource(session_factory=factory)
    barrier = threading.Barrier(3)
    observations: list[tuple[FakeSession, FakeSession]] = []

    def worker() -> None:
        first = source._session()
        barrier.wait()
        second = source._session()
        with created_lock:
            observations.append((first, second))

    threads = [threading.Thread(target=worker) for _ in range(3)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len(created) == 3
    assert len({id(first) for first, _second in observations}) == 3
    assert all(first is second for first, second in observations)
    source.close()
    assert all(session.closed for session in created)


def test_injected_http_session_remains_caller_owned() -> None:
    session = FakeSession([])
    source = GitHubSkinSource(session=session)

    assert source._session() is session
    source.close()
    assert not session.closed
