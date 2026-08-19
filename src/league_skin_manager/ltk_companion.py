"""Secure discovery, preparation, and launch support for LTK Manager.

The companion deliberately owns only its installer cache.  It never writes to
LTK's application data or to CSLOL Manager's installation and profile folders.
"""

from __future__ import annotations

import ctypes
import hashlib
import hmac
import json
import os
import re
import secrets
import stat
import subprocess
import tempfile
import time
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from threading import Event, Lock, RLock
from typing import Any, Protocol, cast
from urllib.parse import urlparse

import requests

from .atomic import atomic_write_json
from .config import APP_NAME, LTK_RELEASES_URL

MAX_LTK_INSTALLER_BYTES = 256 * 1024 * 1024
RELEASE_CHECK_TTL_SECONDS = 6 * 60 * 60
RELEASE_CHECK_SCHEMA_VERSION = 1
RELEASE_CHECK_FILENAME = "release-check.json"
LTK_INSTALLER_SWITCHES = ("/P", "/R")
LTK_EXECUTABLE_NAMES = ("LTK Manager.exe", "ltk-manager.exe")
LTK_SIGNER_NAME = "Natoken LLC"

_DOWNLOAD_CHUNK_BYTES = 256 * 1024
_DIGEST_PATTERN = re.compile(r"^sha256:([0-9a-fA-F]{64})$")
_INSTALLER_PATTERN = re.compile(
    r"^LTK\.Manager_(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)_x64-setup\.exe$"
)
_VERSION_PATTERN = re.compile(r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$")
_WINDOWS_VERSION_PATTERN = re.compile(
    r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)(?:\.0)?$"
)
_DISPLAY_NAME_PATTERN = re.compile(
    r"^LTK Manager(?:\s+v?(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\."
    r"(?:0|[1-9][0-9]*)(?:\.0)?)?$",
    re.IGNORECASE,
)
_GITHUB_DOWNLOAD_HOSTS = frozenset(
    {
        "github.com",
        "objects.githubusercontent.com",
        "release-assets.githubusercontent.com",
    }
)
_OFFICIAL_DOWNLOAD_PREFIX = "/LeagueToolkit/ltk-manager/releases/download/"
_SIGNATURE_PATH_ENV = "LSMVN_LTK_SIGNATURE_PATH"
_FILE_VERSION_PATH_ENV = "LSMVN_LTK_FILE_VERSION_PATH"
_UNSAFE_COPY_DIRECTORY_NAMES = frozenset({"audit", "audits", "cache", "caches"})
_UNINSTALL_KEY = r"Software\Microsoft\Windows\CurrentVersion\Uninstall"

_AUTHENTICODE_SCRIPT = (
    "$ErrorActionPreference='Stop';"
    "$module=Join-Path $PSHOME "
    "'Modules\\Microsoft.PowerShell.Security\\Microsoft.PowerShell.Security.psd1';"
    "Import-Module -Name $module -Force -ErrorAction Stop;"
    "$s=Microsoft.PowerShell.Security\\Get-AuthenticodeSignature "
    "-LiteralPath $env:LSMVN_LTK_SIGNATURE_PATH;"
    "$subject=if($null -eq $s.SignerCertificate){''}else{$s.SignerCertificate.Subject};"
    "[pscustomobject]@{Status=[string]$s.Status;Subject=[string]$subject}|"
    "ConvertTo-Json -Compress"
)
_FILE_VERSION_SCRIPT = (
    "$ErrorActionPreference='Stop';"
    "$v=[Diagnostics.FileVersionInfo]::GetVersionInfo("
    "$env:LSMVN_LTK_FILE_VERSION_PATH).ProductVersion;"
    "[Console]::Out.Write([string]$v)"
)


class LtkCompanionError(RuntimeError):
    """Base class for an LTK companion operation failure."""


class LtkReleaseError(LtkCompanionError):
    """Official release metadata could not be obtained or trusted."""


class LtkDownloadError(LtkCompanionError):
    """The selected official installer could not be downloaded safely."""


class LtkVerificationError(LtkCompanionError):
    """An installer did not satisfy integrity or Authenticode policy."""


class LtkLaunchError(LtkCompanionError):
    """An installed executable or verified installer could not be started."""


class LtkCancelled(LtkCompanionError):
    """An LTK companion operation was cancelled before an external launch."""


class LtkClosedError(LtkCompanionError):
    """An operation was requested after the companion was closed."""


@dataclass(frozen=True, order=True, slots=True)
class LtkVersion:
    major: int
    minor: int
    patch: int

    def __post_init__(self) -> None:
        values = (self.major, self.minor, self.patch)
        if any(
            not isinstance(value, int) or isinstance(value, bool) or value < 0 for value in values
        ):
            raise ValueError("LTK version components must be non-negative integers")

    @classmethod
    def parse(cls, value: str) -> LtkVersion:
        """Parse an exact three-component version without a tag prefix."""

        if len(value) > 64:
            raise ValueError("LTK version is too long")
        match = _VERSION_PATTERN.fullmatch(value)
        if match is None:
            raise ValueError(f"Invalid LTK version: {value!r}")
        return cls(*(int(component) for component in match.groups()))

    @classmethod
    def from_tag(cls, tag: str) -> LtkVersion:
        """Parse the exact ``vMAJOR.MINOR.PATCH`` GitHub tag format."""

        if not tag.startswith("v"):
            raise ValueError(f"Invalid LTK release tag: {tag!r}")
        return cls.parse(tag[1:])

    def __str__(self) -> str:
        return f"{self.major}.{self.minor}.{self.patch}"


@dataclass(frozen=True, slots=True)
class LtkReleaseAsset:
    name: str
    url: str
    size: int
    digest: str

    def __post_init__(self) -> None:
        name_match = _INSTALLER_PATTERN.fullmatch(self.name)
        if name_match is None:
            raise ValueError("LTK release asset is not the exact signed x64 NSIS installer")
        if isinstance(self.size, bool) or not 0 < self.size <= MAX_LTK_INSTALLER_BYTES:
            raise ValueError("LTK installer size is outside the allowed range")
        digest_match = _DIGEST_PATTERN.fullmatch(self.digest)
        if digest_match is None:
            raise ValueError("LTK installer digest must be sha256 followed by 64 hex digits")
        _validate_asset_download_url(self.url, self.name, ".".join(name_match.groups()))
        object.__setattr__(self, "digest", f"sha256:{digest_match.group(1).casefold()}")

    @property
    def sha256(self) -> str:
        return self.digest.removeprefix("sha256:")


@dataclass(frozen=True, slots=True)
class LtkRelease:
    version: LtkVersion
    asset: LtkReleaseAsset

    def __post_init__(self) -> None:
        expected_name = f"LTK.Manager_{self.version}_x64-setup.exe"
        if self.asset.name != expected_name:
            raise ValueError("LTK release tag and installer filename versions do not match")

    @property
    def tag(self) -> str:
        return f"v{self.version}"


@dataclass(frozen=True, slots=True)
class AuthenticodeSignature:
    status: str
    subject: str


@dataclass(frozen=True, slots=True)
class RegistryUninstallEntry:
    display_name: str | None = None
    display_version: str | None = None
    display_icon: str | None = None
    install_location: str | None = None


@dataclass(frozen=True, slots=True)
class LtkInstallation:
    executable: Path
    version: LtkVersion


class LtkPreparationStatus(str, Enum):
    CURRENT_INSTALLED = "current-installed"
    INSTALLER_READY = "installer-ready"


@dataclass(frozen=True, slots=True)
class LtkPreparationResult:
    status: LtkPreparationStatus
    release: LtkRelease
    installation: LtkInstallation | None
    installer_path: Path | None


class LtkCompanionStatus(str, Enum):
    LAUNCHED_CURRENT = "launched-current"
    INSTALLER_STARTED = "installer-started"
    EXISTING_LAUNCHED_AFTER_RELEASE_CHECK_FAILURE = "existing-launched-after-release-check-failure"


@dataclass(frozen=True, slots=True)
class LtkCompanionResult:
    status: LtkCompanionStatus
    version: LtkVersion
    path: Path
    release: LtkRelease | None
    check_error: str | None = None


class HttpResponse(Protocol):
    url: str

    def raise_for_status(self) -> None: ...

    def json(self) -> object: ...

    def iter_content(self, chunk_size: int) -> Iterator[bytes]: ...

    def close(self) -> None: ...


class HttpSession(Protocol):
    def get(self, url: str, **kwargs: object) -> HttpResponse: ...

    def close(self) -> None: ...


class LtkReleaseProvider(Protocol):
    def latest(self) -> LtkRelease: ...

    def download(
        self,
        asset: LtkReleaseAsset,
        destination: Path,
        cancel_event: Event | None = None,
    ) -> Path: ...

    def close(self) -> None: ...


class AuthenticodeVerifier(Protocol):
    def verify(self, path: Path) -> AuthenticodeSignature: ...


class UninstallRegistry(Protocol):
    def entries(self) -> Iterable[RegistryUninstallEntry]: ...


class LtkInstallationLocator(Protocol):
    def locate(self) -> LtkInstallation | None: ...


PowerShellRunner = Callable[
    [Sequence[str], Mapping[str, str], float, int], subprocess.CompletedProcess[str]
]
FileVersionReader = Callable[[Path], LtkVersion | str | None]
ExecutableLauncher = Callable[[Path], bool | None]
InstallerLauncher = Callable[[Path, tuple[str, ...]], bool | None]


class LtkReleaseClient:
    """Read and stream only the official GitHub latest-release asset."""

    def __init__(
        self,
        session: HttpSession | None = None,
        *,
        timeout: float = 15.0,
    ) -> None:
        if timeout <= 0:
            raise ValueError("timeout must be positive")
        self._owns_session = session is None
        self._session = cast(HttpSession, requests.Session()) if session is None else session
        self._timeout = timeout

    def close(self) -> None:
        if self._owns_session:
            self._session.close()

    def latest(self) -> LtkRelease:
        response: HttpResponse | None = None
        try:
            response = self._session.get(
                LTK_RELEASES_URL,
                timeout=self._timeout,
                headers={"Accept": "application/vnd.github+json"},
            )
            response.raise_for_status()
            payload = response.json()
        except Exception as error:
            if isinstance(error, LtkReleaseError):
                raise
            raise LtkReleaseError("Could not read the official LTK latest release") from error
        finally:
            if response is not None:
                _close_response(response)
        return self._parse_release(payload)

    def download(
        self,
        asset: LtkReleaseAsset,
        destination: Path,
        cancel_event: Event | None = None,
    ) -> Path:
        response: HttpResponse | None = None
        partial: Path | None = None
        owns_partial = False
        try:
            _check_cancelled(cancel_event)
            _validate_asset_download_url(asset.url, asset.name, _installer_version_text(asset.name))
            destination = _absolute_path(destination)
            destination.parent.mkdir(parents=True, exist_ok=True)
            if destination.parent.is_symlink() or not destination.parent.is_dir():
                raise LtkDownloadError("LTK installer cache parent is not a real directory")
            partial = destination.with_name(
                f".{destination.name}.{os.getpid()}.{secrets.token_hex(8)}.part"
            )

            response = self._session.get(
                asset.url,
                stream=True,
                timeout=(10.0, 60.0),
                headers={"Accept": "application/octet-stream"},
            )
            response.raise_for_status()
            final_url = response.url
            if not isinstance(final_url, str):
                raise LtkDownloadError("LTK download response did not contain a final URL")
            _validate_asset_download_url(final_url, asset.name, _installer_version_text(asset.name))

            written = 0
            digest = hashlib.sha256()
            with partial.open("xb") as output:
                owns_partial = True
                for chunk in response.iter_content(chunk_size=_DOWNLOAD_CHUNK_BYTES):
                    _check_cancelled(cancel_event)
                    if not chunk:
                        continue
                    if not isinstance(chunk, bytes):
                        raise LtkDownloadError("LTK download returned a non-byte chunk")
                    written += len(chunk)
                    if written > asset.size or written > MAX_LTK_INSTALLER_BYTES:
                        raise LtkDownloadError(
                            f"LTK installer exceeded its expected {asset.size} bytes"
                        )
                    output.write(chunk)
                    digest.update(chunk)
                _check_cancelled(cancel_event)
                output.flush()
                os.fsync(output.fileno())

            if written != asset.size:
                raise LtkDownloadError(
                    f"LTK installer size mismatch: expected {asset.size}, received {written}"
                )
            if not hmac.compare_digest(digest.hexdigest(), asset.sha256):
                raise LtkDownloadError("LTK installer SHA-256 did not match GitHub metadata")
            os.replace(partial, destination)
            _fsync_directory(destination.parent)
            return destination
        except Exception as error:
            if isinstance(error, (LtkCancelled, LtkDownloadError)):
                raise
            raise LtkDownloadError("Could not download the official LTK installer") from error
        finally:
            if response is not None:
                _close_response(response)
            if partial is not None and owns_partial:
                with suppress(OSError):
                    partial.unlink(missing_ok=True)

    @staticmethod
    def _parse_release(payload: object) -> LtkRelease:
        if not isinstance(payload, dict):
            raise LtkReleaseError("GitHub returned invalid LTK release metadata")
        tag = payload.get("tag_name")
        if not isinstance(tag, str):
            raise LtkReleaseError("LTK release metadata has no valid tag")
        try:
            version = LtkVersion.from_tag(tag)
        except ValueError as error:
            raise LtkReleaseError("LTK release tag is not vMAJOR.MINOR.PATCH") from error
        if payload.get("draft") is True or payload.get("prerelease") is True:
            raise LtkReleaseError("GitHub latest release was unexpectedly draft or prerelease")

        assets = payload.get("assets")
        if not isinstance(assets, list):
            raise LtkReleaseError("LTK release did not contain an asset list")
        expected_name = f"LTK.Manager_{version}_x64-setup.exe"
        matching = [
            value
            for value in assets
            if isinstance(value, dict) and value.get("name") == expected_name
        ]
        if len(matching) != 1:
            raise LtkReleaseError(
                "LTK release must contain exactly one matching signed x64 NSIS installer"
            )
        candidate = matching[0]
        url = candidate.get("browser_download_url")
        size = candidate.get("size")
        digest = candidate.get("digest")
        if not isinstance(url, str) or not isinstance(digest, str):
            raise LtkReleaseError("LTK installer URL or digest metadata is invalid")
        if not isinstance(size, int) or isinstance(size, bool):
            raise LtkReleaseError("LTK installer size metadata is invalid")
        try:
            return LtkRelease(
                version,
                LtkReleaseAsset(expected_name, url, size, digest),
            )
        except ValueError as error:
            raise LtkReleaseError("LTK installer metadata failed validation") from error


class PowerShellAuthenticodeVerifier:
    """Require a valid Windows signature from the fixed trusted publisher."""

    def __init__(
        self,
        *,
        powershell_path: Path | None = None,
        runner: PowerShellRunner | None = None,
        timeout: float = 20.0,
    ) -> None:
        if timeout <= 0:
            raise ValueError("timeout must be positive")
        self._powershell_path = (
            _system_powershell_path() if powershell_path is None else powershell_path
        )
        self._runner = _run_powershell if runner is None else runner
        self._timeout = timeout

    def verify(self, path: Path) -> AuthenticodeSignature:
        executable = _absolute_path(path)
        if executable.is_symlink() or not executable.is_file():
            raise LtkVerificationError("LTK installer must be a real file")
        powershell = _absolute_path(self._powershell_path)
        if powershell.is_symlink() or not powershell.is_file():
            raise LtkVerificationError("Fixed system Windows PowerShell was not found")

        arguments = (
            str(powershell),
            "-NoLogo",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-Command",
            _AUTHENTICODE_SCRIPT,
        )
        environment = dict(os.environ)
        environment[_SIGNATURE_PATH_ENV] = str(executable)
        environment["PSMODULEPATH"] = str(powershell.parent / "Modules")
        creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        try:
            completed = self._runner(
                arguments,
                environment,
                self._timeout,
                creation_flags,
            )
        except Exception as error:
            raise LtkVerificationError("Windows could not inspect the LTK signature") from error
        if completed.returncode != 0 or not isinstance(completed.stdout, str):
            raise LtkVerificationError("Windows rejected the LTK Authenticode inspection")
        try:
            payload = json.loads(completed.stdout.strip())
        except (TypeError, ValueError) as error:
            raise LtkVerificationError("Windows returned invalid Authenticode details") from error
        if not isinstance(payload, dict):
            raise LtkVerificationError("Windows returned invalid Authenticode details")
        status = payload.get("Status")
        subject = payload.get("Subject")
        if not isinstance(status, str) or not isinstance(subject, str):
            raise LtkVerificationError("Windows returned incomplete Authenticode details")
        if status != "Valid":
            raise LtkVerificationError(f"LTK Authenticode status is not Valid: {status}")
        if not _is_allowed_signer(subject):
            raise LtkVerificationError("LTK installer signer is not Natoken LLC")
        return AuthenticodeSignature(status, subject)


class WindowsUninstallRegistry:
    """Yield matching uninstall metadata from HKCU and both HKLM views."""

    def entries(self) -> Iterable[RegistryUninstallEntry]:
        if os.name != "nt":
            return ()
        try:
            import winreg
        except ImportError:
            return ()

        roots: tuple[tuple[Any, int], ...] = (
            (winreg.HKEY_CURRENT_USER, 0),
            (winreg.HKEY_CURRENT_USER, winreg.KEY_WOW64_32KEY),
            (winreg.HKEY_LOCAL_MACHINE, winreg.KEY_WOW64_64KEY),
            (winreg.HKEY_LOCAL_MACHINE, winreg.KEY_WOW64_32KEY),
        )
        found: list[RegistryUninstallEntry] = []
        seen: set[tuple[str | None, str | None, str | None, str | None]] = set()
        for root, view in roots:
            try:
                uninstall_key = winreg.OpenKey(root, _UNINSTALL_KEY, 0, winreg.KEY_READ | view)
            except OSError:
                continue
            with uninstall_key:
                index = 0
                while True:
                    try:
                        subkey_name = winreg.EnumKey(uninstall_key, index)
                    except OSError:
                        break
                    index += 1
                    try:
                        subkey = winreg.OpenKey(uninstall_key, subkey_name, 0, winreg.KEY_READ)
                    except OSError:
                        continue
                    with subkey:
                        entry = RegistryUninstallEntry(
                            display_name=_registry_string(winreg, subkey, "DisplayName"),
                            display_version=_registry_string(winreg, subkey, "DisplayVersion"),
                            display_icon=_registry_string(winreg, subkey, "DisplayIcon"),
                            install_location=_registry_string(winreg, subkey, "InstallLocation"),
                        )
                    identity = (
                        entry.display_name,
                        entry.display_version,
                        entry.display_icon,
                        entry.install_location,
                    )
                    if identity not in seen:
                        seen.add(identity)
                        found.append(entry)
        return tuple(found)


class LtkInstallLocator:
    """Locate a real installed LTK executable without scanning temp/cache copies."""

    def __init__(
        self,
        registry: UninstallRegistry | None = None,
        *,
        fallbacks: Sequence[Path] | None = None,
        file_version_reader: FileVersionReader | None = None,
        temp_root: Path | None = None,
        excluded_roots: Sequence[Path] = (),
    ) -> None:
        self._registry = WindowsUninstallRegistry() if registry is None else registry
        self._fallbacks = tuple(fallbacks) if fallbacks is not None else _known_install_roots()
        self._file_version_reader = (
            _read_file_version if file_version_reader is None else file_version_reader
        )
        self._temp_root = _absolute_path(temp_root or Path(tempfile.gettempdir()))
        implicit_cache_roots = _known_companion_cache_roots()
        self._excluded_roots = tuple(
            _absolute_path(path) for path in (*implicit_cache_roots, *excluded_roots)
        )
        self._cache_lock = Lock()
        self._cached: tuple[LtkInstallation, tuple[int, int, int]] | None = None

    def locate(self) -> LtkInstallation | None:
        """Return the installed LTK, reusing a still-valid previous result.

        A full lookup enumerates the uninstall registry and reads the
        executable's file version, which spawns a helper process.  Callers such
        as the automatic port check run after every synchronization, so the
        previous result is reused whenever the exact executable is unchanged.
        Any difference in size or modification time - including an LTK
        self-update - falls through to a complete lookup.
        """

        with self._cache_lock:
            cached = self._cached
        if cached is not None:
            cached_installation, cached_identity = cached
            if _executable_identity(cached_installation.executable) == cached_identity:
                return cached_installation
        located = self._locate_uncached()
        with self._cache_lock:
            if located is None:
                self._cached = None
            else:
                identity = _executable_identity(located.executable)
                self._cached = None if identity is None else (located, identity)
        return located

    def invalidate(self) -> None:
        """Drop any cached lookup, forcing the next call to rescan."""

        with self._cache_lock:
            self._cached = None

    def _locate_uncached(self) -> LtkInstallation | None:
        try:
            entries = tuple(self._registry.entries())
        except OSError:
            entries = ()
        seen: set[str] = set()
        for entry in entries:
            if (
                entry.display_name is None
                or _DISPLAY_NAME_PATTERN.fullmatch(entry.display_name.strip()) is None
            ):
                continue
            for candidate in self._entry_candidates(entry):
                installation = self._installation(candidate, entry.display_version, seen)
                if installation is not None:
                    return installation

        for fallback in self._fallbacks:
            fallback_path = _absolute_path(fallback)
            candidates = (
                (fallback_path,)
                if fallback_path.name.casefold() in _executable_name_keys()
                else tuple(fallback_path / name for name in LTK_EXECUTABLE_NAMES)
            )
            for candidate in candidates:
                installation = self._installation(candidate, None, seen)
                if installation is not None:
                    return installation
        return None

    @staticmethod
    def _entry_candidates(entry: RegistryUninstallEntry) -> tuple[Path, ...]:
        candidates: list[Path] = []
        if entry.display_icon:
            icon_path = _display_icon_path(entry.display_icon)
            if icon_path is not None:
                candidates.append(icon_path)
        if entry.install_location:
            raw_location = _expand_windows_environment(entry.install_location)
            if raw_location.startswith('"') and raw_location.endswith('"'):
                raw_location = raw_location[1:-1]
            expanded_location = Path(raw_location).expanduser()
            if expanded_location.is_absolute():
                location = _absolute_path(expanded_location)
                candidates.extend(location / name for name in LTK_EXECUTABLE_NAMES)
        return tuple(candidates)

    def _installation(
        self,
        candidate: Path,
        display_version: str | None,
        seen: set[str],
    ) -> LtkInstallation | None:
        candidate = _absolute_path(candidate)
        identity = os.path.normcase(str(candidate))
        if identity in seen:
            return None
        seen.add(identity)
        if candidate.name.casefold() not in _executable_name_keys():
            return None
        if not self._safe_installed_file(candidate):
            return None

        version = _try_parse_version(display_version)
        if version is None:
            try:
                raw_version = self._file_version_reader(candidate)
            except OSError:
                return None
            version = (
                raw_version
                if isinstance(raw_version, LtkVersion)
                else _try_parse_version(raw_version)
            )
        if version is None:
            return None
        return LtkInstallation(candidate, version)

    def _safe_installed_file(self, candidate: Path) -> bool:
        try:
            if candidate.is_symlink() or not candidate.is_file():
                return False
            resolved = candidate.resolve(strict=True)
        except OSError:
            return False
        if os.path.normcase(str(resolved)) != os.path.normcase(str(candidate)):
            return False
        if _path_is_within(candidate, self._temp_root):
            return False
        if any(_path_is_within(candidate, root) for root in self._excluded_roots):
            return False
        return not any(
            component.casefold() in _UNSAFE_COPY_DIRECTORY_NAMES
            for component in candidate.parent.parts
        )


class LtkCompanion:
    """Serialize preparation and explicit launch/install operations."""

    def __init__(
        self,
        client: LtkReleaseProvider,
        locator: LtkInstallationLocator,
        verifier: AuthenticodeVerifier,
        cache_dir: Path,
        *,
        launcher: ExecutableLauncher | None = None,
        installer_launcher: InstallerLauncher | None = None,
    ) -> None:
        self._client = client
        self._locator = locator
        self._verifier = verifier
        self._cache_dir = _absolute_path(cache_dir)
        self._release_cache_path = self._cache_dir / RELEASE_CHECK_FILENAME
        self._launcher = _launch_executable if launcher is None else launcher
        self._installer_launcher = (
            _launch_installer if installer_launcher is None else installer_launcher
        )
        self._lock = RLock()
        self._closed = False

    def __enter__(self) -> LtkCompanion:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._client.close()

    def prepare(self, cancel_event: Event | None = None) -> LtkPreparationResult:
        """Fetch and verify an installer when needed, without executing anything."""

        with self._lock:
            self._ensure_open()
            installation = self._locator.locate()
            return self._prepare_locked(installation, cancel_event)

    def start(self, cancel_event: Event | None = None) -> LtkCompanionResult:
        """Launch a current install, or start a verified NSIS installer with ``/P /R``."""

        with self._lock:
            self._ensure_open()
            _check_cancelled(cancel_event)
            installation = self._locator.locate()
            try:
                prepared = self._prepare_locked(installation, cancel_event)
            except LtkReleaseError as error:
                _check_cancelled(cancel_event)
                if installation is None:
                    raise
                self._launch_existing(installation.executable)
                return LtkCompanionResult(
                    LtkCompanionStatus.EXISTING_LAUNCHED_AFTER_RELEASE_CHECK_FAILURE,
                    installation.version,
                    installation.executable,
                    None,
                    str(error),
                )

            _check_cancelled(cancel_event)
            if prepared.status is LtkPreparationStatus.CURRENT_INSTALLED:
                current = prepared.installation
                if current is None:
                    raise LtkCompanionError("Current LTK preparation has no installation")
                self._launch_existing(current.executable)
                return LtkCompanionResult(
                    LtkCompanionStatus.LAUNCHED_CURRENT,
                    current.version,
                    current.executable,
                    prepared.release,
                )

            installer_path = prepared.installer_path
            if installer_path is None:
                raise LtkCompanionError("Prepared LTK installer path is missing")
            self._launch_verified_installer(installer_path)
            return LtkCompanionResult(
                LtkCompanionStatus.INSTALLER_STARTED,
                prepared.release.version,
                installer_path,
                prepared.release,
            )

    def _prepare_locked(
        self,
        installation: LtkInstallation | None,
        cancel_event: Event | None,
    ) -> LtkPreparationResult:
        _check_cancelled(cancel_event)
        if installation is not None:
            # An install that already satisfies a recent release check needs no
            # network call at all. The cached release is only ever used to
            # confirm this "already current" outcome, never to verify or
            # download an installer, so a stale entry cannot weaken trust.
            cached = self._recent_release()
            if cached is not None and installation.version >= cached.version:
                return LtkPreparationResult(
                    LtkPreparationStatus.CURRENT_INSTALLED,
                    cached,
                    installation,
                    None,
                )
        release = self._client.latest()
        self._remember_release(release)
        _check_cancelled(cancel_event)
        if installation is not None and installation.version >= release.version:
            return LtkPreparationResult(
                LtkPreparationStatus.CURRENT_INSTALLED,
                release,
                installation,
                None,
            )

        self._ensure_cache_directory()
        installer_path = self._cache_dir / release.asset.name
        if installer_path.exists() or installer_path.is_symlink():
            try:
                self._verify_installer_file(release.asset, installer_path, cancel_event)
            except LtkVerificationError:
                self._discard_cache_file(installer_path)
            else:
                self._prune_obsolete_installers(installer_path)
                return LtkPreparationResult(
                    LtkPreparationStatus.INSTALLER_READY,
                    release,
                    installation,
                    installer_path,
                )

        _check_cancelled(cancel_event)
        downloaded = self._client.download(release.asset, installer_path, cancel_event)
        if _absolute_path(downloaded) != installer_path:
            self._discard_cache_file(installer_path)
            raise LtkDownloadError("LTK client returned an unexpected installer path")
        try:
            self._verify_installer_file(release.asset, installer_path, cancel_event)
        except (LtkCancelled, LtkVerificationError):
            self._discard_cache_file(installer_path)
            raise
        self._prune_obsolete_installers(installer_path)
        return LtkPreparationResult(
            LtkPreparationStatus.INSTALLER_READY,
            release,
            installation,
            installer_path,
        )

    def _verify_installer_file(
        self,
        asset: LtkReleaseAsset,
        path: Path,
        cancel_event: Event | None,
    ) -> None:
        _check_cancelled(cancel_event)
        try:
            metadata_before = path.lstat()
        except OSError as error:
            raise LtkVerificationError("LTK installer cache file is missing") from error
        if not stat.S_ISREG(metadata_before.st_mode) or path.is_symlink():
            raise LtkVerificationError("LTK installer cache entry is not a real file")
        if metadata_before.st_size != asset.size:
            raise LtkVerificationError("Cached LTK installer size is invalid")
        try:
            digest = _sha256_file(path, cancel_event)
        except OSError as error:
            raise LtkVerificationError("Cached LTK installer could not be read") from error
        if not hmac.compare_digest(digest, asset.sha256):
            raise LtkVerificationError("Cached LTK installer SHA-256 is invalid")
        try:
            signature = self._verifier.verify(path)
        except LtkVerificationError:
            raise
        except Exception as error:
            raise LtkVerificationError("Windows could not verify the LTK installer") from error
        if signature.status != "Valid" or not _is_allowed_signer(signature.subject):
            raise LtkVerificationError("LTK installer did not have the trusted valid signature")
        _check_cancelled(cancel_event)
        try:
            metadata_after = path.lstat()
        except OSError as error:
            raise LtkVerificationError("LTK installer changed during verification") from error
        identity_before = (
            metadata_before.st_dev,
            metadata_before.st_ino,
            metadata_before.st_size,
            metadata_before.st_mtime_ns,
        )
        identity_after = (
            metadata_after.st_dev,
            metadata_after.st_ino,
            metadata_after.st_size,
            metadata_after.st_mtime_ns,
        )
        if identity_before != identity_after or path.is_symlink():
            raise LtkVerificationError("LTK installer changed during verification")

    def _recent_release(self) -> LtkRelease | None:
        """Return a recently observed release, or None when it must be refetched.

        The cached payload is rebuilt through the same value objects that
        validate live GitHub metadata, so a corrupted or tampered file simply
        fails to reconstruct and falls back to a fresh network check.
        """

        path = self._release_cache_path
        try:
            if not _is_safe_regular_file(path):
                return None
            raw = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(raw, Mapping):
                return None
            checked_at = raw.get("checked_at")
            if isinstance(checked_at, bool) or not isinstance(checked_at, (int, float)):
                return None
            age = time.time() - float(checked_at)
            if not 0 <= age < RELEASE_CHECK_TTL_SECONDS:
                return None
            asset = raw.get("asset")
            if not isinstance(asset, Mapping):
                return None
            return LtkRelease(
                LtkVersion.parse(str(raw.get("version", ""))),
                LtkReleaseAsset(
                    str(asset.get("name", "")),
                    str(asset.get("url", "")),
                    int(asset.get("size", 0)),
                    str(asset.get("digest", "")),
                ),
            )
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError):
            return None

    def _remember_release(self, release: LtkRelease) -> None:
        """Record the freshly observed release; failures are never fatal."""

        try:
            self._ensure_cache_directory()
            atomic_write_json(
                self._release_cache_path,
                {
                    "schema_version": RELEASE_CHECK_SCHEMA_VERSION,
                    "checked_at": time.time(),
                    "version": str(release.version),
                    "asset": {
                        "name": release.asset.name,
                        "url": release.asset.url,
                        "size": release.asset.size,
                        "digest": release.asset.digest,
                    },
                },
            )
        except (LtkCompanionError, OSError, ValueError):
            return

    def _ensure_cache_directory(self) -> None:
        try:
            self._cache_dir.mkdir(parents=True, exist_ok=True)
        except OSError as error:
            raise LtkDownloadError("LTK installer cache could not be created") from error
        if self._cache_dir.is_symlink() or not self._cache_dir.is_dir():
            raise LtkDownloadError("LTK installer cache is not a real directory")

    @staticmethod
    def _discard_cache_file(path: Path) -> None:
        try:
            if path.is_dir() and not path.is_symlink():
                raise LtkVerificationError("Invalid LTK cache entry is a directory")
            path.unlink(missing_ok=True)
        except LtkVerificationError:
            raise
        except OSError as error:
            raise LtkVerificationError("Invalid LTK cache entry could not be removed") from error

    def _prune_obsolete_installers(self, current: Path) -> None:
        """Best-effort prune only obsolete exact installer files in our cache."""

        try:
            entries = tuple(self._cache_dir.iterdir())
        except OSError:
            return
        changed = False
        for entry in entries:
            if entry == current or _INSTALLER_PATTERN.fullmatch(entry.name) is None:
                continue
            try:
                metadata = entry.lstat()
            except OSError:
                continue
            if not stat.S_ISREG(metadata.st_mode) or entry.is_symlink():
                continue
            try:
                entry.unlink()
            except OSError:
                continue
            changed = True
        if changed:
            _fsync_directory(self._cache_dir)

    def _launch_existing(self, executable: Path) -> None:
        try:
            started = self._launcher(executable)
        except Exception as error:
            raise LtkLaunchError("Installed LTK Manager could not be launched") from error
        if started is False:
            raise LtkLaunchError("Installed LTK Manager could not be launched")

    def _launch_verified_installer(self, installer: Path) -> None:
        try:
            started = self._installer_launcher(installer, LTK_INSTALLER_SWITCHES)
        except Exception as error:
            raise LtkLaunchError("Verified LTK installer could not be started") from error
        if started is False:
            raise LtkLaunchError("Verified LTK installer could not be started")
        # The installer replaces LTK's executable, so any memoized lookup for
        # the previous version must not be served afterwards.
        invalidate = getattr(self._locator, "invalidate", None)
        if callable(invalidate):
            with suppress(Exception):
                invalidate()

    def _ensure_open(self) -> None:
        if self._closed:
            raise LtkClosedError("LTK companion is closed")


def _validate_download_url(url: str) -> None:
    parsed = urlparse(url)
    try:
        port = parsed.port
    except ValueError as error:
        raise ValueError("LTK download URL has an invalid port") from error
    hostname = parsed.hostname.casefold() if parsed.hostname else None
    if (
        parsed.scheme.casefold() != "https"
        or hostname not in _GITHUB_DOWNLOAD_HOSTS
        or parsed.username is not None
        or parsed.password is not None
        or port not in {None, 443}
        or bool(parsed.fragment)
    ):
        raise ValueError("LTK download URL is not on an allowed HTTPS GitHub host")
    if hostname == "github.com" and not parsed.path.startswith(_OFFICIAL_DOWNLOAD_PREFIX):
        raise ValueError("LTK GitHub download URL is not for the official repository")


def _validate_asset_download_url(url: str, name: str, version: str) -> None:
    _validate_download_url(url)
    parsed = urlparse(url)
    if parsed.hostname and parsed.hostname.casefold() == "github.com":
        expected_path = f"{_OFFICIAL_DOWNLOAD_PREFIX}v{version}/{name}"
        if parsed.path != expected_path:
            raise ValueError("LTK GitHub URL does not match the selected tag and asset")


def _installer_version_text(name: str) -> str:
    match = _INSTALLER_PATTERN.fullmatch(name)
    if match is None:
        raise ValueError("Invalid LTK installer filename")
    return ".".join(match.groups())


def _close_response(response: HttpResponse) -> None:
    with suppress(Exception):
        response.close()


def _check_cancelled(cancel_event: Event | None) -> None:
    if cancel_event is not None and cancel_event.is_set():
        raise LtkCancelled("LTK companion operation was cancelled")


def _sha256_file(path: Path, cancel_event: Event | None = None) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(_DOWNLOAD_CHUNK_BYTES):
            _check_cancelled(cancel_event)
            digest.update(chunk)
    _check_cancelled(cancel_event)
    return digest.hexdigest()


def _system_powershell_path() -> Path:
    windows_root = _windows_directory()
    return windows_root / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"


def _windows_directory() -> Path:
    """Get the immutable OS Windows directory without trusting process environment."""

    if os.name != "nt":
        return Path(r"C:\Windows")
    buffer = ctypes.create_unicode_buffer(32_768)
    try:
        length = ctypes.windll.kernel32.GetWindowsDirectoryW(buffer, len(buffer))
    except (AttributeError, OSError):
        length = 0
    if not isinstance(length, int) or not 0 < length < len(buffer):
        return Path(r"C:\Windows")
    return Path(buffer.value)


def _run_powershell(
    arguments: Sequence[str],
    environment: Mapping[str, str],
    timeout: float,
    creation_flags: int,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        list(arguments),
        capture_output=True,
        text=True,
        check=False,
        timeout=timeout,
        env=dict(environment),
        creationflags=creation_flags,
    )


def _is_allowed_signer(subject: str) -> bool:
    for component in subject.split(","):
        key, separator, value = component.partition("=")
        if (
            separator
            and key.strip().casefold() in {"cn", "o"}
            and value.strip().casefold() == LTK_SIGNER_NAME.casefold()
        ):
            return True
    return False


def _registry_string(registry_module: Any, key: Any, name: str) -> str | None:
    try:
        value, _kind = registry_module.QueryValueEx(key, name)
    except OSError:
        return None
    return value.strip() if isinstance(value, str) and value.strip() else None


def _display_icon_path(value: str) -> Path | None:
    expanded = _expand_windows_environment(value.strip())
    if not expanded:
        return None
    if expanded.startswith('"'):
        closing_quote = expanded.find('"', 1)
        if closing_quote < 0:
            return None
        raw_path = expanded[1:closing_quote]
    else:
        raw_path = expanded.split(",", 1)[0].strip()
    if not raw_path:
        return None
    icon_path = Path(raw_path).expanduser()
    if not icon_path.is_absolute():
        return None
    return _absolute_path(icon_path)


def _expand_windows_environment(value: str) -> str:
    expanded = os.path.expandvars(value)
    for name, environment_value in os.environ.items():

        def substitute_environment(
            _match: re.Match[str], replacement: str = environment_value
        ) -> str:
            return replacement

        expanded = re.sub(
            rf"%{re.escape(name)}%",
            substitute_environment,
            expanded,
            flags=re.IGNORECASE,
        )
    return expanded.strip()


def _try_parse_version(value: str | None) -> LtkVersion | None:
    if value is None:
        return None
    normalized = value.strip()
    if normalized[:1].casefold() == "v":
        normalized = normalized[1:]
    match = _WINDOWS_VERSION_PATTERN.fullmatch(normalized)
    if match is None:
        return None
    return LtkVersion(*(int(component) for component in match.groups()))


def _read_file_version(path: Path) -> LtkVersion | None:
    if os.name != "nt":
        return None
    powershell = _system_powershell_path()
    if powershell.is_symlink() or not powershell.is_file():
        return None
    environment = dict(os.environ)
    environment[_FILE_VERSION_PATH_ENV] = str(path)
    arguments = (
        str(powershell),
        "-NoLogo",
        "-NoProfile",
        "-NonInteractive",
        "-Command",
        _FILE_VERSION_SCRIPT,
    )
    try:
        completed = _run_powershell(
            arguments,
            environment,
            10.0,
            getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0 or not isinstance(completed.stdout, str):
        return None
    return _try_parse_version(completed.stdout)


def _known_install_roots() -> tuple[Path, ...]:
    roots: list[Path] = []
    local_app_data = os.environ.get("LOCALAPPDATA")
    program_files = os.environ.get("PROGRAMFILES")
    program_files_x86 = os.environ.get("PROGRAMFILES(X86)")
    if local_app_data:
        local = Path(local_app_data)
        roots.extend((local / "LTK Manager", local / "Programs" / "LTK Manager"))
    for value in (program_files, program_files_x86):
        if value:
            roots.append(Path(value) / "LTK Manager")
    return tuple(roots)


def _is_safe_regular_file(path: Path) -> bool:
    try:
        value = os.stat(path, follow_symlinks=False)
    except OSError:
        return False
    return stat.S_ISREG(value.st_mode) and not path.is_symlink()


def _executable_identity(path: Path) -> tuple[int, int, int] | None:
    """Return a cheap identity for an executable, or None when unavailable."""

    try:
        value = os.stat(path, follow_symlinks=False)
    except OSError:
        return None
    if not stat.S_ISREG(value.st_mode):
        return None
    return (value.st_size, value.st_mtime_ns, value.st_ino)


def _known_companion_cache_roots() -> tuple[Path, ...]:
    roots: list[Path] = []
    for variable in ("APPDATA", "LOCALAPPDATA"):
        value = os.environ.get(variable)
        if value:
            roots.append(Path(value) / APP_NAME / "cache")
    return tuple(roots)


def _executable_name_keys() -> frozenset[str]:
    return frozenset(name.casefold() for name in LTK_EXECUTABLE_NAMES)


def _path_is_within(path: Path, root: Path) -> bool:
    normalized_path = os.path.normcase(os.path.abspath(path))
    normalized_root = os.path.normcase(os.path.abspath(root))
    try:
        return os.path.commonpath((normalized_path, normalized_root)) == normalized_root
    except ValueError:
        return False


def _absolute_path(path: str | os.PathLike[str]) -> Path:
    return Path(os.path.abspath(os.path.expanduser(os.fspath(path))))


def _launch_executable(path: Path) -> bool:
    subprocess.Popen(
        [str(path)],
        cwd=str(path.parent),
        close_fds=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    return True


def _launch_installer(path: Path, switches: tuple[str, ...]) -> bool:
    subprocess.Popen(
        [str(path), *switches],
        cwd=str(path.parent),
        close_fds=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    return True


def _fsync_directory(path: Path) -> None:
    try:
        descriptor = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    except OSError:
        pass
    finally:
        os.close(descriptor)


__all__ = [
    "AuthenticodeSignature",
    "AuthenticodeVerifier",
    "ExecutableLauncher",
    "FileVersionReader",
    "InstallerLauncher",
    "LTK_EXECUTABLE_NAMES",
    "LTK_INSTALLER_SWITCHES",
    "LtkCancelled",
    "LtkClosedError",
    "LtkCompanion",
    "LtkCompanionError",
    "LtkCompanionResult",
    "LtkCompanionStatus",
    "LtkDownloadError",
    "LtkInstallLocator",
    "LtkInstallation",
    "LtkInstallationLocator",
    "LtkLaunchError",
    "LtkPreparationResult",
    "LtkPreparationStatus",
    "LtkRelease",
    "LtkReleaseAsset",
    "LtkReleaseClient",
    "LtkReleaseError",
    "LtkReleaseProvider",
    "LtkVerificationError",
    "LtkVersion",
    "MAX_LTK_INSTALLER_BYTES",
    "PowerShellRunner",
    "PowerShellAuthenticodeVerifier",
    "RegistryUninstallEntry",
    "UninstallRegistry",
    "WindowsUninstallRegistry",
]
