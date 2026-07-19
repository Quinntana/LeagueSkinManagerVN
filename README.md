# LeagueSkinManagerVN

A native Windows desktop and tray application that keeps a CSLOL Manager
installation and an application-owned set of League skin mods synchronized,
while also providing a verified official LTK Manager companion and a safe
CSLOL-to-LTK migration path. User mods, profiles, and CSLOL originals are not
overwritten.

## Desktop library

Normal launches open a desktop library backed by the validated offline manifest:

- Instant accent- and punctuation-tolerant search across champion and skin names.
- Champion filtering and sortable Champion, Skin, and Package size columns.
- Installed skin, champion, patch, and source-commit statistics.
- Selected-mod details plus shortcuts for the installed folder, AppData, and logs.
- `Sync VN skins`, CSLOL and LTK launch controls, a guided LTK migration tool,
  background startup, refresh, and clean exit controls.

Closing the window keeps the service available in the tray. Windows startup uses
`--background`, so login remains unobtrusive. The tray's default action reopens the
desktop window.

## Important notice

This project is not affiliated with or endorsed by Riot Games. Custom skin tools,
redistributed game assets, and game-file modification can stop working after any
patch and may carry account, license, or Terms-of-Service risk. The configured skin
mirror has no declared software/content license. Review the upstream projects and
Riot's current terms before running or distributing a build.

LTK's bundled CSLOL-derived DLL has separate redistribution conditions in its
[`LICENSE-CSLOL.md`](https://github.com/LeagueToolkit/ltk-manager/blob/main/LICENSE-CSLOL.md).
LeagueSkinManagerVN therefore does not vendor or mirror LTK binaries: it retrieves the
installer directly from the official GitHub release, verifies it, and lets LTK's own
installer and updater manage the external application.

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
10. In a separate background task, the latest official LTK Manager release metadata is
    checked. When LTK is missing or outdated, its exact x64 NSIS installer is downloaded
    from `LeagueToolkit/ltk-manager`, checked against GitHub's release size and SHA-256,
    and required to have a valid Windows Authenticode signature from `Natoken LLC`.
    Verification failures are never downgraded to warnings. Downloading does not run the
    installer; an explicit **Open / install LTK** or confirmed migration action does.
11. The port chooser accepts either a CSLOL Manager root or its direct `installed`
    folder. Porting is **manual only**: startup, automatic LTK release preparation, and
    `Sync now` never scan or copy CSLOL mods into LTK. Files are considered only after the
    user presses **Port CSLOL skins to LTK now...** and confirms the selected folder. Both
    CSLOL and LTK (including the legacy LeagueSkinManagerLTK app/patcher) must be closed.
    Valid mods are queued only through LTK's supported `archives` inbox; normal porting
    never edits LTK's library, profiles, or indexes directly.
12. Migration reuses the verified package cache when an extracted mod still matches its
    manifest fingerprint and deterministically repackages changed or user-owned mods.
    WAD-heavy packages use ZIP's no-recompression mode, avoiding expensive level-9
    deflate for data that is already compressed. Packages are SHA-256 deduplicated across
    both the current inbox and bounded VN-owned content/archive indexes. Unchanged repeat
    ports skip packaging and full LTK archive scans even after LTK consumes its inbox.
    Writes still use fsynced temporary files, atomic renames, cancellation, resource
    limits, and an audit report. Legacy migration history is read compatibly and upgraded
    without requeueing unchanged packages.

If the network or source is unavailable, a valid existing manager/mod installation
remains usable and the tray reports `Ready offline`. A first run with nothing cached
reports an actionable error instead of destroying or partially installing content.

CSLOL Manager's maintainers now describe it as being in maintenance/deprecation mode
while development moves to LTK Manager. CSLOL remains the automatic backend started for
League in this release, preserving the existing workflow. LTK is an independent,
explicitly launched companion: no LTK DLL or injection core is bundled, copied, or
invoked directly by LeagueSkinManagerVN.

## Tray controls

- `Status`: current lifecycle/synchronization state.
- `Sync VN skins now`: starts one manual update; a second concurrent sync is rejected safely.
- `CSLOL Manager` submenu: opens CSLOL Manager or its direct `installed` skins folder.
- `LTK Manager` submenu: opens/installs LTK, opens the detected application folder, opens
  the configured skin-storage folder, starts the explicit port tool, or removes all skins.
- `Port CSLOL skins to LTK now...`: the only action that can begin a port. It opens the
  desktop folder chooser on Tk's UI thread, confirms the non-destructive handoff, shows
  progress/cancellation, and opens LTK after packages and migration history are durable.
- `Remove all LTK skins...`: a default-No destructive confirmation. With LTK and its
  patcher closed, it removes every archive (including non-VN skins), extracted metadata,
  WAD report, and generated profile overlay from LTK's configured storage. LTK itself,
  settings, logs, and named profile definitions are preserved. The VN port history is
  reset so a later explicit port can intentionally add skins again.
- `Reset port history...` (desktop): an explicit confirmation-only recovery action
  for intentionally requeuing content removed from LTK. It deletes no CSLOL or LTK data.
- `Start with Windows`: explicit per-user HKCU toggle; disabled by default.
- `Uninstall LeagueSkinManagerVN...`: stops bounded background work, starts the exact
  installed uninstaller after this process exits, and uses the same cleanup path as
  Windows Apps & Features.
- `Exit`: requests cancellation. If the bounded wait expires, the tray and
  single-instance locks remain active until background work has stopped safely.

## Data layout

All mutable data is under `%APPDATA%\LeagueSkinManagerVN`:

- `cslol-manager/`: manager files, user profiles, and installed mods.
- `cache/packages/`: content-addressed verified `.fantome` cache.
- `cache/ltk/`: the current verified official LTK NSIS installer; old exact-version
  installer files are pruned safely.
- `managed_skins.json`: app-owned install manifest and transaction identity.
- `ltk_migration_state.json`: bounded SHA-256 history for cross-run migration dedupe.
- `ltk_archive_index.json`: VN-owned file-identity/hash cache that avoids repeatedly
  hashing an unchanged large LTK archive library.
- `migration-reports/`: timestamped migration results and per-mod failures.
- `logs/LeagueSkinManagerVN.log`: rotating diagnostics.

LTK itself remains external. Its normal data root is
`%APPDATA%\dev.leaguetoolkit.manager`, or the absolute `modStoragePath` selected in LTK's
own settings. LeagueSkinManagerVN writes only complete packages to that root's
`archives` directory after port confirmation. Only the separate, explicitly confirmed
**Remove all LTK skins** action clears LTK-owned skin artifacts.

The per-user setup installs program files under
`%LOCALAPPDATA%\Programs\LeagueSkinManagerVN` and registers **League Skin Manager VN**
in Windows Apps & Features. Its uninstall command points to the installed
`LeagueSkinManagerVNUninstall.exe`.

The uninstaller requests no elevation, participates in the application mutex,
refuses to run while the service, manager, or an owned helper process is active,
and validates the exact AppData and install targets before deletion. A full uninstall
removes the startup value, Apps & Features entry, downloaded skins, cache, logs,
CSLOL profiles, other application data, and installed program files. Portable
executables outside the fixed per-user install directory are never deleted. The
separately installed official LTK Manager and its application data are also never
removed by the LeagueSkinManagerVN uninstaller; only this app's cached installer,
migration ledger, archive index, and reports are removed with its AppData. Removing LTK
skins is always a separate explicit action and is never part of app uninstall.

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
