"""Verified access to base skins in the upstream GitHub repository.

The Git tree is the manifest.  One recursive tree request returns every path
with its byte count and Git blob SHA-1, so integrity metadata costs nothing
extra and every download can be verified against it as the bytes arrive.

Only ``skins/<champion>/<name>.fantome`` is selected.  Deeper paths are
chromas and variants, and the repository as a whole is 2.4 GB against 53 MB of
base skins, so the whole-repository archive is never fetched.
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import tempfile
import threading
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import quote

import requests

from .config import SKIN_SOURCE_BRANCH, SKIN_SOURCE_OWNER, SKIN_SOURCE_REPOSITORY
from .hashing import CHUNK_BYTES

LOGGER = logging.getLogger(__name__)

_SHA1 = re.compile(r"^[0-9a-f]{40}$")
_PATCH = re.compile(r"^\d+\.\d+(?:\.\d+)?$")

# Deliberately well below the real figures (173 champions, ~1,935 base skins).
# These exist to catch a truncated or restructured repository *before* it can
# cause LTK's library to be wiped and reseeded with far too little.
MINIMUM_ASSETS = 1_000
MINIMUM_CHAMPIONS = 100
MAXIMUM_ASSET_BYTES = 64 * 1024 * 1024
MAXIMUM_MANIFEST_BYTES = 2 * 1024 * 1024 * 1024


class SkinSourceError(RuntimeError):
    """Base class for a skin-source failure."""


class ManifestError(SkinSourceError):
    """The manifest could not be fetched or failed its sanity checks."""


class DownloadError(SkinSourceError):
    """An asset could not be downloaded or did not match its metadata."""


class DownloadCancelled(SkinSourceError):
    """A download was cancelled before it finished."""


class CancelSignal(Protocol):
    def is_set(self) -> bool:
        """Return whether cancellation was requested."""


@dataclass(frozen=True, slots=True)
class SkinAsset:
    """One base (non-chroma) ``.fantome`` at a pinned commit."""

    champion: str
    name: str
    path: str
    size: int
    sha: str


@dataclass(frozen=True, slots=True)
class SkinManifest:
    """An immutable snapshot of the base skin set at one upstream commit."""

    commit: str
    patch: str | None
    assets: tuple[SkinAsset, ...]

    @property
    def champions(self) -> tuple[str, ...]:
        return tuple(sorted({asset.champion for asset in self.assets}, key=str.casefold))

    def validate(self) -> None:
        """Reject a manifest that does not look like the production repository.

        This is the guard that stops a truncated or restructured upstream from
        emptying LTK: a wipe-and-reseed is only safe when the replacement set is
        known to be complete.
        """

        if not _SHA1.fullmatch(self.commit):
            raise ManifestError("Manifest commit is not a Git SHA-1")
        if self.patch is not None and not _PATCH.fullmatch(self.patch):
            raise ManifestError(f"Manifest patch has an unexpected format: {self.patch}")
        if len(self.assets) < MINIMUM_ASSETS:
            raise ManifestError(
                f"Manifest holds only {len(self.assets)} base skins; "
                f"expected at least {MINIMUM_ASSETS}"
            )
        if len(self.champions) < MINIMUM_CHAMPIONS:
            raise ManifestError(
                f"Manifest holds only {len(self.champions)} champions; "
                f"expected at least {MINIMUM_CHAMPIONS}"
            )

        seen: set[str] = set()
        total = 0
        for asset in self.assets:
            if asset.path in seen:
                raise ManifestError(f"Manifest contains a duplicate path: {asset.path}")
            seen.add(asset.path)
            if _base_skin_parts(asset.path) is None:
                raise ManifestError(f"Manifest contains a non-base skin path: {asset.path}")
            if asset.size <= 0:
                raise ManifestError(f"Manifest contains an empty asset: {asset.path}")
            if not _SHA1.fullmatch(asset.sha):
                raise ManifestError(f"Manifest contains an invalid blob SHA: {asset.path}")
            if asset.size > MAXIMUM_ASSET_BYTES:
                raise ManifestError(f"Manifest asset is implausibly large: {asset.path}")
            total += asset.size
            if total > MAXIMUM_MANIFEST_BYTES:
                raise ManifestError("Manifest exceeds its aggregate size limit")


class GitHubSkinSource:
    """Adapter over the GitHub REST and raw endpoints."""

    API_ROOT = "https://api.github.com"
    RAW_ROOT = "https://raw.githubusercontent.com"

    def __init__(
        self,
        *,
        owner: str = SKIN_SOURCE_OWNER,
        repository: str = SKIN_SOURCE_REPOSITORY,
        branch: str = SKIN_SOURCE_BRANCH,
        token: str | None = None,
        attempts: int = 3,
        backoff_seconds: float = 1.0,
        session_factory: Callable[[], Any] | None = None,
        sleeper: Callable[[float], None] = time.sleep,
        logger: logging.Logger = LOGGER,
    ) -> None:
        if attempts < 1:
            raise ValueError("attempts must be at least one")
        self.owner = owner
        self.repository = repository
        self.branch = branch
        self.attempts = attempts
        self.backoff_seconds = backoff_seconds
        self.sleeper = sleeper
        self.logger = logger
        self._session_factory = session_factory or requests.Session
        self._local = threading.local()
        self._lock = threading.Lock()
        self._sessions: list[Any] = []
        self._closed = False
        self._headers = {
            "Accept": "application/vnd.github+json",
            "User-Agent": f"{SKIN_SOURCE_REPOSITORY}-sync",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        # Optional and never required: an unauthenticated first sync of ~1,900
        # raw requests measured clean, so a token is a courtesy, not a
        # dependency a fresh install could fail on.
        if token:
            self._headers["Authorization"] = f"Bearer {token}"

    # -- lifecycle --------------------------------------------------------

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            sessions = tuple(self._sessions)
            self._sessions.clear()
        for session in sessions:
            try:
                session.close()
            except Exception:  # noqa: BLE001 - closing must never raise
                self.logger.debug("Failed to close an HTTP session", exc_info=True)

    def __enter__(self) -> GitHubSkinSource:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def _session(self) -> Any:
        existing = getattr(self._local, "session", None)
        if existing is not None:
            return existing
        with self._lock:
            if self._closed:
                raise SkinSourceError("Skin source is closed")
            session = self._session_factory()
            self._sessions.append(session)
        self._local.session = session
        return session

    # -- manifest ---------------------------------------------------------

    def head_commit(self) -> str:
        """Return the branch's current commit SHA. One request."""

        url = f"{self.API_ROOT}/repos/{self.owner}/{self.repository}/git/ref/heads/{self.branch}"
        payload = self._get_json(url)
        if not isinstance(payload, Mapping):
            raise ManifestError("Git ref response was not an object")
        obj = payload.get("object")
        if not isinstance(obj, Mapping) or obj.get("type") != "commit":
            raise ManifestError("Git ref did not point at a commit")
        commit = obj.get("sha")
        if not isinstance(commit, str) or not _SHA1.fullmatch(commit.casefold()):
            raise ManifestError("Git ref returned an invalid commit SHA")
        return commit.casefold()

    def fetch_manifest(self, commit: str | None = None) -> SkinManifest:
        """Fetch a validated manifest pinned to *commit* (default: branch head)."""

        commit_sha = commit or self.head_commit()
        url = (
            f"{self.API_ROOT}/repos/{self.owner}/{self.repository}/"
            f"git/trees/{commit_sha}?recursive=1"
        )
        payload = self._get_json(url)
        if not isinstance(payload, Mapping):
            raise ManifestError("Git tree response was not an object")
        if payload.get("truncated") is not False:
            raise ManifestError("GitHub returned a truncated recursive tree")
        entries = payload.get("tree")
        if not isinstance(entries, list):
            raise ManifestError("Git tree response contained no entry list")

        manifest = SkinManifest(
            commit=commit_sha,
            patch=self._optional_patch(commit_sha),
            assets=_assets_from_tree(entries),
        )
        manifest.validate()
        return manifest

    def _optional_patch(self, commit: str) -> str | None:
        """Read the repository's advertised patch label; never fatal."""

        try:
            payload = self._get_json(self._raw_url(commit, "index.json"))
        except SkinSourceError as error:
            self.logger.warning("Patch label unavailable: %s", error)
            return None
        if not isinstance(payload, Mapping):
            return None
        patch = payload.get("patch")
        if not isinstance(patch, str) or not _PATCH.fullmatch(patch.strip()):
            return None
        return patch.strip()

    # -- downloads --------------------------------------------------------

    def download(
        self,
        asset: SkinAsset,
        destination: Path,
        commit: str,
        cancel: CancelSignal | None = None,
    ) -> None:
        """Download *asset* to *destination*, verifying size and blob SHA inline.

        The digest is computed as the bytes arrive, so a corrupted or truncated
        transfer is rejected without a second pass over the file.
        """

        last: BaseException | None = None
        for attempt in range(self.attempts):
            _raise_if_cancelled(cancel)
            try:
                self._download_once(asset, destination, commit, cancel)
                return
            except DownloadCancelled:
                raise
            except (requests.RequestException, DownloadError, OSError) as error:
                last = error
                if attempt + 1 < self.attempts:
                    self.sleeper(self.backoff_seconds * (2**attempt))
        raise DownloadError(f"Failed to download {asset.path}") from last

    def _download_once(
        self,
        asset: SkinAsset,
        destination: Path,
        commit: str,
        cancel: CancelSignal | None,
    ) -> None:
        destination.parent.mkdir(parents=True, exist_ok=True)
        digest = _blob_hasher(asset.size)
        written = 0
        temporary: Path | None = None
        response = None
        try:
            response = self._session().get(
                self._raw_url(commit, asset.path),
                headers=self._headers,
                timeout=(5.0, 180.0),
                stream=True,
            )
            response.raise_for_status()
            with tempfile.NamedTemporaryFile(
                mode="wb",
                dir=str(destination.parent),
                prefix=".dl-",
                suffix=".part",
                delete=False,
            ) as handle:
                temporary = Path(handle.name)
                for chunk in response.iter_content(chunk_size=CHUNK_BYTES):
                    _raise_if_cancelled(cancel)
                    if not chunk:
                        continue
                    written += len(chunk)
                    if written > asset.size:
                        raise DownloadError(
                            f"{asset.path} exceeded its declared {asset.size} bytes"
                        )
                    handle.write(chunk)
                    digest.update(chunk)
                handle.flush()
                os.fsync(handle.fileno())

            if written != asset.size:
                raise DownloadError(
                    f"{asset.path} transferred {written} bytes; expected {asset.size}"
                )
            actual = digest.hexdigest()
            if actual != asset.sha:
                raise DownloadError(
                    f"{asset.path} digest mismatch: expected {asset.sha}, got {actual}"
                )
            os.replace(temporary, destination)
            temporary = None
        finally:
            if response is not None:
                response.close()
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    # -- plumbing ---------------------------------------------------------

    def _get_json(self, url: str) -> Any:
        last: BaseException | None = None
        for attempt in range(self.attempts):
            response = None
            try:
                response = self._session().get(url, headers=self._headers, timeout=(5.0, 60.0))
                response.raise_for_status()
                return response.json()
            except (requests.RequestException, ValueError) as error:
                last = error
                if attempt + 1 < self.attempts:
                    self.sleeper(self.backoff_seconds * (2**attempt))
            finally:
                if response is not None:
                    response.close()
        raise ManifestError(f"Failed to fetch {url}") from last

    def _raw_url(self, commit: str, path: str) -> str:
        return "{}/{}/{}/{}/{}".format(
            self.RAW_ROOT,
            quote(self.owner, safe=""),
            quote(self.repository, safe=""),
            quote(commit, safe=""),
            quote(path, safe="/"),
        )


def _assets_from_tree(entries: Iterable[object]) -> tuple[SkinAsset, ...]:
    assets: list[SkinAsset] = []
    for entry in entries:
        if not isinstance(entry, Mapping) or entry.get("type") != "blob":
            continue
        path = entry.get("path")
        if not isinstance(path, str):
            continue
        parts = _base_skin_parts(path)
        if parts is None:
            continue
        size = entry.get("size")
        sha = entry.get("sha")
        if not isinstance(size, int) or isinstance(size, bool) or not isinstance(sha, str):
            continue
        champion, name = parts
        assets.append(
            SkinAsset(champion=champion, name=name, path=path, size=size, sha=sha.casefold())
        )
    return tuple(sorted(assets, key=lambda asset: asset.path))


def _base_skin_parts(path: str) -> tuple[str, str] | None:
    """Parse only ``skins/<champion>/<name>.fantome``.

    Matching on exact depth is more durable than trying to recognise every
    chroma naming convention: anything nested deeper is a chroma or variant.
    """

    parts = path.split("/")
    if len(parts) != 3 or parts[0] != "skins":
        return None
    champion, filename = parts[1], parts[2]
    if not champion or champion in {".", ".."} or not filename.endswith(".fantome"):
        return None
    name = filename[: -len(".fantome")]
    if not name or name in {".", ".."}:
        return None
    return champion, name


def _blob_hasher(size: int) -> Any:
    digest = hashlib.sha1(usedforsecurity=False)
    digest.update(f"blob {size}\0".encode())
    return digest


def _raise_if_cancelled(cancel: CancelSignal | None) -> None:
    if cancel is not None and cancel.is_set():
        raise DownloadCancelled("Cancelled")


__all__ = [
    "CancelSignal",
    "DownloadCancelled",
    "DownloadError",
    "GitHubSkinSource",
    "ManifestError",
    "SkinAsset",
    "SkinManifest",
    "SkinSourceError",
]
