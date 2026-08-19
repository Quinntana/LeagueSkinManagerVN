"""Build the single Windows executable with PyInstaller.

One artifact, not three.  The previous design shipped the application, a
separate uninstaller, and a setup executable that embedded both as payload;
the application is now portable, and uninstall is a tray action rather than a
program, so there is nothing else to build.
"""

from __future__ import annotations

import os
import shutil
from collections.abc import Callable
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
SRC_DIR = PROJECT_ROOT / "src"
PACKAGE_DIR = SRC_DIR / "league_skin_manager"
DIST_DIR = PROJECT_ROOT / "dist"
BUILD_DIR = PROJECT_ROOT / "build_main"

APP_NAME = "LeagueSkinManagerVN"
ENTRYPOINT = PACKAGE_DIR / "__main__.py"
HIDDEN_IMPORTS = ("tkinter", "tkinter.ttk", "pystray", "pystray._win32")

_ALLOWED_OUTPUT_NAMES = frozenset({"build_main", "dist"})

BuildRunner = Callable[[list[str]], None]


def _verified_output_path(project_root: Path, path: Path) -> Path:
    """Accept only the named direct children this build is allowed to clean."""

    root = project_root.resolve()
    candidate = Path(os.path.abspath(path))
    if candidate.parent != root or candidate.name not in _ALLOWED_OUTPUT_NAMES:
        raise RuntimeError(f"Refusing to clean unverified build path: {candidate}")
    if candidate.is_symlink():
        raise RuntimeError(f"Refusing to clean symbolic-link build path: {candidate}")
    if candidate.exists():
        resolved = candidate.resolve()
        if resolved.parent != root or resolved.name != candidate.name:
            raise RuntimeError(f"Refusing to clean redirected build path: {candidate}")
        if not candidate.is_dir():
            raise RuntimeError(f"Build output path is not a directory: {candidate}")
    return candidate


def clean_outputs(project_root: Path = PROJECT_ROOT) -> None:
    for output in (BUILD_DIR, DIST_DIR):
        verified = _verified_output_path(project_root, output)
        if verified.exists():
            print(f"Cleaning {verified}", flush=True)
            shutil.rmtree(verified)


def build_arguments(*, project_root: Path = PROJECT_ROOT, dist_dir: Path = DIST_DIR) -> list[str]:
    root = project_root.resolve()
    source_dir = root / "src"
    package_dir = source_dir / "league_skin_manager"
    entrypoint = (package_dir / "__main__.py").resolve()
    try:
        entrypoint.relative_to(package_dir)
    except ValueError as error:
        raise RuntimeError(f"Build entrypoint is outside the package: {entrypoint}") from error
    if not entrypoint.is_file():
        raise RuntimeError(f"Build entrypoint does not exist: {entrypoint}")

    # Derived from project_root, not the module constant, so a build driven at
    # another root verifies against that root rather than this file's.
    work_dir = _verified_output_path(root, root / BUILD_DIR.name)
    verified_dist = _verified_output_path(root, dist_dir)
    arguments = [
        "--onefile",
        "--noconfirm",
        "--clean",
        "--noconsole",
        "--name",
        APP_NAME,
        "--distpath",
        str(verified_dist),
        "--workpath",
        str(work_dir),
        "--specpath",
        str(work_dir),
        "--paths",
        str(source_dir),
    ]
    for hidden in HIDDEN_IMPORTS:
        arguments.extend(("--hidden-import", hidden))
    arguments.append(str(entrypoint))
    return arguments


def _run_pyinstaller(arguments: list[str]) -> None:
    import PyInstaller.__main__  # type: ignore[import-untyped]

    PyInstaller.__main__.run(arguments)


def build(
    *,
    project_root: Path = PROJECT_ROOT,
    dist_dir: Path = DIST_DIR,
    runner: BuildRunner = _run_pyinstaller,
) -> Path:
    arguments = build_arguments(project_root=project_root, dist_dir=dist_dir)
    print(f"Building {APP_NAME} from {ENTRYPOINT}", flush=True)
    runner(arguments)

    executable = _verified_output_path(project_root, dist_dir) / f"{APP_NAME}.exe"
    if not executable.is_file() or executable.stat().st_size <= 0:
        raise RuntimeError(f"PyInstaller did not produce a valid executable: {executable}")
    print(f"Built {executable} ({executable.stat().st_size:,} bytes)", flush=True)
    return executable


def main() -> None:
    build_arguments()  # validate before removing any prior output
    clean_outputs()
    print(f"Build complete: {build()}", flush=True)


if __name__ == "__main__":
    main()


__all__ = ["APP_NAME", "ENTRYPOINT", "build", "build_arguments", "clean_outputs", "main"]
