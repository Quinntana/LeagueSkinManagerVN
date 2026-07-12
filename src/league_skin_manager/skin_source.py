"""Verified access to the base-skin assets in bettie9/LeagueSkins.

The GitHub tree is the source of truth for asset paths, sizes, and Git blob
hashes.  ``index.json`` is consulted only for its optional League patch label;
an unavailable or malformed index never invalidates an otherwise sound tree.
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
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, cast
from urllib.parse import quote

import requests

from .config import SKIN_SOURCE_BRANCH, SKIN_SOURCE_OWNER, SKIN_SOURCE_REPOSITORY
from .skin_installer import DEFAULT_MAX_COMPRESSED_BYTES

LOGGER = logging.getLogger(__name__)

_SHA1_PATTERN = re.compile(r"^[0-9a-f]{40}$")
_PATCH_PATTERN = re.compile(r"^\d+\.\d+(?:\.\d+)?$")

# These deliberately sit well below the current upstream values (173 champions
# and 1,920 base skins), while still catching a truncated or structurally changed
# repository before production code removes/replaces an existing installation.
MINIMUM_CHAMPIONS = 100
MINIMUM_ASSETS = 1_000
MAXIMUM_ASSET_BYTES = DEFAULT_MAX_COMPRESSED_BYTES
MAXIMUM_MANIFEST_BYTES = 2 * 1024 * 1024 * 1024


class SkinSourceError(RuntimeError):
    """Base error for manifest and asset-source failures."""


class ManifestFetchError(SkinSourceError):
    """GitHub did not return a usable manifest response."""


class ManifestValidationError(SkinSourceError):
    """A fetched manifest failed structural or production sanity checks."""


class AssetDownloadError(SkinSourceError):
    """An asset could not be downloaded after all retry attempts."""


class AssetIntegrityError(AssetDownloadError):
    """Downloaded bytes did not match the immutable Git tree metadata."""


class DownloadCancelledError(AssetDownloadError):
    """An in-progress asset download was cancelled by its caller."""


class CancelEvent(Protocol):
    """The subset of ``threading.Event`` required by the downloader."""

    def is_set(self) -> bool: ...


ProgressCallback = Callable[[int, int], None]


class HttpSession(Protocol):
    """Injectable HTTP boundary used by production and deterministic tests."""

    def get(self, url: str, **kwargs: Any) -> Any: ...

    def close(self) -> None: ...


@dataclass(frozen=True, slots=True)
class SkinAsset:
    """One immutable, base (non-chroma) ``.fantome`` asset."""

    champion: str
    name: str
    path: str
    size: int
    sha: str


@dataclass(frozen=True, slots=True)
class SkinManifest:
    """Immutable snapshot of base skins at one upstream commit."""

    commit: str
    patch: str | None
    assets: tuple[SkinAsset, ...]

    @property
    def champions(self) -> tuple[str, ...]:
        """Return champion names in deterministic, case-insensitive order."""

        return tuple(sorted({asset.champion for asset in self.assets}, key=str.casefold))

    def assets_for(self, champion: str) -> tuple[SkinAsset, ...]:
        """Return the assets for a champion without exposing mutable state."""

        wanted = champion.casefold()
        return tuple(asset for asset in self.assets if asset.champion.casefold() == wanted)

    def validate_sanity(
        self,
        minimum_assets: int = MINIMUM_ASSETS,
        minimum_champions: int = MINIMUM_CHAMPIONS,
        maximum_asset_bytes: int = MAXIMUM_ASSET_BYTES,
        maximum_manifest_bytes: int = MAXIMUM_MANIFEST_BYTES,
    ) -> None:
        """Reject incomplete or unexpectedly shaped production manifests."""

        if maximum_asset_bytes < 1 or maximum_manifest_bytes < 1:
            raise ValueError("Manifest resource limits must be positive")
        if not _SHA1_PATTERN.fullmatch(self.commit):
            raise ManifestValidationError("Manifest commit SHA is not a Git SHA-1")
        if self.patch is not None and not _PATCH_PATTERN.fullmatch(self.patch):
            raise ManifestValidationError("Manifest patch has an unexpected format")
        if len(self.assets) < minimum_assets:
            raise ManifestValidationError(
                f"Manifest contains only {len(self.assets)} base skins; "
                f"expected at least {minimum_assets}"
            )
        if len(self.champions) < minimum_champions:
            raise ManifestValidationError(
                f"Manifest contains only {len(self.champions)} champions; "
                f"expected at least {minimum_champions}"
            )

        paths = set()
        total_bytes = 0
        for asset in self.assets:
            if asset.path in paths:
                raise ManifestValidationError("Manifest contains duplicate asset paths")
            paths.add(asset.path)
            if _base_skin_parts(asset.path) is None:
                raise ManifestValidationError(
                    f"Manifest contains a non-base skin path: {asset.path}"
                )
            if asset.size <= 0:
                raise ManifestValidationError(f"Manifest contains an empty asset: {asset.path}")
            if not _SHA1_PATTERN.fullmatch(asset.sha):
                raise ManifestValidationError(
                    f"Manifest contains an invalid blob SHA: {asset.path}"
                )
            if asset.size > maximum_asset_bytes:
                raise ManifestValidationError(
                    f"Manifest asset exceeds {maximum_asset_bytes} compressed bytes: {asset.path}"
                )
            total_bytes += asset.size
            if total_bytes > maximum_manifest_bytes:
                raise ManifestValidationError(
                    f"Manifest exceeds {maximum_manifest_bytes} aggregate compressed bytes"
                )


def _base_skin_parts(path: str) -> tuple[str, str] | None:
    """Parse only ``skins/<champion>/<skin>.fantome`` paths.

    Nested paths are chromas/variants in LeagueSkins.  Filtering by exact path
    structure is more durable than trying to recognize every chroma name.
    """

    parts = path.split("/")
    if len(parts) != 3 or parts[0] != "skins":
        return None
    champion, filename = parts[1], parts[2]
    suffix = ".fantome"
    if not champion or champion in {".", ".."} or not filename.endswith(suffix):
        return None
    skin_name = filename[: -len(suffix)]
    if not skin_name or skin_name in {".", ".."}:
        return None
    return champion, skin_name


def _git_blob_hasher(expected_size: int) -> Any:
    """Create a SHA-1 hasher seeded with Git's blob object header."""

    digest = hashlib.sha1()  # noqa: S324 - required to verify Git object IDs
    digest.update(f"blob {expected_size}\0".encode("ascii"))
    return digest


class GitHubSkinSource:
    """Fetch manifests and verified assets from ``bettie9/LeagueSkins``."""

    OWNER = SKIN_SOURCE_OWNER
    REPOSITORY = SKIN_SOURCE_REPOSITORY
    BRANCH = SKIN_SOURCE_BRANCH
    API_ROOT = "https://api.github.com"
    RAW_ROOT = "https://raw.githubusercontent.com"

    def __init__(
        self,
        session: HttpSession | None = None,
        session_factory: Callable[[], HttpSession] | None = None,
        sleeper: Callable[[float], None] = time.sleep,
        attempts: int = 3,
        backoff_seconds: float = 1.0,
        api_timeout: tuple[float, float] = (5.0, 60.0),
        download_timeout: tuple[float, float] = (5.0, 180.0),
        logger: logging.Logger = LOGGER,
    ) -> None:
        if attempts < 1:
            raise ValueError("attempts must be at least one")
        if backoff_seconds < 0:
            raise ValueError("backoff_seconds cannot be negative")
        if session is not None and session_factory is not None:
            raise ValueError("session and session_factory are mutually exclusive")
        self._injected_session = session
        self._session_factory = session_factory or (lambda: cast(HttpSession, requests.Session()))
        self._thread_local = threading.local()
        self._session_lock = threading.Lock()
        self._owned_sessions: list[HttpSession] = []
        self._closed = False
        self.sleeper = sleeper
        self.attempts = attempts
        self.backoff_seconds = backoff_seconds
        self.api_timeout = api_timeout
        self.download_timeout = download_timeout
        self.logger = logger
        self._asset_commits: dict[SkinAsset, str] = {}
        self._headers = {
            "Accept": "application/vnd.github+json",
            "User-Agent": "LeagueSkinManagerVN/skin-source",
            "X-GitHub-Api-Version": "2022-11-28",
        }

    def close(self) -> None:
        if self._injected_session is not None:
            return
        with self._session_lock:
            if self._closed:
                return
            self._closed = True
            sessions = tuple(self._owned_sessions)
            self._owned_sessions.clear()
        for session in sessions:
            session.close()

    def _session(self) -> HttpSession:
        if self._injected_session is not None:
            return self._injected_session
        existing = getattr(self._thread_local, "session", None)
        if existing is not None:
            return cast(HttpSession, existing)
        with self._session_lock:
            if self._closed:
                raise SkinSourceError("Skin source is closed")
            session = self._session_factory()
            self._owned_sessions.append(session)
            self._thread_local.session = session
            return session

    def __enter__(self) -> GitHubSkinSource:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def _retry_delay(self, failed_attempt: int) -> None:
        self.sleeper(self.backoff_seconds * (2**failed_attempt))

    def _get_json(self, url: str) -> Any:
        last_error: BaseException | None = None
        for attempt in range(self.attempts):
            response: Any = None
            try:
                response = self._session().get(
                    url,
                    headers=self._headers,
                    timeout=self.api_timeout,
                )
                response.raise_for_status()
                return response.json()
            except (requests.RequestException, ValueError) as exc:
                last_error = exc
                if attempt + 1 < self.attempts:
                    self._retry_delay(attempt)
            finally:
                if response is not None:
                    response.close()
        raise ManifestFetchError(f"Failed to fetch {url}") from last_error

    @classmethod
    def _raw_url(cls, commit_sha: str, path: str) -> str:
        encoded_path = quote(path, safe="/")
        return "{}/{}/{}/{}/{}".format(
            cls.RAW_ROOT,
            quote(cls.OWNER, safe=""),
            quote(cls.REPOSITORY, safe=""),
            quote(commit_sha, safe=""),
            encoded_path,
        )

    def _fetch_commit_sha(self) -> str:
        url = f"{self.API_ROOT}/repos/{self.OWNER}/{self.REPOSITORY}/git/ref/heads/{self.BRANCH}"
        payload = self._get_json(url)
        if not isinstance(payload, Mapping):
            raise ManifestFetchError("Git ref response was not an object")
        ref_object = payload.get("object")
        if not isinstance(ref_object, Mapping):
            raise ManifestFetchError("Git ref response contained no object")
        commit_sha = ref_object.get("sha")
        if ref_object.get("type") != "commit" or not isinstance(commit_sha, str):
            raise ManifestFetchError("The main ref did not point to a commit")
        commit_sha = commit_sha.casefold()
        if not _SHA1_PATTERN.fullmatch(commit_sha):
            raise ManifestFetchError("The main ref returned an invalid commit SHA")
        return commit_sha

    def _fetch_tree(self, commit_sha: str) -> Mapping[str, Any]:
        url = (
            f"{self.API_ROOT}/repos/{self.OWNER}/{self.REPOSITORY}/"
            f"git/trees/{commit_sha}?recursive=1"
        )
        payload = self._get_json(url)
        if not isinstance(payload, Mapping):
            raise ManifestFetchError("Git tree response was not an object")
        if payload.get("truncated") is not False:
            raise ManifestValidationError("GitHub returned a truncated recursive tree")
        if not isinstance(payload.get("tree"), list):
            raise ManifestFetchError("Git tree response contained no entry list")
        return payload

    def _fetch_optional_patch(self, commit_sha: str) -> str | None:
        try:
            payload = self._get_json(self._raw_url(commit_sha, "index.json"))
        except ManifestFetchError as exc:
            self.logger.warning("LeagueSkins index patch unavailable: %s", exc)
            return None
        if not isinstance(payload, Mapping):
            self.logger.warning("LeagueSkins index was not an object; ignoring its patch")
            return None
        patch = payload.get("patch")
        if not isinstance(patch, str) or not _PATCH_PATTERN.fullmatch(patch.strip()):
            self.logger.warning("LeagueSkins index patch was malformed; ignoring it")
            return None
        return patch.strip()

    def _assets_from_tree(self, entries: Iterable[Mapping[str, Any]]) -> tuple[SkinAsset, ...]:
        assets = []
        seen_paths = set()
        for entry in entries:
            if not isinstance(entry, Mapping) or entry.get("type") != "blob":
                continue
            path = entry.get("path")
            if not isinstance(path, str):
                continue
            parsed = _base_skin_parts(path)
            if parsed is None:
                continue
            if path in seen_paths:
                raise ManifestValidationError(f"Git tree contains a duplicate path: {path}")
            size = entry.get("size")
            blob_sha = entry.get("sha")
            if not isinstance(size, int) or isinstance(size, bool) or size <= 0:
                raise ManifestValidationError(f"Skin asset has an invalid size: {path}")
            if not isinstance(blob_sha, str) or not _SHA1_PATTERN.fullmatch(blob_sha.casefold()):
                raise ManifestValidationError(f"Skin asset has an invalid blob SHA: {path}")
            seen_paths.add(path)
            champion, skin_name = parsed
            assets.append(
                SkinAsset(
                    champion=champion,
                    name=skin_name,
                    path=path,
                    size=size,
                    sha=blob_sha.casefold(),
                )
            )
        return tuple(
            sorted(
                assets,
                key=lambda asset: (
                    asset.champion.casefold(),
                    asset.name.casefold(),
                    asset.path,
                ),
            )
        )

    def fetch_manifest(
        self,
        include_patch: bool = True,
        validate_sanity: bool = True,
    ) -> SkinManifest:
        """Fetch an immutable manifest pinned to the current ``main`` commit."""

        commit_sha = self._fetch_commit_sha()
        tree = self._fetch_tree(commit_sha)
        entries = tree["tree"]
        assets = self._assets_from_tree(entries)
        patch = self._fetch_optional_patch(commit_sha) if include_patch else None
        manifest = SkinManifest(
            commit=commit_sha,
            patch=patch,
            assets=assets,
        )
        if validate_sanity:
            manifest.validate_sanity()
        self._asset_commits.update({asset: commit_sha for asset in assets})
        return manifest

    def _download_once(
        self,
        asset: SkinAsset,
        destination: Path,
        commit: str,
        cancel_event: CancelEvent | None,
        progress: ProgressCallback | None,
    ) -> None:
        response: Any = None
        temporary_path: Path | None = None
        if cancel_event is not None and cancel_event.is_set():
            raise DownloadCancelledError("Download cancelled before it started")
        destination.parent.mkdir(parents=True, exist_ok=True)
        try:
            response = self._session().get(
                self._raw_url(commit, asset.path),
                headers=self._headers,
                timeout=self.download_timeout,
                stream=True,
            )
            response.raise_for_status()
            digest = _git_blob_hasher(asset.size)
            byte_count = 0
            if progress is not None:
                progress(0, asset.size)
            with tempfile.NamedTemporaryFile(
                mode="wb",
                dir=str(destination.parent),
                prefix=f".{destination.name}.",
                suffix=".part",
                delete=False,
            ) as temporary:
                temporary_path = Path(temporary.name)
                for chunk in response.iter_content(chunk_size=128 * 1024):
                    if cancel_event is not None and cancel_event.is_set():
                        raise DownloadCancelledError(f"Download cancelled: {asset.path}")
                    if not chunk:
                        continue
                    temporary.write(chunk)
                    byte_count += len(chunk)
                    digest.update(chunk)
                    if byte_count > asset.size:
                        raise AssetIntegrityError(
                            f"{asset.path} exceeded its expected {asset.size} bytes"
                        )
                    if progress is not None:
                        progress(byte_count, asset.size)
                temporary.flush()
                os.fsync(temporary.fileno())

            if byte_count != asset.size:
                raise AssetIntegrityError(
                    f"{asset.path} downloaded {byte_count} bytes; expected {asset.size}"
                )
            actual_sha = digest.hexdigest()
            if actual_sha != asset.sha:
                raise AssetIntegrityError(
                    f"{asset.path} Git blob SHA was {actual_sha}; expected {asset.sha}"
                )
            os.replace(str(temporary_path), str(destination))
            temporary_path = None
        finally:
            if response is not None:
                response.close()
            if temporary_path is not None:
                with suppress(FileNotFoundError):
                    temporary_path.unlink()

    def download(
        self,
        asset: SkinAsset,
        target: Path,
        *,
        cancel_event: CancelEvent | None = None,
        progress: ProgressCallback | None = None,
    ) -> Path:
        """Atomically download and verify one asset, retrying transient failures."""

        if asset.size < 1 or asset.size > MAXIMUM_ASSET_BYTES:
            raise AssetDownloadError(
                f"Asset size is outside the download safety limit: {asset.path}"
            )
        destination = Path(target)
        commit = self._asset_commits.get(asset)
        if commit is None:
            raise AssetDownloadError("Asset is not part of a manifest fetched by this source")
        last_error: BaseException | None = None
        for attempt in range(self.attempts):
            try:
                self._download_once(asset, destination, commit, cancel_event, progress)
                return destination
            except (requests.RequestException, OSError, AssetIntegrityError) as exc:
                last_error = exc
                if attempt + 1 < self.attempts:
                    self._retry_delay(attempt)

        if isinstance(last_error, AssetIntegrityError):
            raise last_error
        raise AssetDownloadError(
            f"Failed to download {asset.path} after {self.attempts} attempt(s)"
        ) from last_error


__all__ = [
    "AssetDownloadError",
    "AssetIntegrityError",
    "DownloadCancelledError",
    "GitHubSkinSource",
    "ManifestFetchError",
    "ManifestValidationError",
    "MAXIMUM_ASSET_BYTES",
    "MAXIMUM_MANIFEST_BYTES",
    "SkinAsset",
    "SkinManifest",
    "SkinSourceError",
]
