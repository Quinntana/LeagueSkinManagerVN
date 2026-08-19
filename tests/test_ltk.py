"""Tests for the LTK Manager adapter.

The verification path is the security boundary of this application: an
unverified installer must never reach execution. Those tests are the reason
this file is long.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path
from typing import Any

import pytest
import requests

from league_skin_manager import ltk
from league_skin_manager.ltk import (
    LtkDownloadError,
    LtkLaunchError,
    LtkReleaseError,
    LtkVerificationError,
    ReleaseAsset,
    ReleaseClient,
)

VERSION = "1.13.3"
NAME = f"LTK.Manager_{VERSION}_x64-setup.exe"
URL = f"https://github.com/LeagueToolkit/ltk-manager/releases/download/v{VERSION}/{NAME}"
BODY = b"pretend installer bytes"
SHA = hashlib.sha256(BODY).hexdigest()


def asset(**overrides: Any) -> ReleaseAsset:
    values: dict[str, Any] = {
        "name": NAME,
        "url": URL,
        "size": len(BODY),
        "digest": f"sha256:{SHA}",
    }
    values.update(overrides)
    return ReleaseAsset(**values)


def release_payload(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "tag_name": f"v{VERSION}",
        "draft": False,
        "prerelease": False,
        "assets": [
            {
                "name": NAME,
                "browser_download_url": URL,
                "size": len(BODY),
                "digest": f"sha256:{SHA}",
            }
        ],
    }
    payload.update(overrides)
    return payload


class FakeResponse:
    def __init__(self, *, payload: Any = None, body: bytes = b"", url: str = URL) -> None:
        self._payload = payload
        self._body = body
        self.url = url

    def raise_for_status(self) -> None:
        return None

    def json(self) -> Any:
        return self._payload

    def iter_content(self, chunk_size: int = 1) -> Any:
        for start in range(0, len(self._body), chunk_size):
            yield self._body[start : start + chunk_size]

    def close(self) -> None:
        return None


class FakeSession:
    def __init__(self, response: Any) -> None:
        self._response = response

    def get(self, _url: str, **_kwargs: Any) -> Any:
        if isinstance(self._response, Exception):
            raise self._response
        return self._response

    def close(self) -> None:
        return None


def completed(stdout: str, returncode: int = 0) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr="")


def signature_runner(status: str, subject: str) -> Any:
    def run(_args: Any, _env: Any, _timeout: float) -> subprocess.CompletedProcess[str]:
        return completed(json.dumps({"Status": status, "Subject": subject}))

    return run


# --- asset validation -----------------------------------------------------


def test_a_well_formed_asset_is_accepted() -> None:
    item = asset()
    assert item.sha256 == SHA
    assert item.name == NAME


@pytest.mark.parametrize(
    "name",
    [
        "LTK.Manager_1.13.3_x86-setup.exe",
        "LTK.Manager_1.13.3.exe",
        "ltk-manager-setup.exe",
        "LTK.Manager_1.13.3_x64-setup.msi",
        "evil.exe",
    ],
)
def test_a_non_matching_installer_name_is_rejected(name: str) -> None:
    with pytest.raises(ValueError, match="exact signed x64 NSIS installer"):
        asset(name=name)


@pytest.mark.parametrize("size", [0, -1, ltk.MAX_INSTALLER_BYTES + 1, True])
def test_an_out_of_range_size_is_rejected(size: Any) -> None:
    with pytest.raises(ValueError, match="size is outside"):
        asset(size=size)


@pytest.mark.parametrize(
    "digest", ["", SHA, "sha256:nothex", "md5:" + "a" * 32, "sha256:" + "a" * 63]
)
def test_a_malformed_digest_is_rejected(digest: str) -> None:
    with pytest.raises(ValueError, match="digest must be sha256"):
        asset(digest=digest)


@pytest.mark.parametrize(
    "url",
    [
        f"http://github.com/LeagueToolkit/ltk-manager/releases/download/v{VERSION}/{NAME}",
        f"https://evil.example.com/LeagueToolkit/ltk-manager/releases/download/v{VERSION}/{NAME}",
        f"https://github.com/attacker/ltk-manager/releases/download/v{VERSION}/{NAME}",
        f"https://user:pass@github.com/LeagueToolkit/ltk-manager/releases/download/v{VERSION}/{NAME}",
        f"https://github.com:8443/LeagueToolkit/ltk-manager/releases/download/v{VERSION}/{NAME}",
    ],
)
def test_an_untrusted_download_url_is_rejected(url: str) -> None:
    with pytest.raises(ValueError):
        asset(url=url)


def test_a_url_for_a_different_tag_is_rejected() -> None:
    wrong = f"https://github.com/LeagueToolkit/ltk-manager/releases/download/v9.9.9/{NAME}"
    with pytest.raises(ValueError, match="does not match the selected tag"):
        asset(url=wrong)


def test_the_github_release_cdn_host_is_allowed() -> None:
    asset(url="https://objects.githubusercontent.com/some/opaque/path")


# --- release parsing ------------------------------------------------------


def test_the_latest_release_is_parsed() -> None:
    client = ReleaseClient(session=FakeSession(FakeResponse(payload=release_payload())))
    assert client.latest().name == NAME


def test_a_draft_release_is_rejected() -> None:
    client = ReleaseClient(session=FakeSession(FakeResponse(payload=release_payload(draft=True))))
    with pytest.raises(LtkReleaseError, match="draft or prerelease"):
        client.latest()


def test_a_prerelease_is_rejected() -> None:
    payload = release_payload(prerelease=True)
    client = ReleaseClient(session=FakeSession(FakeResponse(payload=payload)))
    with pytest.raises(LtkReleaseError, match="draft or prerelease"):
        client.latest()


@pytest.mark.parametrize("tag", ["1.13.3", "release-1.13.3", "v1.13", "vNEXT", ""])
def test_a_malformed_tag_is_rejected(tag: str) -> None:
    payload = release_payload(tag_name=tag)
    client = ReleaseClient(session=FakeSession(FakeResponse(payload=payload)))
    with pytest.raises(LtkReleaseError, match="vMAJOR"):
        client.latest()


def test_a_release_without_the_expected_asset_is_rejected() -> None:
    payload = release_payload(assets=[{"name": "something-else.exe"}])
    client = ReleaseClient(session=FakeSession(FakeResponse(payload=payload)))
    with pytest.raises(LtkReleaseError, match="exactly one matching"):
        client.latest()


def test_a_release_with_duplicate_assets_is_rejected() -> None:
    entry = {"name": NAME, "browser_download_url": URL, "size": 1, "digest": f"sha256:{SHA}"}
    client = ReleaseClient(
        session=FakeSession(FakeResponse(payload=release_payload(assets=[entry, entry])))
    )
    with pytest.raises(LtkReleaseError, match="exactly one matching"):
        client.latest()


def test_a_network_failure_is_wrapped() -> None:
    client = ReleaseClient(session=FakeSession(requests.ConnectionError("offline")))
    with pytest.raises(LtkReleaseError, match="Could not read"):
        client.latest()


# --- download verification ------------------------------------------------


def test_a_matching_download_is_written(tmp_path: Path) -> None:
    client = ReleaseClient(session=FakeSession(FakeResponse(body=BODY)))
    target = tmp_path / NAME
    client.download(asset(), target)
    assert target.read_bytes() == BODY


def test_a_digest_mismatch_leaves_nothing_behind(tmp_path: Path) -> None:
    client = ReleaseClient(session=FakeSession(FakeResponse(body=b"tampered bytes!!")))
    target = tmp_path / NAME
    bad = asset(size=len(b"tampered bytes!!"))
    with pytest.raises(LtkDownloadError, match="SHA-256"):
        client.download(bad, target)
    assert not target.exists()
    assert list(tmp_path.iterdir()) == []


def test_an_oversized_download_is_rejected(tmp_path: Path) -> None:
    client = ReleaseClient(session=FakeSession(FakeResponse(body=b"x" * 5000)))
    with pytest.raises(LtkDownloadError, match="exceeded"):
        client.download(asset(), tmp_path / NAME)


def test_a_short_download_is_rejected(tmp_path: Path) -> None:
    client = ReleaseClient(session=FakeSession(FakeResponse(body=b"tiny")))
    with pytest.raises(LtkDownloadError, match="size mismatch"):
        client.download(asset(), tmp_path / NAME)


def test_a_redirect_to_an_untrusted_host_is_rejected(tmp_path: Path) -> None:
    response = FakeResponse(body=BODY, url="https://evil.example.com/payload.exe")
    client = ReleaseClient(session=FakeSession(response))
    with pytest.raises(LtkDownloadError):
        client.download(asset(), tmp_path / NAME)


# --- Authenticode ---------------------------------------------------------


def test_a_valid_signature_from_the_publisher_is_accepted(tmp_path: Path) -> None:
    target = tmp_path / NAME
    target.write_bytes(BODY)
    subject = ltk.verify_signature(
        target, runner=signature_runner("Valid", f"CN={ltk.SIGNER_NAME}, O={ltk.SIGNER_NAME}")
    )
    assert ltk.SIGNER_NAME in subject


@pytest.mark.parametrize("status", ["NotSigned", "HashMismatch", "UnknownError", "Invalid"])
def test_an_invalid_signature_status_is_rejected(tmp_path: Path, status: str) -> None:
    target = tmp_path / NAME
    target.write_bytes(BODY)
    with pytest.raises(LtkVerificationError, match="not Valid"):
        ltk.verify_signature(target, runner=signature_runner(status, f"CN={ltk.SIGNER_NAME}"))


@pytest.mark.parametrize(
    "subject",
    ["CN=Someone Else", "O=Evil Corp", "", "CN=Natoken LLC Fake", "CN=NotNatoken LLC"],
)
def test_a_signature_from_another_publisher_is_rejected(tmp_path: Path, subject: str) -> None:
    target = tmp_path / NAME
    target.write_bytes(BODY)
    with pytest.raises(LtkVerificationError, match="signer is not"):
        ltk.verify_signature(target, runner=signature_runner("Valid", subject))


def test_a_failed_powershell_run_is_rejected(tmp_path: Path) -> None:
    target = tmp_path / NAME
    target.write_bytes(BODY)

    def failing(_a: Any, _e: Any, _t: float) -> subprocess.CompletedProcess[str]:
        return completed("", returncode=1)

    with pytest.raises(LtkVerificationError, match="rejected"):
        ltk.verify_signature(target, runner=failing)


def test_unparseable_signature_output_is_rejected(tmp_path: Path) -> None:
    target = tmp_path / NAME
    target.write_bytes(BODY)

    def garbage(_a: Any, _e: Any, _t: float) -> subprocess.CompletedProcess[str]:
        return completed("not json")

    with pytest.raises(LtkVerificationError, match="invalid Authenticode"):
        ltk.verify_signature(target, runner=garbage)


def test_a_missing_installer_file_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(LtkVerificationError, match="real file"):
        ltk.verify_signature(tmp_path / "absent.exe", runner=signature_runner("Valid", "CN=x"))


# --- install orchestration ------------------------------------------------


def test_install_verifies_before_launching(tmp_path: Path) -> None:
    order: list[str] = []

    def verifier(_path: Path) -> str:
        order.append("verify")
        return f"CN={ltk.SIGNER_NAME}"

    def launcher(_path: Path, switches: Any) -> bool:
        order.append("launch")
        assert tuple(switches) == ltk.INSTALLER_SWITCHES
        return True

    client = ReleaseClient(session=FakeSession(FakeResponse(payload=release_payload(), body=BODY)))
    client.latest = lambda: asset()  # type: ignore[method-assign]
    ltk.install(client, tmp_path, verifier=verifier, launcher=launcher)

    assert order == ["verify", "launch"]


def test_install_never_launches_an_unverified_installer(tmp_path: Path) -> None:
    launched: list[Path] = []

    def verifier(_path: Path) -> str:
        raise LtkVerificationError("bad signature")

    client = ReleaseClient(session=FakeSession(FakeResponse(body=BODY)))
    client.latest = lambda: asset()  # type: ignore[method-assign]

    with pytest.raises(LtkVerificationError):
        ltk.install(
            client, tmp_path, verifier=verifier, launcher=lambda p, s: launched.append(p) or True
        )
    assert launched == []
    assert not (tmp_path / NAME).exists()


def test_the_installer_runs_passively_and_does_not_auto_launch() -> None:
    """/P is passive; /R would start LTK, which must not happen before seeding."""

    assert ltk.INSTALLER_SWITCHES == ("/P",)
    assert "/R" not in ltk.INSTALLER_SWITCHES


# --- locate ---------------------------------------------------------------


def test_locate_finds_an_installed_executable(tmp_path: Path) -> None:
    root = tmp_path / "LTK Manager"
    root.mkdir()
    (root / "ltk-manager.exe").write_bytes(b"exe")
    assert ltk.locate([root]) == root / "ltk-manager.exe"


def test_locate_returns_none_when_absent(tmp_path: Path) -> None:
    assert ltk.locate([tmp_path / "nowhere"]) is None


def test_uninstaller_is_found_next_to_the_executable(tmp_path: Path) -> None:
    root = tmp_path / "LTK Manager"
    root.mkdir()
    (root / "uninstall.exe").write_bytes(b"exe")
    assert ltk.uninstaller([root]) == root / "uninstall.exe"


def test_launching_a_missing_executable_raises(tmp_path: Path) -> None:
    with pytest.raises(LtkLaunchError, match="real file"):
        ltk.launch(tmp_path / "absent.exe")


# --- storage and settings -------------------------------------------------


def test_storage_defaults_to_the_data_root(tmp_path: Path) -> None:
    assert ltk.resolve_storage_dir(tmp_path) == tmp_path


def test_a_configured_mod_storage_path_is_honoured(tmp_path: Path) -> None:
    elsewhere = tmp_path / "D_drive_mods"
    (tmp_path / "settings.json").write_text(
        json.dumps({"firstRunComplete": True, "modStoragePath": str(elsewhere)}), encoding="utf-8"
    )
    assert ltk.resolve_storage_dir(tmp_path) == elsewhere


def test_a_null_mod_storage_path_falls_back(tmp_path: Path) -> None:
    (tmp_path / "settings.json").write_text(
        json.dumps({"firstRunComplete": True, "modStoragePath": None}), encoding="utf-8"
    )
    assert ltk.resolve_storage_dir(tmp_path) == tmp_path


def test_the_requested_settings_are_applied(tmp_path: Path) -> None:
    path = tmp_path / "settings.json"
    path.write_text(
        json.dumps({"firstRunComplete": True, "enforceSkinhackScan": True, "theme": "dark"}),
        encoding="utf-8",
    )
    assert ltk.apply_settings(tmp_path) is True
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved["enforceSkinhackScan"] is False
    assert saved["theme"] == "dark", "unrelated LTK settings must survive"


def test_the_watcher_setting_is_never_touched(tmp_path: Path) -> None:
    """LTK defaults it off, seeding does not need it, and forcing it on makes
    packages adopt and self-enable mid-session."""

    assert "watcherEnabled" not in ltk.MANAGED_SETTINGS
    path = tmp_path / "settings.json"
    path.write_text(
        json.dumps({"firstRunComplete": True, "watcherEnabled": False}), encoding="utf-8"
    )
    ltk.apply_settings(tmp_path)
    assert json.loads(path.read_text(encoding="utf-8"))["watcherEnabled"] is False


def test_applying_settings_twice_is_a_no_op(tmp_path: Path) -> None:
    path = tmp_path / "settings.json"
    path.write_text(
        json.dumps({"firstRunComplete": True, "enforceSkinhackScan": True}), encoding="utf-8"
    )
    assert ltk.apply_settings(tmp_path) is True
    assert ltk.apply_settings(tmp_path) is False


def test_settings_are_never_written_from_scratch(tmp_path: Path) -> None:
    """LTK requires firstRunComplete and discards a file lacking it."""

    assert ltk.apply_settings(tmp_path) is False
    assert not (tmp_path / "settings.json").exists()


def test_a_partial_settings_file_is_not_patched(tmp_path: Path) -> None:
    path = tmp_path / "settings.json"
    path.write_text(json.dumps({"enforceSkinhackScan": True}), encoding="utf-8")
    assert ltk.apply_settings(tmp_path) is False


# --- the enabled baseline -------------------------------------------------


def library(*enabled: str) -> dict[str, Any]:
    return {
        "version": 1,
        "mods": [{"id": name} for name in enabled],
        "profiles": [
            {
                "id": "p1",
                "name": "Default",
                "enabledMods": list(enabled),
                "modOrder": list(enabled),
                "layerStates": {name: {} for name in enabled},
            }
        ],
    }


def test_enabled_mods_are_cleared(tmp_path: Path) -> None:
    """171 of 173 champions have more than one skin; all-on means all compete."""

    path = tmp_path / "library.json"
    path.write_text(json.dumps(library("a", "b", "c")), encoding="utf-8")

    assert ltk.clear_enabled_mods(tmp_path) == 3

    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved["profiles"][0]["enabledMods"] == []
    assert saved["profiles"][0]["layerStates"] == {}


def test_the_mods_themselves_are_kept(tmp_path: Path) -> None:
    """Clearing switches skins off; it never removes them from the library."""

    path = tmp_path / "library.json"
    path.write_text(json.dumps(library("a", "b")), encoding="utf-8")
    ltk.clear_enabled_mods(tmp_path)
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert [m["id"] for m in saved["mods"]] == ["a", "b"]
    assert saved["version"] == 1


def test_clearing_an_already_clear_library_is_a_no_op(tmp_path: Path) -> None:
    path = tmp_path / "library.json"
    path.write_text(json.dumps(library()), encoding="utf-8")
    assert ltk.clear_enabled_mods(tmp_path) == 0


def test_clearing_a_missing_library_is_harmless(tmp_path: Path) -> None:
    assert ltk.clear_enabled_mods(tmp_path) == 0


def test_a_malformed_library_is_left_alone(tmp_path: Path) -> None:
    path = tmp_path / "library.json"
    path.write_text("[]", encoding="utf-8")
    assert ltk.clear_enabled_mods(tmp_path) == 0
    assert path.read_text(encoding="utf-8") == "[]"
