from __future__ import annotations

from pathlib import Path

import pytest

from build import BuildTarget, build_arguments, build_target, clean_outputs


def project_layout(tmp_path: Path) -> tuple[Path, Path, Path]:
    root = tmp_path / "repository"
    package = root / "src" / "league_skin_manager"
    package.mkdir(parents=True)
    entrypoint = package / "__main__.py"
    entrypoint.write_text("def main(): pass\n", encoding="utf-8")
    return root, package, entrypoint


def test_build_arguments_use_package_entrypoint_without_elevation(tmp_path: Path) -> None:
    root, _package, entrypoint = project_layout(tmp_path)
    target = BuildTarget("LeagueSkinManagerVN", entrypoint, root / "build_main")

    arguments = build_arguments(target, project_root=root, dist_dir=root / "dist")

    assert "--onefile" in arguments
    assert "--noconsole" in arguments
    assert "--uac-admin" not in arguments
    assert arguments[-1] == str(entrypoint.resolve())
    collect_index = arguments.index("--collect-submodules")
    assert arguments[collect_index + 1] == "league_skin_manager"
    hidden_imports = [
        arguments[index + 1]
        for index, argument in enumerate(arguments)
        if argument == "--hidden-import"
    ]
    assert hidden_imports == ["pystray", "pystray._win32"]
    assert str(root / "src") in arguments


def test_clean_removes_only_verified_repo_output_directories(tmp_path: Path) -> None:
    root, _package, _entrypoint = project_layout(tmp_path)
    outputs = [root / "build_main", root / "build_uninstall", root / "dist"]
    for output in outputs:
        output.mkdir()
        (output / "artifact").write_text("temporary", encoding="utf-8")
    outside = tmp_path / "outside"
    outside.mkdir()
    sentinel = outside / "keep.txt"
    sentinel.write_text("keep", encoding="utf-8")

    clean_outputs(root, outputs)

    assert not any(output.exists() for output in outputs)
    assert sentinel.read_text(encoding="utf-8") == "keep"

    with pytest.raises(RuntimeError, match="unverified"):
        clean_outputs(root, [outside])
    assert sentinel.exists()


def test_build_target_requires_a_nonempty_executable(tmp_path: Path) -> None:
    root, _package, entrypoint = project_layout(tmp_path)
    target = BuildTarget("LeagueSkinManagerVN", entrypoint, root / "build_main")
    captured: list[list[str]] = []

    def successful_runner(arguments: list[str]) -> None:
        captured.append(arguments)
        dist = Path(arguments[arguments.index("--distpath") + 1])
        name = arguments[arguments.index("--name") + 1]
        dist.mkdir(parents=True)
        (dist / f"{name}.exe").write_bytes(b"executable")

    executable = build_target(
        target,
        project_root=root,
        dist_dir=root / "dist",
        runner=successful_runner,
    )

    assert executable.read_bytes() == b"executable"
    assert len(captured) == 1

    executable.unlink()
    with pytest.raises(RuntimeError, match="did not produce"):
        build_target(
            target,
            project_root=root,
            dist_dir=root / "dist",
            runner=lambda _arguments: None,
        )


def test_build_rejects_entrypoint_outside_package(tmp_path: Path) -> None:
    root, _package, _entrypoint = project_layout(tmp_path)
    outside = root / "legacy_main.py"
    outside.write_text("pass\n", encoding="utf-8")
    target = BuildTarget("Legacy", outside, root / "build_main")

    with pytest.raises(RuntimeError, match="outside the package"):
        build_arguments(target, project_root=root, dist_dir=root / "dist")
