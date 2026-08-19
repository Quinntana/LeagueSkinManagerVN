"""Locate, install, and launch the official LTK Manager.

LTK ships Tauri's own updater, so this module never manages versions: it
verifies the *first* install and then gets out of the way.  That removes the
release cache, the installed-versus-latest comparison, the installer pruning,
and the file-version probe that the previous design needed.

What remains is the part that has to be right.  The installer is downloaded
only from an allowed GitHub host, checked against GitHub's declared size and
SHA-256, and required to carry a valid Authenticode signature from the fixed
publisher before it is ever executed.  Verification failures are never
downgraded to warnings.
"""

from __future__ import annotations

import ctypes
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import shutil
import subprocess
from collections.abc import Iterable, Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlparse

import requests

from .atomic import atomic_write_json, read_json
from .config import LTK_BUNDLE_IDENTIFIER, LTK_PROCESS_NAMES, LTK_RELEASES_URL
from .hashing import is_real_directory, is_real_file

LOGGER = logging.getLogger(__name__)

SIGNER_NAME = "Natoken LLC"
MAX_INSTALLER_BYTES = 256 * 1024 * 1024
INSTALLER_SWITCHES = ("/P",)
"""Tauri NSIS passive mode: a progress bar, no prompts, and no auto-launch.

``/R`` is deliberately omitted so the installer does not start LTK. Seeding
happens while LTK is closed, and the application launches it once afterwards.
"""

EXECUTABLE_NAMES = ("ltk-manager.exe", "LTK Manager.exe")

MANAGED_SETTINGS: dict[str, object] = {
    # Requested explicitly. Every other LTK setting is the user's to configure.
    "enforceSkinhackScan": False,
}

_CHUNK = 256 * 1024
_DIGEST = re.compile(r"^sha256:([0-9a-fA-F]{64})$")
_INSTALLER = re.compile(
    r"^LTK\.Manager_(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)_x64-setup\.exe$"
)
_VERSION = re.compile(r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$")
_DOWNLOAD_HOSTS = frozenset(
    {"github.com", "objects.githubusercontent.com", "release-assets.githubusercontent.com"}
)
_DOWNLOAD_PREFIX = "/LeagueToolkit/ltk-manager/releases/download/"
_SIGNATURE_PATH_ENV = "LSMVN_LTK_SIGNATURE_PATH"

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


class LtkError(RuntimeError):
    """Base class for an LTK companion failure."""


class LtkReleaseError(LtkError):
    """The official release metadata could not be read or was unusable."""


class LtkDownloadError(LtkError):
    """The installer could not be downloaded or did not match its metadata."""


class LtkVerificationError(LtkError):
    """The installer failed size, digest, or signature verification."""


class LtkLaunchError(LtkError):
    """LTK or its installer could not be started."""


class ProcessLookup(Protocol):
    def is_any_running(self, names: Sequence[str]) -> bool:
        """Return whether any named process is running."""


@dataclass(frozen=True, slots=True)
class ReleaseAsset:
    """The exact signed x64 NSIS installer published for one release."""

    name: str
    url: str
    size: int
    digest: str

    def __post_init__(self) -> None:
        match = _INSTALLER.fullmatch(self.name)
        if match is None:
            raise ValueError("LTK asset is not the exact signed x64 NSIS installer")
        if isinstance(self.size, bool) or not 0 < self.size <= MAX_INSTALLER_BYTES:
            raise ValueError("LTK installer size is outside the allowed range")
        digest = _DIGEST.fullmatch(self.digest)
        if digest is None:
            raise ValueError("LTK installer digest must be sha256 plus 64 hex digits")
        _validate_download_url(self.url, self.name, ".".join(match.groups()))
        object.__setattr__(self, "digest", f"sha256:{digest.group(1).casefold()}")

    @property
    def sha256(self) -> str:
        return self.digest.removeprefix("sha256:")


# --------------------------------------------------------------------------
# Release metadata and download
# --------------------------------------------------------------------------


class ReleaseClient:
    """Reads and streams only the official GitHub latest-release asset."""

    def __init__(self, session: Any | None = None, *, timeout: float = 15.0) -> None:
        if timeout <= 0:
            raise ValueError("timeout must be positive")
        self._owns_session = session is None
        self._session = requests.Session() if session is None else session
        self._timeout = timeout

    def close(self) -> None:
        if self._owns_session:
            with suppress(Exception):
                self._session.close()

    def latest(self) -> ReleaseAsset:
        response = None
        try:
            response = self._session.get(
                LTK_RELEASES_URL,
                timeout=self._timeout,
                headers={"Accept": "application/vnd.github+json"},
            )
            response.raise_for_status()
            payload = response.json()
        except Exception as error:
            raise LtkReleaseError("Could not read the official LTK latest release") from error
        finally:
            if response is not None:
                with suppress(Exception):
                    response.close()
        return _parse_release(payload)

    def download(self, asset: ReleaseAsset, destination: Path) -> Path:
        """Download and verify size and SHA-256 before the file is usable."""

        destination = Path(os.path.abspath(destination))
        destination.parent.mkdir(parents=True, exist_ok=True)
        if not is_real_directory(destination.parent):
            raise LtkDownloadError("LTK installer cache is not a real directory")

        partial = destination.with_name(f".{destination.name}.{secrets.token_hex(8)}.part")
        response = None
        try:
            response = self._session.get(
                asset.url,
                stream=True,
                timeout=(10.0, 60.0),
                headers={"Accept": "application/octet-stream"},
            )
            response.raise_for_status()
            final_url = getattr(response, "url", asset.url)
            if isinstance(final_url, str):
                _validate_download_url(final_url, asset.name, _version_text(asset.name))

            written = 0
            digest = hashlib.sha256()
            with partial.open("xb") as output:
                for chunk in response.iter_content(chunk_size=_CHUNK):
                    if not chunk:
                        continue
                    written += len(chunk)
                    if written > asset.size:
                        raise LtkDownloadError(
                            f"LTK installer exceeded its declared {asset.size} bytes"
                        )
                    output.write(chunk)
                    digest.update(chunk)
                output.flush()
                os.fsync(output.fileno())

            if written != asset.size:
                raise LtkDownloadError(
                    f"LTK installer size mismatch: expected {asset.size}, received {written}"
                )
            if not hmac.compare_digest(digest.hexdigest(), asset.sha256):
                raise LtkDownloadError("LTK installer SHA-256 did not match GitHub metadata")
            os.replace(partial, destination)
            return destination
        except LtkError:
            raise
        except Exception as error:
            raise LtkDownloadError("Could not download the official LTK installer") from error
        finally:
            if response is not None:
                with suppress(Exception):
                    response.close()
            with suppress(OSError):
                partial.unlink(missing_ok=True)


def _parse_release(payload: object) -> ReleaseAsset:
    if not isinstance(payload, dict):
        raise LtkReleaseError("GitHub returned invalid LTK release metadata")
    tag = payload.get("tag_name")
    if not isinstance(tag, str) or not tag.startswith("v"):
        raise LtkReleaseError("LTK release tag is not vMAJOR.MINOR.PATCH")
    version = tag[1:]
    if _VERSION.fullmatch(version) is None:
        raise LtkReleaseError("LTK release tag is not vMAJOR.MINOR.PATCH")
    if payload.get("draft") is True or payload.get("prerelease") is True:
        raise LtkReleaseError("GitHub latest release was draft or prerelease")

    assets = payload.get("assets")
    if not isinstance(assets, list):
        raise LtkReleaseError("LTK release contained no asset list")
    expected = f"LTK.Manager_{version}_x64-setup.exe"
    matching = [item for item in assets if isinstance(item, dict) and item.get("name") == expected]
    if len(matching) != 1:
        raise LtkReleaseError("LTK release must contain exactly one matching x64 installer")

    candidate = matching[0]
    url = candidate.get("browser_download_url")
    size = candidate.get("size")
    digest = candidate.get("digest")
    if not isinstance(url, str) or not isinstance(digest, str):
        raise LtkReleaseError("LTK installer URL or digest metadata is invalid")
    if not isinstance(size, int) or isinstance(size, bool):
        raise LtkReleaseError("LTK installer size metadata is invalid")
    try:
        return ReleaseAsset(expected, url, size, digest)
    except ValueError as error:
        raise LtkReleaseError("LTK installer metadata failed validation") from error


def _validate_download_url(url: str, name: str, version: str) -> None:
    parsed = urlparse(url)
    try:
        port = parsed.port
    except ValueError as error:
        raise ValueError("LTK download URL has an invalid port") from error
    hostname = parsed.hostname.casefold() if parsed.hostname else None
    if (
        parsed.scheme.casefold() != "https"
        or hostname not in _DOWNLOAD_HOSTS
        or parsed.username is not None
        or parsed.password is not None
        or port not in {None, 443}
        or bool(parsed.fragment)
    ):
        raise ValueError("LTK download URL is not on an allowed HTTPS GitHub host")
    if hostname == "github.com":
        expected = f"{_DOWNLOAD_PREFIX}v{version}/{name}"
        if parsed.path != expected:
            raise ValueError("LTK GitHub URL does not match the selected tag and asset")


def _version_text(name: str) -> str:
    match = _INSTALLER.fullmatch(name)
    if match is None:
        raise ValueError("Invalid LTK installer filename")
    return ".".join(match.groups())


# --------------------------------------------------------------------------
# Authenticode
# --------------------------------------------------------------------------

PowerShellRunner = Any


def verify_signature(path: Path, *, runner: Any = None, timeout: float = 20.0) -> str:
    """Require a valid Authenticode signature from the fixed publisher.

    Returns the certificate subject. Any doubt raises: an unsigned, invalid,
    or differently-signed installer must never reach execution.
    """

    executable = Path(os.path.abspath(path))
    if not is_real_file(executable):
        raise LtkVerificationError("LTK installer must be a real file")
    powershell = _system_powershell()
    if not is_real_file(powershell):
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

    run = runner or _run_powershell
    try:
        completed = run(arguments, environment, timeout)
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
        raise LtkVerificationError(f"LTK installer signer is not {SIGNER_NAME}")
    return subject


def _is_allowed_signer(subject: str) -> bool:
    for component in subject.split(","):
        key, separator, value = component.partition("=")
        if (
            separator
            and key.strip().casefold() in {"cn", "o"}
            and value.strip().casefold() == SIGNER_NAME.casefold()
        ):
            return True
    return False


def _run_powershell(
    arguments: Sequence[str], environment: Mapping[str, str], timeout: float
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        list(arguments),
        capture_output=True,
        text=True,
        check=False,
        timeout=timeout,
        env=dict(environment),
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


def _system_powershell() -> Path:
    return _windows_directory() / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"


def _windows_directory() -> Path:
    """Resolve the OS directory without trusting the process environment."""

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


# --------------------------------------------------------------------------
# Installation
# --------------------------------------------------------------------------


def install_roots() -> tuple[Path, ...]:
    roots: list[Path] = []
    local = os.environ.get("LOCALAPPDATA")
    if local:
        roots.extend((Path(local) / "LTK Manager", Path(local) / "Programs" / "LTK Manager"))
    for variable in ("PROGRAMFILES", "PROGRAMFILES(X86)"):
        value = os.environ.get(variable)
        if value:
            roots.append(Path(value) / "LTK Manager")
    return tuple(roots)


def locate(roots: Iterable[Path] | None = None) -> Path | None:
    """Return LTK's executable, or None when it is not installed.

    Version is deliberately not read: nothing in this application compares
    versions, so the file-version probe (which spawned a helper process) is
    gone with it.
    """

    for root in roots if roots is not None else install_roots():
        for name in EXECUTABLE_NAMES:
            candidate = root / name
            if is_real_file(candidate):
                return candidate
    return None


def uninstaller(roots: Iterable[Path] | None = None) -> Path | None:
    """Return LTK's own uninstaller, used when removing an LTK we installed."""

    for root in roots if roots is not None else install_roots():
        candidate = root / "uninstall.exe"
        if is_real_file(candidate):
            return candidate
    return None


def install(
    client: ReleaseClient,
    cache_dir: Path,
    *,
    verifier: Any = None,
    launcher: Any = None,
) -> Path:
    """Fetch, verify, and run the official installer. Returns the installer path.

    Every check is fail-closed and happens before execution: allowed host,
    declared size, declared SHA-256, then a valid Authenticode signature from
    the fixed publisher.
    """

    asset = client.latest()
    cache_dir.mkdir(parents=True, exist_ok=True)
    installer = cache_dir / asset.name
    client.download(asset, installer)

    verify = verifier or verify_signature
    try:
        subject = verify(installer)
    except LtkError:
        with suppress(OSError):
            installer.unlink(missing_ok=True)
        raise
    LOGGER.info("Verified LTK installer %s signed by %s", asset.name, subject)

    start = launcher or _launch
    if not start(installer, INSTALLER_SWITCHES):
        raise LtkLaunchError("The verified LTK installer could not be started")
    return installer


def launch(executable: Path, *, launcher: Any = None) -> bool:
    """Start an installed LTK executable."""

    if not is_real_file(executable):
        raise LtkLaunchError(f"LTK executable is not a real file: {executable}")
    start = launcher or _launch
    return bool(start(executable, ()))


def _launch(path: Path, switches: Sequence[str]) -> bool:
    subprocess.Popen(
        [str(path), *switches],
        cwd=str(path.parent),
        close_fds=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    return True


def is_running(lookup: ProcessLookup) -> bool:
    return lookup.is_any_running(LTK_PROCESS_NAMES)


# --------------------------------------------------------------------------
# Storage and settings
# --------------------------------------------------------------------------


def default_data_dir() -> Path:
    """LTK's data root, derived from its fixed Tauri bundle identifier."""

    appdata = os.environ.get("APPDATA")
    base = Path(appdata) if appdata else Path.home() / "AppData" / "Roaming"
    return base / LTK_BUNDLE_IDENTIFIER


def resolve_storage_dir(data_dir: Path | None = None) -> Path:
    """Return where LTK keeps its library, honouring its own modStoragePath.

    This is the only value read out of LTK's settings; an LTK that has never
    run has no settings file, in which case the default root applies.
    """

    root = data_dir or default_data_dir()
    raw = read_json(root / "settings.json", default=None)
    if isinstance(raw, dict):
        configured = raw.get("modStoragePath")
        if isinstance(configured, str) and configured.strip():
            return Path(configured)
    return root


def apply_settings(data_dir: Path | None = None) -> bool:
    """Apply the few LTK settings this application has an opinion about.

    Deliberately narrow. Every other key is the user's to configure, and this
    only ever edits a complete file LTK has already written: LTK requires
    firstRunComplete and discards a file lacking it, restoring its own
    defaults, so a settings file is never authored from scratch.

    watcherEnabled is deliberately *not* touched. LTK defaults it off, seeding
    is proven not to depend on it, and forcing it on makes packages adopt (and
    self-enable) mid-session while the user is looking at the library.
    """

    root = data_dir or default_data_dir()
    path = root / "settings.json"
    raw = read_json(path, default=None)
    if not isinstance(raw, dict) or "firstRunComplete" not in raw:
        return False

    updated = dict(raw)
    changed = False
    for key, value in MANAGED_SETTINGS.items():
        if updated.get(key) != value:
            updated[key] = value
            changed = True
    if not changed:
        return False
    try:
        atomic_write_json(path, updated)
    except OSError:
        LOGGER.warning("Could not update LTK settings", exc_info=True)
        return False
    LOGGER.info("Applied LTK settings: %s", ", ".join(sorted(MANAGED_SETTINGS)))
    return True


def clear_enabled_mods(data_dir: Path | None = None) -> int:
    """Return the library to its baseline: present, but nothing switched on.

    LTK enables a package the moment it adopts it, with or without the file
    watcher. Since the library holds every skin in the source -- 171 of 173
    champions have more than one, and Miss Fortune alone has 23 -- leaving
    them all on means every champion has a dozen skins competing and the one
    that wins changes silently whenever the source updates.

    So the baseline is nothing enabled, and the user turns on what they want.
    This is the only field outside settings.json that is ever written, and it
    is skipped entirely while LTK is running.
    """

    root = data_dir or default_data_dir()
    path = root / "library.json"
    raw = read_json(path, default=None)
    if not isinstance(raw, dict):
        return 0
    profiles = raw.get("profiles")
    if not isinstance(profiles, list):
        return 0

    cleared = 0
    updated_profiles: list[Any] = []
    for profile in profiles:
        if not isinstance(profile, dict):
            updated_profiles.append(profile)
            continue
        enabled = profile.get("enabledMods")
        if isinstance(enabled, list) and enabled:
            cleared += len(enabled)
            profile = dict(profile)
            profile["enabledMods"] = []
            if isinstance(profile.get("layerStates"), dict):
                profile["layerStates"] = {}
        updated_profiles.append(profile)

    if not cleared:
        return 0
    payload = dict(raw)
    payload["profiles"] = updated_profiles
    try:
        atomic_write_json(path, payload)
    except OSError:
        LOGGER.warning("Could not clear LTK's enabled mods", exc_info=True)
        return 0
    LOGGER.info("Cleared %d enabled mods; the library baseline is nothing enabled", cleared)
    return cleared


def remove_data(data_dir: Path | None = None) -> bool:
    """Delete LTK's entire data root. Used only by uninstall."""

    root = data_dir or default_data_dir()
    if not is_real_directory(root):
        return False
    try:
        shutil.rmtree(root)
    except OSError:
        LOGGER.warning("Could not remove LTK's data directory", exc_info=True)
        return False
    return True


__all__ = [
    "EXECUTABLE_NAMES",
    "INSTALLER_SWITCHES",
    "SIGNER_NAME",
    "LtkDownloadError",
    "LtkError",
    "LtkLaunchError",
    "LtkReleaseError",
    "LtkVerificationError",
    "ReleaseAsset",
    "ReleaseClient",
    "MANAGED_SETTINGS",
    "apply_settings",
    "clear_enabled_mods",
    "default_data_dir",
    "install",
    "install_roots",
    "is_running",
    "launch",
    "locate",
    "remove_data",
    "resolve_storage_dir",
    "uninstaller",
    "verify_signature",
]
