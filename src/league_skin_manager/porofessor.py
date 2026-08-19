"""Porofessor: open its download page, and nothing else.

Porofessor is an Overwolf extension, not a standalone application.  Installing
it means installing Overwolf -- a background service, a browser runtime, and
game-overlay hooks -- and there is no signed standalone installer to verify the
way LTK's is verified.  Silently installing a game-overlay platform is also
exactly the class of behaviour that trips anti-cheat and antivirus heuristics.

So this application does not manage Porofessor in any sense: no detection, no
version check, no launch, no uninstall.  One tray entry opens the download page
in the default browser.  There is nothing here that can break.
"""

from __future__ import annotations

from .config import POROFESSOR_DOWNLOAD_URL
from .windows import open_url


def open_download_page() -> bool:
    """Open Porofessor's download page in the default browser."""

    return open_url(POROFESSOR_DOWNLOAD_URL)


__all__ = ["open_download_page"]
