# LeagueSkinManagerVN

A Windows tray application that keeps a CSLOL Manager installation and an
application-owned set of League skin mods synchronized without overwriting the
user's own mods or profiles.

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
8. A cooperative process monitor starts CSLOL Manager once for each League Client
   process. If League starts during synchronization, launch is deferred until the
   transaction finishes.

If the network or source is unavailable, a valid existing manager/mod installation
remains usable and the tray reports `Ready offline`. A first run with nothing cached
reports an actionable error instead of destroying or partially installing content.

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

The uninstaller is per-user, requests no elevation, refuses to run while the service
or manager is active, and validates the exact AppData target before deletion.

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

Build both one-file Windows executables:

```powershell
.\.venv\Scripts\python build.py
```

`build.py` validates both package entrypoints before cleaning old outputs and limits
all cleanup to the repository's known build directories.

For a packaged startup check that does not contact the skin source, run
`LeagueSkinManagerVN.exe --no-sync`.

## Source boundary

The source/catalog API is intentionally isolated. A future preferred backend can build
fantomes locally from the user's installed League WADs (for example, the approach used
by [league-skin-fantome-builder](https://github.com/bettie9/league-skin-fantome-builder))
without changing transaction, lifecycle, or tray code. Vendored tools and their
licenses must be audited before bundling such a backend.
