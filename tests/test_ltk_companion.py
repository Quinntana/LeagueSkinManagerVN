from __future__ import annotations

import hashlib
import json
import os
import secrets
import subprocess
from collections.abc import Iterator, Mapping, Sequence
from pathlib import Path
from threading import Event, Lock, Thread
from typing import Any

import pytest

from league_skin_manager import ltk_companion as ltk_module
from league_skin_manager.ltk_companion import (
    MAX_LTK_INSTALLER_BYTES,
    AuthenticodeSignature,
    LtkCancelled,
    LtkClosedError,
    LtkCompanion,
    LtkCompanionStatus,
    LtkDownloadError,
    LtkInstallation,
    LtkInstallLocator,
    LtkLaunchError,
    LtkPreparationStatus,
    LtkRelease,
    LtkReleaseAsset,
    LtkReleaseClient,
    LtkReleaseError,
    LtkVerificationError,
    LtkVersion,
    PowerShellAuthenticodeVerifier,
    RegistryUninstallEntry,
)


def installer_name(version: str = "1.2.3") -> str:
    return f"LTK.Manager_{version}_x64-setup.exe"


def installer_url(version: str = "1.2.3", *, host: str = "github.com") -> str:
    name = installer_name(version)
    if host == "github.com":
        return f"https://github.com/LeagueToolkit/ltk-manager/releases/download/v{version}/{name}"
    return f"https://{host}/download/{name}"


def release_for(data: bytes = b"signed installer", version: str = "1.2.3") -> LtkRelease:
    return LtkRelease(
        LtkVersion.parse(version),
        LtkReleaseAsset(
            installer_name(version),
            installer_url(version),
            len(data),
            f"sha256:{hashlib.sha256(data).hexdigest()}",
        ),
    )


def release_payload(data: bytes = b"signed installer", version: str = "1.2.3") -> dict[str, Any]:
    return {
        "tag_name": f"v{version}",
        "draft": False,
        "prerelease": False,
        "assets": [
            {
                "name": installer_name(version),
                "browser_download_url": installer_url(version),
                "size": len(data),
                "digest": f"sha256:{hashlib.sha256(data).hexdigest()}",
            }
        ],
    }


class Response:
    def __init__(
        self,
        *,
        payload: object | None = None,
        chunks: Sequence[bytes] = (),
        url: str = "https://api.github.com/repos/LeagueToolkit/ltk-manager/releases/latest",
        failure: Exception | None = None,
    ) -> None:
        self.payload = payload
        self.chunks = tuple(chunks)
        self.url = url
        self.failure = failure
        self.closed = False

    def raise_for_status(self) -> None:
        if self.failure is not None:
            raise self.failure

    def json(self) -> object:
        return self.payload

    def iter_content(self, chunk_size: int) -> Iterator[bytes]:
        assert chunk_size > 0
        yield from self.chunks

    def close(self) -> None:
        self.closed = True


class Session:
    def __init__(self, *responses: Response | Exception) -> None:
        self.responses = list(responses)
        self.calls: list[tuple[str, dict[str, object]]] = []
        self.closed = False

    def get(self, url: str, **kwargs: object) -> Response:
        self.calls.append((url, kwargs))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response

    def close(self) -> None:
        self.closed = True


def test_release_client_selects_only_exact_official_signed_x64_asset() -> None:
    data = b"release"
    payload = release_payload(data)
    payload["assets"].extend(
        [
            {
                "name": "LTK.Manager_1.2.3_arm64-setup.exe",
                "browser_download_url": installer_url(),
                "size": len(data),
                "digest": f"sha256:{hashlib.sha256(data).hexdigest()}",
            },
            {
                "name": "LTK.Manager_1.2.3_x64-portable.zip",
                "browser_download_url": installer_url(),
                "size": len(data),
                "digest": f"sha256:{hashlib.sha256(data).hexdigest()}",
            },
        ]
    )
    response = Response(payload=payload)
    session = Session(response)

    release = LtkReleaseClient(session).latest()

    assert release.version == LtkVersion(1, 2, 3)
    assert release.asset.name == installer_name()
    assert release.asset.sha256 == hashlib.sha256(data).hexdigest()
    assert response.closed


def test_release_client_rejects_ambiguous_and_wrong_assets() -> None:
    ambiguous = release_payload()
    ambiguous["assets"].append(dict(ambiguous["assets"][0]))
    wrong = release_payload()
    wrong["assets"][0]["name"] = "LTK.Manager_1.2.3_x86-setup.exe"

    with pytest.raises(LtkReleaseError, match="exactly one"):
        LtkReleaseClient(Session(Response(payload=ambiguous))).latest()
    with pytest.raises(LtkReleaseError, match="exactly one"):
        LtkReleaseClient(Session(Response(payload=wrong))).latest()


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("digest", "sha256:abc"),
        ("digest", "md5:" + "0" * 64),
        ("browser_download_url", "https://evil.example/LTK.Manager_1.2.3_x64-setup.exe"),
        ("browser_download_url", installer_url("9.9.9")),
        (
            "browser_download_url",
            "https://github.com/LeagueToolkit/ltk-manager/releases/download/v1.2.3/latest.json",
        ),
        ("size", 0),
        ("size", MAX_LTK_INSTALLER_BYTES + 1),
    ],
)
def test_release_client_rejects_invalid_digest_host_and_size(field: str, value: object) -> None:
    payload = release_payload()
    payload["assets"][0][field] = value

    with pytest.raises(LtkReleaseError, match="failed validation"):
        LtkReleaseClient(Session(Response(payload=payload))).latest()


@pytest.mark.parametrize("tag", ["1.2.3", "v1.2", "v1.2.3-beta", "v01.2.3", "v1.2.3 "])
def test_release_client_rejects_malformed_tags(tag: str) -> None:
    payload = release_payload()
    payload["tag_name"] = tag

    with pytest.raises(LtkReleaseError, match="tag"):
        LtkReleaseClient(Session(Response(payload=payload))).latest()


def test_release_client_rejects_tag_asset_version_mismatch() -> None:
    payload = release_payload(version="1.2.3")
    payload["tag_name"] = "v1.2.4"

    with pytest.raises(LtkReleaseError, match="exactly one"):
        LtkReleaseClient(Session(Response(payload=payload))).latest()


def test_download_streams_exact_bytes_hashes_fsyncs_and_atomically_replaces(
    tmp_path: Path,
) -> None:
    data = b"abcdef"
    release = release_for(data)
    response = Response(
        chunks=(b"ab", b"", b"cdef"),
        url="https://release-assets.githubusercontent.com/signed-download",
    )
    destination = tmp_path / release.asset.name
    destination.write_bytes(b"old")

    result = LtkReleaseClient(Session(response)).download(release.asset, destination)

    assert result == destination
    assert destination.read_bytes() == data
    assert response.closed
    assert not list(tmp_path.glob("*.part"))


@pytest.mark.parametrize(
    ("chunks", "message"),
    [
        ((b"short",), "size mismatch"),
        ((b"wrong!",), "SHA-256"),
        ((b"toolong!",), "exceeded"),
    ],
)
def test_download_rejects_wrong_size_and_digest_and_preserves_destination(
    tmp_path: Path,
    chunks: tuple[bytes, ...],
    message: str,
) -> None:
    release = release_for(b"target")
    destination = tmp_path / release.asset.name
    destination.write_bytes(b"old")
    response = Response(
        chunks=chunks,
        url="https://objects.githubusercontent.com/signed-download",
    )

    with pytest.raises(LtkDownloadError, match=message):
        LtkReleaseClient(Session(response)).download(release.asset, destination)

    assert destination.read_bytes() == b"old"
    assert not list(tmp_path.glob("*.part"))


def test_download_rejects_bad_redirect_host_and_honours_cancellation(tmp_path: Path) -> None:
    data = b"target"
    release = release_for(data)
    bad_redirect = Response(chunks=(data,), url="https://downloads.evil.example/file")
    with pytest.raises(LtkDownloadError, match="official"):
        LtkReleaseClient(Session(bad_redirect)).download(release.asset, tmp_path / "bad.exe")

    cancelled = Event()
    cancelled.set()
    session = Session(Response(chunks=(data,), url=release.asset.url))
    with pytest.raises(LtkCancelled):
        LtkReleaseClient(session).download(release.asset, tmp_path / "cancelled.exe", cancelled)
    assert not session.calls


def test_download_wraps_preflight_filesystem_failure(tmp_path: Path) -> None:
    release = release_for(b"target")
    non_directory = tmp_path / "not-a-directory"
    non_directory.write_bytes(b"file")

    with pytest.raises(LtkDownloadError, match="official"):
        LtkReleaseClient(Session()).download(release.asset, non_directory / release.asset.name)


def test_download_never_deletes_a_partial_it_did_not_create(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data = b"target"
    release = release_for(data)
    destination = tmp_path / release.asset.name
    token = "a" * 16
    monkeypatch.setattr(secrets, "token_hex", lambda _size: token)
    partial = tmp_path / f".{destination.name}.{os.getpid()}.{token}.part"
    partial.write_bytes(b"owned by another producer")
    response = Response(chunks=(data,), url=release.asset.url)

    with pytest.raises(LtkDownloadError):
        LtkReleaseClient(Session(response)).download(release.asset, destination)

    assert partial.read_bytes() == b"owned by another producer"


def test_authenticode_uses_fixed_arguments_and_environment_only_path(tmp_path: Path) -> None:
    powershell = tmp_path / "powershell.exe"
    target = tmp_path / "name with 'quotes & metacharacters.exe"
    powershell.write_bytes(b"powershell")
    target.write_bytes(b"installer")
    calls: list[tuple[Sequence[str], Mapping[str, str], float, int]] = []

    def runner(
        arguments: Sequence[str],
        environment: Mapping[str, str],
        timeout: float,
        creation_flags: int,
    ) -> subprocess.CompletedProcess[str]:
        calls.append((arguments, environment, timeout, creation_flags))
        output = json.dumps({"Status": "Valid", "Subject": "CN=Natoken LLC, O=Natoken LLC, C=US"})
        return subprocess.CompletedProcess(arguments, 0, output, "")

    verifier = PowerShellAuthenticodeVerifier(powershell_path=powershell, runner=runner)

    signature = verifier.verify(target)

    assert signature.status == "Valid"
    arguments, environment, timeout, _flags = calls[0]
    assert "-NoProfile" in arguments
    assert "-NonInteractive" in arguments
    assert timeout == 20.0
    assert str(target) not in " ".join(arguments)
    assert environment["LSMVN_LTK_SIGNATURE_PATH"] == str(target)
    assert environment["PSMODULEPATH"] == str(powershell.parent / "Modules")
    assert "Microsoft.PowerShell.Security\\Get-AuthenticodeSignature" in arguments[-1]


@pytest.mark.parametrize(
    ("status", "subject", "message"),
    [
        ("NotSigned", "", "not Valid"),
        ("Valid", "CN=Natoken LLC Malware Division", "not Natoken LLC"),
        ("Valid", "CN=Somebody Else, O=Other", "not Natoken LLC"),
    ],
)
def test_authenticode_rejects_invalid_status_and_wrong_signer(
    tmp_path: Path,
    status: str,
    subject: str,
    message: str,
) -> None:
    powershell = tmp_path / "powershell.exe"
    target = tmp_path / "installer.exe"
    powershell.write_bytes(b"powershell")
    target.write_bytes(b"installer")

    def runner(
        arguments: Sequence[str],
        _environment: Mapping[str, str],
        _timeout: float,
        _creation_flags: int,
    ) -> subprocess.CompletedProcess[str]:
        output = json.dumps({"Status": status, "Subject": subject})
        return subprocess.CompletedProcess(arguments, 0, output, "")

    verifier = PowerShellAuthenticodeVerifier(powershell_path=powershell, runner=runner)
    with pytest.raises(LtkVerificationError, match=message):
        verifier.verify(target)


class Registry:
    def __init__(self, *entries: RegistryUninstallEntry, failure: bool = False) -> None:
        self._entries = entries
        self._failure = failure

    def entries(self) -> tuple[RegistryUninstallEntry, ...]:
        if self._failure:
            raise OSError("registry unavailable")
        return self._entries


@pytest.mark.parametrize("executable_name", ["LTK Manager.exe", "ltk-manager.exe"])
def test_locator_accepts_both_names_and_prefers_registry_display_icon(
    tmp_path: Path,
    executable_name: str,
) -> None:
    preferred = tmp_path / "Installed" / executable_name
    secondary = tmp_path / "Other" / executable_name
    preferred.parent.mkdir()
    secondary.parent.mkdir()
    preferred.write_bytes(b"preferred")
    secondary.write_bytes(b"secondary")
    entry = RegistryUninstallEntry(
        display_name="LTK Manager",
        display_version="1.2.3",
        display_icon=f'"{preferred}",0',
        install_location=str(secondary.parent),
    )
    locator = LtkInstallLocator(
        Registry(entry),
        fallbacks=(),
        temp_root=tmp_path / "Temp",
    )

    assert locator.locate() == LtkInstallation(preferred, LtkVersion(1, 2, 3))


def test_locator_falls_back_to_install_location_and_file_version(tmp_path: Path) -> None:
    executable = tmp_path / "Programs" / "LTK Manager.exe"
    executable.parent.mkdir()
    executable.write_bytes(b"manager")
    entry = RegistryUninstallEntry(
        display_name="LTK Manager",
        display_icon='"C:\\missing\\LTK Manager.exe",0',
        install_location=f'"{executable.parent}"',
    )
    reader_calls: list[Path] = []

    def version_reader(path: Path) -> str:
        reader_calls.append(path)
        return "2.4.6"

    locator = LtkInstallLocator(
        Registry(entry),
        fallbacks=(),
        file_version_reader=version_reader,
        temp_root=tmp_path / "Temp",
    )

    assert locator.locate() == LtkInstallation(executable, LtkVersion(2, 4, 6))
    assert reader_calls == [executable]


def test_locator_accepts_decorated_name_and_fourth_zero_version(tmp_path: Path) -> None:
    executable = tmp_path / "Programs" / "LTK Manager.exe"
    executable.parent.mkdir()
    executable.write_bytes(b"manager")
    entry = RegistryUninstallEntry(
        display_name="LTK Manager v2.4.6.0",
        display_version="2.4.6.0",
        display_icon=str(executable),
    )
    locator = LtkInstallLocator(
        Registry(entry),
        fallbacks=(),
        temp_root=tmp_path / "Temp",
    )

    assert locator.locate() == LtkInstallation(executable, LtkVersion(2, 4, 6))


def test_locator_uses_known_fallbacks_when_registry_is_unavailable(tmp_path: Path) -> None:
    root = tmp_path / "Programs" / "LTK Manager"
    executable = root / "ltk-manager.exe"
    root.mkdir(parents=True)
    executable.write_bytes(b"manager")
    locator = LtkInstallLocator(
        Registry(failure=True),
        fallbacks=(root,),
        file_version_reader=lambda _path: LtkVersion(3, 0, 1),
        temp_root=tmp_path / "Temp",
    )

    assert locator.locate() == LtkInstallation(executable, LtkVersion(3, 0, 1))


@pytest.mark.parametrize("unsafe_directory", ["Temp", "cache", "audit"])
def test_locator_never_treats_temp_cache_or_audit_copy_as_installed(
    tmp_path: Path,
    unsafe_directory: str,
) -> None:
    executable = tmp_path / unsafe_directory / "LTK Manager.exe"
    executable.parent.mkdir()
    executable.write_bytes(b"copy")
    entry = RegistryUninstallEntry(
        display_name="LTK Manager",
        display_version="1.2.3",
        display_icon=str(executable),
    )
    locator = LtkInstallLocator(
        Registry(entry),
        fallbacks=(executable,),
        temp_root=tmp_path / "Temp",
        excluded_roots=(tmp_path / "cache",),
    )

    assert locator.locate() is None


def test_locator_rejects_symlinks_and_entries_without_a_version(tmp_path: Path) -> None:
    target = tmp_path / "Installed" / "LTK Manager.exe"
    link = tmp_path / "Programs" / "LTK Manager.exe"
    target.parent.mkdir()
    link.parent.mkdir()
    target.write_bytes(b"manager")
    try:
        link.symlink_to(target)
    except OSError:
        pytest.skip("symlink creation is unavailable")
    entry = RegistryUninstallEntry(
        display_name="LTK Manager",
        display_icon=str(link),
    )
    locator = LtkInstallLocator(
        Registry(entry),
        fallbacks=(),
        file_version_reader=lambda _path: None,
        temp_root=tmp_path / "Temp",
    )

    assert locator.locate() is None


def test_locator_rejects_relative_registry_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    executable = tmp_path / "LTK Manager.exe"
    executable.write_bytes(b"working-directory copy")
    monkeypatch.chdir(tmp_path)
    entry = RegistryUninstallEntry(
        display_name="LTK Manager",
        display_version="1.2.3",
        display_icon="LTK Manager.exe,0",
        install_location=".",
    )
    locator = LtkInstallLocator(
        Registry(entry),
        fallbacks=(),
        temp_root=tmp_path / "Temp",
    )

    assert locator.locate() is None


class FakeVerifier:
    def __init__(self, failures: int = 0) -> None:
        self.failures = failures
        self.calls: list[Path] = []

    def verify(self, path: Path) -> AuthenticodeSignature:
        self.calls.append(path)
        if self.failures:
            self.failures -= 1
            raise LtkVerificationError("wrong signer")
        return AuthenticodeSignature("Valid", "CN=Natoken LLC")


class FakeClient:
    def __init__(
        self,
        release: LtkRelease,
        data: bytes,
        *,
        latest_error: LtkReleaseError | None = None,
        cancel_on_download: Event | None = None,
    ) -> None:
        self.release = release
        self.data = data
        self.latest_error = latest_error
        self.cancel_on_download = cancel_on_download
        self.latest_calls = 0
        self.downloads = 0
        self.closed = 0

    def latest(self) -> LtkRelease:
        self.latest_calls += 1
        if self.latest_error is not None:
            raise self.latest_error
        return self.release

    def download(
        self,
        _asset: LtkReleaseAsset,
        destination: Path,
        _cancel_event: Event | None = None,
    ) -> Path:
        self.downloads += 1
        destination.write_bytes(self.data)
        if self.cancel_on_download is not None:
            self.cancel_on_download.set()
        return destination

    def close(self) -> None:
        self.closed += 1


class FakeLocator:
    def __init__(self, installation: LtkInstallation | None) -> None:
        self.installation = installation
        self.calls = 0

    def locate(self) -> LtkInstallation | None:
        self.calls += 1
        return self.installation


def installation(tmp_path: Path, version: str = "1.2.3") -> LtkInstallation:
    executable = tmp_path / "Installed" / "LTK Manager.exe"
    executable.parent.mkdir(exist_ok=True)
    executable.write_bytes(b"manager")
    return LtkInstallation(executable, LtkVersion.parse(version))


def make_companion(
    client: FakeClient,
    locator: FakeLocator,
    verifier: FakeVerifier,
    cache_dir: Path,
    launched: list[Path],
    installed: list[tuple[Path, tuple[str, ...]]],
) -> LtkCompanion:
    def launch(path: Path) -> bool:
        launched.append(path)
        return True

    def launch_installer(path: Path, switches: tuple[str, ...]) -> bool:
        installed.append((path, switches))
        return True

    return LtkCompanion(
        client,
        locator,
        verifier,
        cache_dir,
        launcher=launch,
        installer_launcher=launch_installer,
    )


@pytest.mark.parametrize("installed_version", ["1.2.3", "2.0.0"])
def test_current_or_newer_install_skips_download_and_launches_existing(
    tmp_path: Path,
    installed_version: str,
) -> None:
    data = b"signed"
    client = FakeClient(release_for(data), data)
    current = installation(tmp_path, installed_version)
    launched: list[Path] = []
    installers: list[tuple[Path, tuple[str, ...]]] = []
    companion = make_companion(
        client,
        FakeLocator(current),
        FakeVerifier(),
        tmp_path / "cache-root",
        launched,
        installers,
    )

    result = companion.start()

    assert result.status is LtkCompanionStatus.LAUNCHED_CURRENT
    assert result.path == current.executable
    assert client.downloads == 0
    assert launched == [current.executable]
    assert not installers


def test_prepare_downloads_and_verifies_without_launching(tmp_path: Path) -> None:
    data = b"signed"
    client = FakeClient(release_for(data), data)
    launched: list[Path] = []
    installers: list[tuple[Path, tuple[str, ...]]] = []
    verifier = FakeVerifier()
    companion = make_companion(
        client,
        FakeLocator(None),
        verifier,
        tmp_path / "cache-root",
        launched,
        installers,
    )

    prepared = companion.prepare()

    assert prepared.status is LtkPreparationStatus.INSTALLER_READY
    assert prepared.installer_path is not None
    assert prepared.installer_path.read_bytes() == data
    assert client.downloads == 1
    assert verifier.calls == [prepared.installer_path]
    assert not launched
    assert not installers


def test_prepare_reports_current_without_downloading_or_launching(tmp_path: Path) -> None:
    data = b"signed"
    client = FakeClient(release_for(data), data)
    current = installation(tmp_path)
    launched: list[Path] = []
    installers: list[tuple[Path, tuple[str, ...]]] = []
    companion = make_companion(
        client,
        FakeLocator(current),
        FakeVerifier(),
        tmp_path / "cache-root",
        launched,
        installers,
    )

    prepared = companion.prepare()

    assert prepared.status is LtkPreparationStatus.CURRENT_INSTALLED
    assert prepared.installation == current
    assert prepared.installer_path is None
    assert client.downloads == 0
    assert not launched
    assert not installers


def test_outdated_or_missing_install_starts_verified_nsis_with_exact_switches(
    tmp_path: Path,
) -> None:
    data = b"signed"
    client = FakeClient(release_for(data), data)
    outdated = installation(tmp_path, "1.0.0")
    launched: list[Path] = []
    installers: list[tuple[Path, tuple[str, ...]]] = []
    companion = make_companion(
        client,
        FakeLocator(outdated),
        FakeVerifier(),
        tmp_path / "cache-root",
        launched,
        installers,
    )

    result = companion.start()

    assert result.status is LtkCompanionStatus.INSTALLER_STARTED
    assert installers == [(result.path, ("/P", "/R"))]
    assert not launched


def test_valid_cache_is_reused_only_after_size_hash_and_signature(tmp_path: Path) -> None:
    data = b"signed"
    release = release_for(data)
    client = FakeClient(release, data)
    cache = tmp_path / "cache-root"
    cache.mkdir()
    installer = cache / release.asset.name
    installer.write_bytes(data)
    verifier = FakeVerifier()
    launched: list[Path] = []
    installers: list[tuple[Path, tuple[str, ...]]] = []
    companion = make_companion(
        client,
        FakeLocator(None),
        verifier,
        cache,
        launched,
        installers,
    )

    prepared = companion.prepare()

    assert prepared.installer_path == installer
    assert client.downloads == 0
    assert verifier.calls == [installer]


def test_successful_prepare_prunes_only_obsolete_exact_installer_files(tmp_path: Path) -> None:
    data = b"signed"
    release = release_for(data)
    cache = tmp_path / "cache-root"
    cache.mkdir()
    current = cache / release.asset.name
    current.write_bytes(data)
    obsolete = cache / installer_name("1.0.0")
    obsolete.write_bytes(b"old")
    unknown = cache / "notes.txt"
    unknown.write_text("keep", encoding="utf-8")
    partial = cache / f".{installer_name('1.1.0')}.1234.deadbeefdeadbeef.part"
    partial.write_bytes(b"possibly active")
    installer_directory = cache / installer_name("1.0.1")
    installer_directory.mkdir()
    installer_symlink = cache / installer_name("1.0.2")
    try:
        installer_symlink.symlink_to(unknown)
        symlink_created = True
    except OSError:
        symlink_created = False
    companion = make_companion(
        FakeClient(release, data),
        FakeLocator(None),
        FakeVerifier(),
        cache,
        [],
        [],
    )

    companion.prepare()

    assert current.read_bytes() == data
    assert not obsolete.exists()
    assert unknown.read_text(encoding="utf-8") == "keep"
    assert partial.read_bytes() == b"possibly active"
    assert installer_directory.is_dir()
    if symlink_created:
        assert installer_symlink.is_symlink()


@pytest.mark.parametrize("corrupt", [b"short", b"tampered"])
def test_corrupt_cache_is_deleted_and_redownloaded(tmp_path: Path, corrupt: bytes) -> None:
    data = b"signed!"
    release = release_for(data)
    cache = tmp_path / "cache-root"
    cache.mkdir()
    installer = cache / release.asset.name
    installer.write_bytes(corrupt)
    client = FakeClient(release, data)
    companion = make_companion(
        client,
        FakeLocator(None),
        FakeVerifier(),
        cache,
        [],
        [],
    )

    prepared = companion.prepare()

    assert prepared.installer_path == installer
    assert installer.read_bytes() == data
    assert client.downloads == 1


def test_bad_cached_signature_is_deleted_then_fresh_copy_is_verified(tmp_path: Path) -> None:
    data = b"signed"
    release = release_for(data)
    cache = tmp_path / "cache-root"
    cache.mkdir()
    installer = cache / release.asset.name
    installer.write_bytes(data)
    client = FakeClient(release, data)
    verifier = FakeVerifier(failures=1)
    companion = make_companion(client, FakeLocator(None), verifier, cache, [], [])

    prepared = companion.prepare()

    assert prepared.installer_path == installer
    assert client.downloads == 1
    assert verifier.calls == [installer, installer]


def test_wrong_signer_on_fresh_download_is_deleted_and_never_launched(tmp_path: Path) -> None:
    data = b"signed"
    release = release_for(data)
    cache = tmp_path / "cache-root"
    client = FakeClient(release, data)
    verifier = FakeVerifier(failures=1)
    launched: list[Path] = []
    installers: list[tuple[Path, tuple[str, ...]]] = []
    companion = make_companion(
        client,
        FakeLocator(None),
        verifier,
        cache,
        launched,
        installers,
    )

    with pytest.raises(LtkVerificationError, match="wrong signer"):
        companion.start()

    assert not (cache / release.asset.name).exists()
    assert not launched
    assert not installers


def test_release_check_failure_launches_existing_with_distinct_status(tmp_path: Path) -> None:
    data = b"signed"
    error = LtkReleaseError("offline")
    client = FakeClient(release_for(data), data, latest_error=error)
    existing = installation(tmp_path, "1.0.0")
    launched: list[Path] = []
    installers: list[tuple[Path, tuple[str, ...]]] = []
    companion = make_companion(
        client,
        FakeLocator(existing),
        FakeVerifier(),
        tmp_path / "cache-root",
        launched,
        installers,
    )

    result = companion.start()

    assert result.status is LtkCompanionStatus.EXISTING_LAUNCHED_AFTER_RELEASE_CHECK_FAILURE
    assert result.release is None
    assert result.check_error == "offline"
    assert launched == [existing.executable]
    assert not installers


def test_release_check_failure_without_existing_install_propagates(tmp_path: Path) -> None:
    data = b"signed"
    client = FakeClient(release_for(data), data, latest_error=LtkReleaseError("offline"))
    companion = make_companion(
        client,
        FakeLocator(None),
        FakeVerifier(),
        tmp_path / "cache-root",
        [],
        [],
    )

    with pytest.raises(LtkReleaseError, match="offline"):
        companion.start()


def test_cancellation_before_or_during_prepare_never_launches(tmp_path: Path) -> None:
    data = b"signed"
    release = release_for(data)
    cancelled = Event()
    cancelled.set()
    client = FakeClient(release, data)
    launched: list[Path] = []
    installers: list[tuple[Path, tuple[str, ...]]] = []
    companion = make_companion(
        client,
        FakeLocator(None),
        FakeVerifier(),
        tmp_path / "before",
        launched,
        installers,
    )
    with pytest.raises(LtkCancelled):
        companion.start(cancelled)
    assert client.latest_calls == 0

    during = Event()
    during_client = FakeClient(release, data, cancel_on_download=during)
    during_companion = make_companion(
        during_client,
        FakeLocator(None),
        FakeVerifier(),
        tmp_path / "during",
        launched,
        installers,
    )
    with pytest.raises(LtkCancelled):
        during_companion.start(during)
    assert not (tmp_path / "during" / release.asset.name).exists()
    assert not launched
    assert not installers


def test_failed_launch_is_reported(tmp_path: Path) -> None:
    data = b"signed"
    current = installation(tmp_path)
    companion = LtkCompanion(
        FakeClient(release_for(data), data),
        FakeLocator(current),
        FakeVerifier(),
        tmp_path / "cache-root",
        launcher=lambda _path: False,
    )

    with pytest.raises(LtkLaunchError):
        companion.start()


def test_void_launcher_callback_is_treated_as_success(tmp_path: Path) -> None:
    data = b"signed"
    current = installation(tmp_path)
    launched: list[Path] = []
    companion = LtkCompanion(
        FakeClient(release_for(data), data),
        FakeLocator(current),
        FakeVerifier(),
        tmp_path / "cache-root",
        launcher=launched.append,
    )

    result = companion.start()

    assert result.status is LtkCompanionStatus.LAUNCHED_CURRENT
    assert launched == [current.executable]


def test_close_is_idempotent_and_prevents_more_work(tmp_path: Path) -> None:
    data = b"signed"
    client = FakeClient(release_for(data), data)
    companion = make_companion(
        client,
        FakeLocator(None),
        FakeVerifier(),
        tmp_path / "cache-root",
        [],
        [],
    )

    companion.close()
    companion.close()

    assert client.closed == 1
    with pytest.raises(LtkClosedError):
        companion.prepare()


def test_prepare_and_start_are_serialized(tmp_path: Path) -> None:
    data = b"signed"
    release = release_for(data)
    entered = Event()
    release_first = Event()
    concurrency_lock = Lock()
    active = 0
    maximum_active = 0

    class BlockingClient(FakeClient):
        def latest(self) -> LtkRelease:
            nonlocal active, maximum_active
            with concurrency_lock:
                active += 1
                maximum_active = max(maximum_active, active)
            entered.set()
            release_first.wait(timeout=5)
            with concurrency_lock:
                active -= 1
            return super().latest()

    client = BlockingClient(release, data)
    companion = make_companion(
        client,
        FakeLocator(None),
        FakeVerifier(),
        tmp_path / "cache-root",
        [],
        [],
    )
    errors: list[BaseException] = []

    def prepare() -> None:
        try:
            companion.prepare()
        except BaseException as error:
            errors.append(error)

    first = Thread(target=prepare)
    second = Thread(target=prepare)
    first.start()
    assert entered.wait(timeout=5)
    second.start()
    release_first.set()
    first.join(timeout=5)
    second.join(timeout=5)

    assert not errors
    assert not first.is_alive()
    assert not second.is_alive()
    assert maximum_active == 1
    assert client.latest_calls == 2


def test_version_ordering_is_numeric_not_lexical() -> None:
    assert LtkVersion.parse("1.10.0") > LtkVersion.parse("1.9.99")
    assert str(LtkVersion.from_tag("v12.3.4")) == "12.3.4"
    with pytest.raises(ValueError):
        LtkVersion.parse("v1.2.3")


def test_release_client_wraps_network_error_and_does_not_close_injected_session() -> None:
    session = Session(OSError("offline"))
    client = LtkReleaseClient(session)

    with pytest.raises(LtkReleaseError, match="official"):
        client.latest()
    client.close()

    assert not session.closed


def test_locator_display_icon_does_not_execute_or_interpolate_arguments(tmp_path: Path) -> None:
    executable = tmp_path / "Installed" / "LTK Manager.exe"
    executable.parent.mkdir()
    executable.write_bytes(b"manager")
    marker = tmp_path / "must-not-exist"
    entry = RegistryUninstallEntry(
        display_name="LTK Manager",
        display_version="1.2.3",
        display_icon=f'"{executable}",0 & echo unsafe > "{marker}"',
    )
    locator = LtkInstallLocator(
        Registry(entry),
        fallbacks=(),
        temp_root=tmp_path / "Temp",
    )

    assert locator.locate() == LtkInstallation(executable, LtkVersion(1, 2, 3))
    assert not marker.exists()


def test_download_does_not_replace_existing_file_when_request_fails(tmp_path: Path) -> None:
    data = b"signed"
    release = release_for(data)
    destination = tmp_path / release.asset.name
    destination.write_bytes(b"existing")
    response = Response(
        chunks=(data,),
        url=release.asset.url,
        failure=OSError("connection reset"),
    )

    with pytest.raises(LtkDownloadError):
        LtkReleaseClient(Session(response)).download(release.asset, destination)

    assert destination.read_bytes() == b"existing"
    assert not any(path.suffix == ".part" for path in tmp_path.iterdir())


def test_authenticode_requires_real_powershell_and_target_files(tmp_path: Path) -> None:
    missing_powershell = tmp_path / "missing-powershell.exe"
    missing_target = tmp_path / "missing-installer.exe"
    verifier = PowerShellAuthenticodeVerifier(powershell_path=missing_powershell)

    with pytest.raises(LtkVerificationError, match="real file"):
        verifier.verify(missing_target)

    target = tmp_path / "installer.exe"
    target.write_bytes(b"installer")
    with pytest.raises(LtkVerificationError, match="PowerShell"):
        verifier.verify(target)


def test_system_powershell_path_does_not_trust_systemroot_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SYSTEMROOT", r"C:\attacker-controlled")

    powershell = ltk_module._system_powershell_path()

    assert "attacker-controlled" not in str(powershell)


def test_installer_asset_constructor_normalizes_digest_case() -> None:
    data = b"signed"
    digest = hashlib.sha256(data).hexdigest().upper()
    asset = LtkReleaseAsset(installer_name(), installer_url(), len(data), f"sha256:{digest}")

    assert asset.digest == f"sha256:{digest.casefold()}"


def test_prepare_rejects_unexpected_download_destination(tmp_path: Path) -> None:
    data = b"signed"
    release = release_for(data)

    class WrongDestinationClient(FakeClient):
        def download(
            self,
            _asset: LtkReleaseAsset,
            destination: Path,
            _cancel_event: Event | None = None,
        ) -> Path:
            destination.write_bytes(self.data)
            return destination.parent / "different.exe"

    client = WrongDestinationClient(release, data)
    cache = tmp_path / "cache-root"
    companion = make_companion(client, FakeLocator(None), FakeVerifier(), cache, [], [])

    with pytest.raises(LtkDownloadError, match="unexpected"):
        companion.prepare()

    assert not (cache / release.asset.name).exists()


def test_prepare_never_mutates_unrelated_ltk_or_cslol_data(tmp_path: Path) -> None:
    data = b"signed"
    ltk_data = tmp_path / "ltk-data"
    cslol_data = tmp_path / "cslol-data"
    ltk_data.mkdir()
    cslol_data.mkdir()
    ltk_marker = ltk_data / "settings.json"
    cslol_marker = cslol_data / "profile.txt"
    ltk_marker.write_text("ltk", encoding="utf-8")
    cslol_marker.write_text("cslol", encoding="utf-8")
    companion = make_companion(
        FakeClient(release_for(data), data),
        FakeLocator(None),
        FakeVerifier(),
        tmp_path / "dedicated-cache",
        [],
        [],
    )

    companion.prepare()

    assert ltk_marker.read_text(encoding="utf-8") == "ltk"
    assert cslol_marker.read_text(encoding="utf-8") == "cslol"
    assert set(path.name for path in tmp_path.iterdir()) == {
        "ltk-data",
        "cslol-data",
        "dedicated-cache",
    }


def test_path_comparisons_use_absolute_cache_destination(tmp_path: Path) -> None:
    data = b"signed"
    cache = tmp_path / "cache-root"
    client = FakeClient(release_for(data), data)
    companion = make_companion(client, FakeLocator(None), FakeVerifier(), cache, [], [])

    prepared = companion.prepare()

    assert prepared.installer_path is not None
    assert prepared.installer_path.is_absolute()
    assert os.path.samefile(prepared.installer_path.parent, cache)
