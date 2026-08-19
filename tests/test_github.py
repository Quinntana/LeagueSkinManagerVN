"""Tests for the GitHub skin-source adapter."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

import pytest
import requests

from league_skin_manager.github import (
    DownloadCancelled,
    DownloadError,
    GitHubSkinSource,
    ManifestError,
    SkinAsset,
    SkinManifest,
    _base_skin_parts,
)

COMMIT = "a20cc5c71166557a26cc5a3446287be1b99650a5"


def blob_sha(payload: bytes) -> str:
    digest = hashlib.sha1(usedforsecurity=False)
    digest.update(b"blob %d\0" % len(payload))
    digest.update(payload)
    return digest.hexdigest()


class FakeResponse:
    def __init__(self, *, payload: Any = None, body: bytes = b"", status: int = 200) -> None:
        self._payload = payload
        self._body = body
        self.status = status
        self.closed = False

    def raise_for_status(self) -> None:
        if self.status >= 400:
            raise requests.HTTPError(f"status {self.status}")

    def json(self) -> Any:
        return self._payload

    def iter_content(self, chunk_size: int = 1) -> Any:
        for start in range(0, len(self._body), chunk_size):
            yield self._body[start : start + chunk_size]

    def close(self) -> None:
        self.closed = True


class FakeSession:
    """Routes by URL substring; records every request made."""

    def __init__(self, routes: dict[str, Any]) -> None:
        self.routes = routes
        self.requests: list[str] = []

    def get(self, url: str, **_kwargs: Any) -> Any:
        self.requests.append(url)
        for fragment, response in self.routes.items():
            if fragment in url:
                if isinstance(response, Exception):
                    raise response
                return response() if callable(response) else response
        raise requests.ConnectionError(f"no route for {url}")

    def close(self) -> None:
        pass


def tree_entry(path: str, payload: bytes) -> dict[str, Any]:
    return {"type": "blob", "path": path, "size": len(payload), "sha": blob_sha(payload)}


def production_tree(count: int = 1200, champions: int = 120) -> list[dict[str, Any]]:
    """A tree large enough to clear the manifest sanity floors."""

    entries = []
    for index in range(count):
        champion = f"Champion{index % champions}"
        entries.append(tree_entry(f"skins/{champion}/Skin{index}.fantome", b"x" * (index % 50 + 1)))
    return entries


def source_for(routes: dict[str, Any]) -> tuple[GitHubSkinSource, FakeSession]:
    session = FakeSession(routes)
    return (
        GitHubSkinSource(session_factory=lambda: session, sleeper=lambda _s: None),
        session,
    )


# --- path filtering -------------------------------------------------------


@pytest.mark.parametrize(
    "path,expected",
    [
        ("skins/Ahri/Foxfire Ahri.fantome", ("Ahri", "Foxfire Ahri")),
        ("skins/Lee Sin/Dragon Fist Lee Sin.fantome", ("Lee Sin", "Dragon Fist Lee Sin")),
        ("skins/Ahri/chromas/Foxfire Ahri Ruby.fantome", None),
        ("wards/Poro Ward.fantome", None),
        ("emotes/Laugh.fantome", None),
        ("skins/Ahri/readme.txt", None),
        ("skins/Ahri.fantome", None),
        ("index.json", None),
    ],
)
def test_only_base_skin_paths_are_selected(path: str, expected: tuple[str, str] | None) -> None:
    assert _base_skin_parts(path) == expected


# --- head commit ----------------------------------------------------------


def test_head_commit_is_read_from_the_ref(tmp_path: Path) -> None:
    source, session = source_for(
        {"git/ref/heads": FakeResponse(payload={"object": {"type": "commit", "sha": COMMIT}})}
    )
    assert source.head_commit() == COMMIT
    assert len(session.requests) == 1


def test_a_ref_pointing_at_a_tag_is_rejected() -> None:
    source, _ = source_for(
        {"git/ref/heads": FakeResponse(payload={"object": {"type": "tag", "sha": COMMIT}})}
    )
    with pytest.raises(ManifestError, match="commit"):
        source.head_commit()


def test_a_malformed_commit_sha_is_rejected() -> None:
    source, _ = source_for(
        {"git/ref/heads": FakeResponse(payload={"object": {"type": "commit", "sha": "nope"}})}
    )
    with pytest.raises(ManifestError, match="invalid commit SHA"):
        source.head_commit()


# --- manifest -------------------------------------------------------------


def test_a_production_shaped_tree_yields_a_manifest() -> None:
    source, _ = source_for(
        {
            "git/trees": FakeResponse(payload={"truncated": False, "tree": production_tree()}),
            "index.json": FakeResponse(payload={"patch": "16.15.1"}),
        }
    )
    result = source.fetch_manifest(commit=COMMIT)
    assert result.commit == COMMIT
    assert result.patch == "16.15.1"
    assert len(result.assets) == 1200
    assert len(result.champions) == 120


def test_chromas_and_other_categories_are_excluded() -> None:
    tree = production_tree()
    tree.append(tree_entry("skins/Ahri/chromas/Ruby.fantome", b"chroma"))
    tree.append(tree_entry("wards/Poro.fantome", b"ward"))
    source, _ = source_for(
        {
            "git/trees": FakeResponse(payload={"truncated": False, "tree": tree}),
            "index.json": FakeResponse(payload={"patch": "16.15.1"}),
        }
    )
    paths = {asset.path for asset in source.fetch_manifest(commit=COMMIT).assets}
    assert not any("chromas" in path for path in paths)
    assert not any(path.startswith("wards/") for path in paths)


def test_a_truncated_tree_is_rejected() -> None:
    source, _ = source_for(
        {"git/trees": FakeResponse(payload={"truncated": True, "tree": production_tree()})}
    )
    with pytest.raises(ManifestError, match="truncated"):
        source.fetch_manifest(commit=COMMIT)


def test_an_unavailable_patch_label_is_not_fatal() -> None:
    source, _ = source_for(
        {
            "git/trees": FakeResponse(payload={"truncated": False, "tree": production_tree()}),
            "index.json": requests.ConnectionError("offline"),
        }
    )
    assert source.fetch_manifest(commit=COMMIT).patch is None


def test_a_malformed_patch_label_is_ignored() -> None:
    source, _ = source_for(
        {
            "git/trees": FakeResponse(payload={"truncated": False, "tree": production_tree()}),
            "index.json": FakeResponse(payload={"patch": "not-a-patch"}),
        }
    )
    assert source.fetch_manifest(commit=COMMIT).patch is None


# --- the guard that protects LTK's library --------------------------------


def test_a_shrunken_repository_is_rejected() -> None:
    """A wipe-and-reseed is only safe when the replacement set is complete."""

    source, _ = source_for(
        {
            "git/trees": FakeResponse(
                payload={"truncated": False, "tree": production_tree(count=40, champions=10)}
            ),
            "index.json": FakeResponse(payload={"patch": "16.15.1"}),
        }
    )
    with pytest.raises(ManifestError, match="base skins"):
        source.fetch_manifest(commit=COMMIT)


def test_too_few_champions_is_rejected() -> None:
    source, _ = source_for(
        {
            "git/trees": FakeResponse(
                payload={"truncated": False, "tree": production_tree(count=1200, champions=5)}
            ),
            "index.json": FakeResponse(payload={"patch": "16.15.1"}),
        }
    )
    with pytest.raises(ManifestError, match="champions"):
        source.fetch_manifest(commit=COMMIT)


def test_a_duplicate_path_is_rejected() -> None:
    assets = tuple(
        SkinAsset(f"C{i % 120}", f"S{i}", f"skins/C{i % 120}/S{i}.fantome", 1, f"{i:040x}")
        for i in range(1200)
    )
    with pytest.raises(ManifestError, match="duplicate"):
        SkinManifest(COMMIT, "16.15.1", (*assets, assets[0])).validate()


def test_an_empty_asset_is_rejected() -> None:
    assets = tuple(
        SkinAsset(f"C{i % 120}", f"S{i}", f"skins/C{i % 120}/S{i}.fantome", 1, f"{i:040x}")
        for i in range(1200)
    )
    broken = (*assets[:-1], SkinAsset("Zed", "Z", "skins/Zed/Z.fantome", 0, "f" * 40))
    with pytest.raises(ManifestError, match="empty asset"):
        SkinManifest(COMMIT, None, broken).validate()


# --- downloads ------------------------------------------------------------


def test_a_download_is_verified_and_written(tmp_path: Path) -> None:
    payload = b"a valid fantome payload"
    item = SkinAsset("Ahri", "A", "skins/Ahri/A.fantome", len(payload), blob_sha(payload))
    source, _ = source_for({"raw.githubusercontent.com": FakeResponse(body=payload)})

    destination = tmp_path / "out.fantome"
    source.download(item, destination, COMMIT)

    assert destination.read_bytes() == payload


def test_a_digest_mismatch_is_rejected_and_leaves_no_file(tmp_path: Path) -> None:
    payload = b"tampered payload"
    item = SkinAsset("Ahri", "A", "skins/Ahri/A.fantome", len(payload), "0" * 40)
    source, _ = source_for({"raw.githubusercontent.com": FakeResponse(body=payload)})

    destination = tmp_path / "out.fantome"
    with pytest.raises(DownloadError):
        source.download(item, destination, COMMIT)
    assert not destination.exists()
    assert not list(tmp_path.glob("*.part"))


def test_a_short_transfer_is_rejected(tmp_path: Path) -> None:
    """Retried first, since a truncated transfer may succeed next time."""

    payload = b"short"
    item = SkinAsset("Ahri", "A", "skins/Ahri/A.fantome", 999, blob_sha(payload))
    source, _ = source_for({"raw.githubusercontent.com": FakeResponse(body=payload)})

    with pytest.raises(DownloadError) as caught:
        source.download(item, tmp_path / "out.fantome", COMMIT)
    assert "transferred" in str(caught.value.__cause__)
    assert not (tmp_path / "out.fantome").exists()


def test_an_oversized_transfer_is_rejected(tmp_path: Path) -> None:
    payload = b"x" * 500
    item = SkinAsset("Ahri", "A", "skins/Ahri/A.fantome", 10, blob_sha(payload))
    source, _ = source_for({"raw.githubusercontent.com": FakeResponse(body=payload)})

    with pytest.raises(DownloadError) as caught:
        source.download(item, tmp_path / "out.fantome", COMMIT)
    assert "exceeded" in str(caught.value.__cause__)


def test_a_transient_failure_is_retried(tmp_path: Path) -> None:
    payload = b"eventually fine"
    item = SkinAsset("Ahri", "A", "skins/Ahri/A.fantome", len(payload), blob_sha(payload))
    attempts = {"count": 0}

    def flaky() -> FakeResponse:
        attempts["count"] += 1
        if attempts["count"] < 3:
            raise requests.ConnectionError("flaky")
        return FakeResponse(body=payload)

    source, _ = source_for({"raw.githubusercontent.com": flaky})
    destination = tmp_path / "out.fantome"
    source.download(item, destination, COMMIT)

    assert attempts["count"] == 3
    assert destination.read_bytes() == payload


def test_download_gives_up_after_the_attempt_limit(tmp_path: Path) -> None:
    item = SkinAsset("Ahri", "A", "skins/Ahri/A.fantome", 5, "a" * 40)
    source, _ = source_for({"raw.githubusercontent.com": requests.ConnectionError("down")})
    with pytest.raises(DownloadError, match="Failed to download"):
        source.download(item, tmp_path / "out.fantome", COMMIT)


def test_cancellation_stops_a_download(tmp_path: Path) -> None:
    class AlreadyCancelled:
        def is_set(self) -> bool:
            return True

    item = SkinAsset("Ahri", "A", "skins/Ahri/A.fantome", 5, "a" * 40)
    source, _ = source_for({"raw.githubusercontent.com": FakeResponse(body=b"bytes")})
    with pytest.raises(DownloadCancelled):
        source.download(item, tmp_path / "out.fantome", COMMIT, cancel=AlreadyCancelled())


# --- authentication -------------------------------------------------------


def test_a_token_is_sent_when_configured() -> None:
    source = GitHubSkinSource(token="ghp_example", session_factory=lambda: FakeSession({}))
    assert source._headers["Authorization"] == "Bearer ghp_example"


def test_no_authorization_header_without_a_token() -> None:
    source = GitHubSkinSource(session_factory=lambda: FakeSession({}))
    assert "Authorization" not in source._headers


def test_closing_is_idempotent() -> None:
    source, _ = source_for({})
    source.close()
    source.close()
