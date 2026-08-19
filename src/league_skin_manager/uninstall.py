"""Remove everything this application caused to exist.

One rule decides the scope: if running this application is why a file exists,
this application removes it.  That includes LTK's data root, because its
contents are packages we put there, and LTK itself when we were the one that
installed it.

A running single-file executable cannot delete itself, and the usual
workaround -- spawning a detached command that waits for the process to die
and then deletes the file -- is precisely the pattern antivirus heuristics
flag.  So the executable is left for the user to delete, and the action says
so rather than pretending otherwise.
"""

from __future__ import annotations

import logging
import shutil
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path

from . import ltk, windows
from .config import APP_NAME
from .hashing import is_real_directory

LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class UninstallReport:
    """What was removed, for the message shown afterwards."""

    app_data: bool = False
    ltk_data: bool = False
    ltk_app: bool = False
    startup: bool = False
    shortcut: bool = False
    failures: tuple[str, ...] = ()

    @property
    def succeeded(self) -> bool:
        return not self.failures

    def summary(self) -> str:
        removed = [
            label
            for label, done in (
                ("application data", self.app_data),
                ("LTK skin library", self.ltk_data),
                ("LTK Manager", self.ltk_app),
                ("startup entry", self.startup),
                ("Start Menu shortcut", self.shortcut),
            )
            if done
        ]
        if not removed:
            return "Nothing needed removing."
        return "Removed: " + ", ".join(removed) + "."


def uninstall(
    *,
    data_dir: Path,
    executable: Path,
    remove_ltk: bool,
    ltk_data_dir: Path | None = None,
    close_logging: bool = True,
) -> UninstallReport:
    """Remove this application's footprint.

    ``remove_ltk`` comes from the settings flag recorded when we installed LTK.
    An LTK that was already present when this application first ran is never
    removed, and is in fact never touched at all -- the application refuses to
    manage a library it did not create.
    """

    failures: list[str] = []

    if close_logging:
        # The log lives inside the directory about to be deleted, and Windows
        # refuses to remove a directory with an open handle in it.
        _shutdown_logging()

    startup = windows.set_startup_enabled(executable, False)
    shortcut = windows.remove_start_menu_shortcut()

    ltk_app = False
    if remove_ltk:
        ltk_app = _run_ltk_uninstaller()
        if not ltk_app:
            failures.append("LTK Manager's uninstaller could not be started")

    ltk_data = False
    if remove_ltk:
        ltk_data = ltk.remove_data(ltk_data_dir)
        if not ltk_data and is_real_directory(ltk_data_dir or ltk.default_data_dir()):
            failures.append("LTK's skin library could not be removed")

    app_data = _remove_tree(data_dir)
    if not app_data and is_real_directory(data_dir):
        failures.append(f"{APP_NAME}'s data folder could not be removed")

    report = UninstallReport(
        app_data=app_data,
        ltk_data=ltk_data,
        ltk_app=ltk_app,
        startup=startup,
        shortcut=shortcut,
        failures=tuple(failures),
    )
    LOGGER.info("Uninstall finished: %s", report.summary())
    return report


def _remove_tree(path: Path) -> bool:
    if not is_real_directory(path):
        return False
    try:
        shutil.rmtree(path)
    except OSError:
        LOGGER.warning("Could not remove %s", path, exc_info=True)
        return False
    return True


def _run_ltk_uninstaller() -> bool:
    """Start LTK's own uninstaller, which is the only supported way to remove it."""

    uninstaller = ltk.uninstaller()
    if uninstaller is None:
        LOGGER.info("LTK Manager's uninstaller was not found; nothing to run")
        return False
    return windows.launch_detached(uninstaller)


def _shutdown_logging() -> None:
    """Release the log file so the directory containing it can be deleted."""

    for logger in (logging.getLogger(), logging.getLogger("league_skin_manager")):
        for handler in list(logger.handlers):
            with suppress(Exception):
                handler.close()
            logger.removeHandler(handler)


__all__ = ["UninstallReport", "uninstall"]
