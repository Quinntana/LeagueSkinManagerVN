"""Console and PyInstaller entry point.

Imports absolutely rather than relatively: PyInstaller runs this file as a
script, where a relative import has no package to resolve against.
"""

from __future__ import annotations

from league_skin_manager.app import run


def main() -> None:
    raise SystemExit(run())


if __name__ == "__main__":
    main()
