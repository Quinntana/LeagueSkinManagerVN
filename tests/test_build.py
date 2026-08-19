"""Tests for the single-artifact PyInstaller build."""

from __future__ import annotations

import runpy
from pathlib import Path

import pytest

import build


def test_the_entrypoint_exists_and_is_in_the_package() -> None:
    assert build.ENTRYPOINT.is_file()
    assert build.ENTRYPOINT.parent.name == "league_skin_manager"


def test_the_entrypoint_is_loadable_as_a_script() -> None:
    """PyInstaller runs the entrypoint as a script, so it must import absolutely."""

    namespace = runpy.run_path(str(build.ENTRYPOINT), run_name="not_main")
    assert callable(namespace["main"])


def test_the_entrypoint_avoids_relative_imports() -> None:
    source = build.ENTRYPOINT.read_text(encoding="utf-8")
    assert "from league_skin_manager" in source
    assert "from .app" not in source


def test_arguments_produce_one_windowless_onefile_binary() -> None:
    arguments = build.build_arguments()
    assert "--onefile" in arguments
    assert "--noconsole" in arguments
    assert arguments[arguments.index("--name") + 1] == build.APP_NAME
    assert arguments[-1] == str(build.ENTRYPOINT.resolve())


def test_the_tray_and_tk_backends_are_hidden_imports() -> None:
    """PyInstaller cannot see these through pystray's dynamic backend import."""

    arguments = build.build_arguments()
    for hidden in ("pystray", "pystray._win32", "tkinter"):
        assert hidden in arguments


def test_only_one_artifact_is_built() -> None:
    """The uninstaller and setup executables are gone; uninstall is a tray action."""

    arguments = build.build_arguments()
    assert arguments.count("--name") == 1
    assert "--add-data" not in arguments


@pytest.mark.parametrize("name", ["src", "..", "C:/Windows", "build_other"])
def test_cleaning_refuses_paths_outside_the_known_outputs(tmp_path: Path, name: str) -> None:
    with pytest.raises(RuntimeError, match="Refusing to clean"):
        build._verified_output_path(tmp_path, tmp_path / name)


def test_cleaning_accepts_the_known_outputs(tmp_path: Path) -> None:
    for name in ("build_main", "dist"):
        assert build._verified_output_path(tmp_path, tmp_path / name).name == name


def test_a_file_where_an_output_directory_belongs_is_refused(tmp_path: Path) -> None:
    target = tmp_path / "dist"
    target.write_text("not a directory", encoding="utf-8")
    with pytest.raises(RuntimeError, match="not a directory"):
        build._verified_output_path(tmp_path, target)


def make_project(root: Path) -> Path:
    """A minimal tree with the entrypoint where build_arguments expects it."""

    package = root / "src" / "league_skin_manager"
    package.mkdir(parents=True)
    (package / "__main__.py").write_text("def main() -> None: ...\n", encoding="utf-8")
    return root


def test_build_validates_the_produced_executable(tmp_path: Path) -> None:
    make_project(tmp_path)
    calls: list[list[str]] = []

    def runner(arguments: list[str]) -> None:
        calls.append(arguments)
        dist = tmp_path / "dist"
        dist.mkdir(parents=True, exist_ok=True)
        (dist / f"{build.APP_NAME}.exe").write_bytes(b"MZ")

    result = build.build(project_root=tmp_path, dist_dir=tmp_path / "dist", runner=runner)
    assert result.name == f"{build.APP_NAME}.exe"
    assert len(calls) == 1


def test_build_fails_when_pyinstaller_produces_nothing(tmp_path: Path) -> None:
    make_project(tmp_path)
    with pytest.raises(RuntimeError, match="did not produce"):
        build.build(project_root=tmp_path, dist_dir=tmp_path / "dist", runner=lambda _a: None)
