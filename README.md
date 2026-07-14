# LeagueSkinManagerVN

A native Windows desktop and tray application that keeps a CSLOL Manager
installation and an application-owned set of League skin mods synchronized
without overwriting the user's own mods or profiles.

## Desktop library

Normal launches open a desktop library backed by the validated offline manifest:

- Instant accent- and punctuation-tolerant search across champion and skin names.
- Champion filtering and sortable Champion, Skin, and Package size columns.
- Installed skin, champion, patch, and source-commit statistics.
- Selected-mod details plus shortcuts for the installed folder, AppData, and logs.
- `Sync now`, `Start CSLOL Manager`, background startup, refresh, and clean exit controls.

Closing the window keeps the service available in the tray. Windows startup uses
`--background`, so login remains unobtrusive. The tray's default action reopens the
desktop window.

## Important notice

This project is not affiliated with or endorsed by Riot Games. Custom skin tools,
redistributed game assets, and game-file modification can stop working after any
patch and may carry account, license, or Terms-of-Service risk. The configured skin
mirror has no declared software/content license. Review the upstream projects and
Riot's current terms before running or distributing a build.

## Current workflow

1. The tray appears immediately. Network checks and updates never block the UI.
2. The official [LeagueToolkit/cslol-manager](https://github.com/LeagueToolkit/cslol-manager)
   release is checked in a background worker. Because its Windows package is unsigned,
   the app executes only a release whose exact tag, filename, byte count, and SHA-256
   are pinned in this build. A future unreviewed release fails closed with manual-update
   guidance. Trusted updates use a crash-recoverable transaction; `installed` and
   `profiles` are preserved.
3. The skin catalog is read from [bettie9/LeagueSkins](https://github.com/bettie9/LeagueSkins)
   at one pinned Git commit, replacing the archived/frozen
   [DarkSeal catalog](https://github.com/darkseal-org/lol-skins). Only direct
   `skins/<champion>/<skin>.fantome` files are selected, excluding nested chromas and
   avoiding the multi-gigabyte repository ZIP.
4. Downloads are retrying, cancellable, written through temporary files, and verified
   against the Git tree's byte count and Git-blob SHA-1.
5. Every `.fantome` is validated for traversal, symlinks, duplicate Windows paths,
   ZIP bombs, CRC errors, `META/info.json`, and WAD content before extraction.
6. The complete desired set is staged first. A journaled transaction replaces only
   directories prefixed and recorded as owned by LeagueSkinManagerVN. Unknown user
   mods and profiles are never deleted.
7. Reused managed mods are content-hashed again, including every file and path. Local
   corruption, same-size tampering, symlinks, or junctions force a rebuild from the
   verified package cache. Per-file, aggregate, and free-space limits fail before a
   source can exhaust the machine during normal staging.
8. A low-overhead native Windows process monitor starts CSLOL Manager once for each
   League Client process. If League starts during synchronization, launch is deferred
   until the transaction finishes.
9. Synchronization is paused while CSLOL Manager or any helper executable inside its
   owned directory is running, avoiding live mod-directory mutation.

If the network or source is unavailable, a valid existing manager/mod installation
remains usable and the tray reports `Ready offline`. A first run with nothing cached
reports an actionable error instead of destroying or partially installing content.

CSLOL Manager's maintainers now describe it as being in maintenance/deprecation mode
while development moves to LTK Manager. The current audited CSLOL release remains the
supported backend here; manager update and launch code is isolated so a future LTK
adapter does not require another skin-sync rewrite.

## Tray controls

- `Status`: current lifecycle/synchronization state.
- `Sync now`: starts one manual update; a second concurrent sync is rejected safely.
- `Start manager`: starts the installed CSLOL Manager, or queues the launch behind a sync.
- `Start with Windows`: explicit per-user HKCU toggle; disabled by default.
- `Exit`: requests cancellation. If the bounded wait expires, the tray and
  single-instance locks remain active until background work has stopped safely.

## Data layout

All mutable data is under `%APPDATA%\LeagueSkinManagerVN`:

- `cslol-manager/`: manager files, user profiles, and installed mods.
- `cache/packages/`: content-addressed verified `.fantome` cache.
- `managed_skins.json`: app-owned install manifest and transaction identity.
- `logs/LeagueSkinManagerVN.log`: rotating diagnostics.

The per-user setup installs program files under
`%LOCALAPPDATA%\Programs\LeagueSkinManagerVN` and registers **League Skin Manager VN**
in Windows Apps & Features. Its uninstall command points to the installed
`LeagueSkinManagerVNUninstall.exe`.

The uninstaller requests no elevation, participates in the application mutex,
refuses to run while the service, manager, or an owned helper process is active,
and validates the exact AppData and install targets before deletion. A full uninstall
removes the startup value, Apps & Features entry, downloaded skins, cache, logs,
CSLOL profiles, other application data, and installed program files. Portable
executables outside the fixed per-user install directory are never deleted.

## Development

Python 3.10 through 3.14 is supported. With Poetry:

```powershell
poetry install
poetry run ruff format --check src tests build.py
poetry run ruff check src tests build.py
poetry run mypy src/league_skin_manager
poetry run pytest
```

Or with a normal virtual environment:

```powershell
python -m venv .venv
.\.venv\Scripts\python -m pip install -e . pytest pytest-cov ruff mypy types-requests pyinstaller
.\.venv\Scripts\python -m pytest
```

Build the main app, uninstaller, and self-contained per-user setup executable:

```powershell
.\.venv\Scripts\python build.py
```

`build.py` validates all package entrypoints before cleaning old outputs and limits
all cleanup to the repository's known build directories. Run
`dist\LeagueSkinManagerVNSetup.exe` to install or update the per-user copy and create
the Windows Apps & Features entry.

For a packaged startup check that does not contact the skin source, run
`LeagueSkinManagerVN.exe --no-sync`.

## Source boundary

The source/catalog API is intentionally isolated. A future preferred backend can build
fantomes locally from the user's installed League WADs (for example, the approach used
by [league-skin-fantome-builder](https://github.com/bettie9/league-skin-fantome-builder))
without changing transaction, lifecycle, or tray code. Vendored tools and their
licenses must be audited before bundling such a backend.
