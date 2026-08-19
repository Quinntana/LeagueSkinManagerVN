# LeagueSkinManagerVN

A native Windows **system-tray** application that keeps an application-owned set of
League skin mods synchronized and keeps LTK Manager's skin library equal to that set.
It installs and verifies the official LTK Manager, and uses CSLOL Manager as the
fallback backend.

## The tray is the only interface

There is no application window and no skin browser. Every action lives in the tray
menu, which stays deliberately small:

```text
Ready - 1,927 skins - patch 16.14.1        (status, not clickable)
LTK: 1,927 skins - 0 enabled               (status, not clickable)
-------------------------------------------
Open LTK Manager                            <- left-click default
Sync skins now
Enemy cooldown timers...
-------------------------------------------
Folders   >   Skins in LTK | Skins in CSLOL | App data | Diagnostics log
Advanced  >   Rebuild LTK library now
              Remove all skins from LTK... | Uninstall LeagueSkinManagerVN...
-------------------------------------------
Start with Windows                      [x]
Exit
```

The second row reports the honest end-to-end state: how many skins LTK actually
holds, and **how many are switched on**. A skin present in LTK but not enabled does
nothing in game, so that count is shown rather than implied. When LTK has drifted from
the current set the row reads `LTK: 1,900 of 1,927 skins - 27 to rebuild`; when LTK is
absent it reads `LTK: not installed` and the first action becomes
**Install LTK Manager...**.

**Enemy cooldown timers...** opens the one optional window: a manual timer board that
reads nothing from the game or the network. Click a slot to start or restart it,
right-click to cancel. Each row has an editable name, an ultimate duration in seconds,
and two summoner-spell presets; transitions are appended to `cooldown-events.csv`.
It owns its own event loop, so closing or crashing it cannot disturb the tray.

`--no-sync` starts without the initial synchronization. `--background` is accepted for
compatibility with the Windows startup entry and does nothing.

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
8. A low-overhead native Windows process monitor starts the preferred skin manager
   backend once for each League Client process: the installed official LTK Manager
   when present, otherwise CSLOL Manager. If League starts during synchronization,
   launch is deferred until the transaction finishes. The tray's explicit launch
   action uses the same preference.
9. Synchronization is paused while CSLOL Manager or any helper executable inside its
   owned directory is running, avoiding live mod-directory mutation.
10. In a separate background task, the installed LTK version is compared against the
    latest official release. That comparison is what decides everything below, and it
    is deliberately cheap to repeat: the located installation is reused while LTK's
    executable is unchanged (an update, move, or removal forces a fresh lookup), and a
    successful release check is cached for six hours. An install that already satisfies
    a recent check therefore needs no network request at all. The cache is only ever
    used to confirm that outcome - whenever an installer might actually be downloaded,
    the release metadata is refetched, so verification never runs on cached data.
    When LTK is missing or outdated, its exact x64 NSIS installer is downloaded
    from `LeagueToolkit/ltk-manager`, checked against GitHub's release size and SHA-256,
    and required to have a valid Windows Authenticode signature from `Natoken LLC`.
    Verification failures are never downgraded to warnings. Downloading does not run the
    installer; only an explicit **Open / Install LTK Manager** click does.
    LeagueSkinManagerVN never writes LTK's `settings.json`, so its content-enforcement
    and safety settings are left exactly as you set them, and no flags intended to
    disable them are ever passed.
11. **LTK's skin library is managed, not shared.** Because this application installs
    and controls LTK, its library is treated as a reproducible mirror of the current
    skin set rather than as user-owned data. The baseline is: *LTK holds exactly one
    package per current skin, nothing else, and nothing enabled.*
12. Reconciliation to that baseline is declarative and idempotent. The desired set is
    derived from the managed manifest and the verified package cache; the actual set is
    read from LTK's storage. Anything present but not desired is removed, anything
    desired but absent is queued, and every profile's enabled selections are cleared.
    This works because LTK stores an imported package byte-for-byte under its own
    identifier, so a package's SHA-256 is a stable identity across import.
13. It is differential, not a wipe: a normal update touches only the difference, and a
    repeat pass is a no-op. Packages come **straight from the verified package cache** -
    the manifest records each skin's upstream byte count and Git blob SHA-1 and the
    cached `.fantome` is revalidated against both - so no CSLOL extraction is read,
    walked, or hashed, and a rebuild works even with an empty CSLOL folder.
14. A rebuild runs after every successful sync, and can be started by hand with
    **Rebuild LTK library now**. It requires both CSLOL and LTK to be closed, so a pass
    that finds either running defers without a popup and retries after the League client
    exits or after the next sync. An unchanged automatic pass stays silent.
15. **What this means for you:** skins you import into LTK yourself, and which skins you
    switched on, are *not* preserved - the next rebuild resets both. LTK Manager itself,
    its `settings.json`, and its logs are never touched. If you want a skin active,
    enable it in LTK after a rebuild.
## How LTK import works

This behaviour was established empirically against LTK Manager v1.13.0 by importing a
package and removing it again, and it is what the automatic port relies on. Re-verify
it before trusting these guarantees against a much newer LTK release.

- LTK runs a **library watcher** over its `archives` and `mods` directories and also
  reconciles once at startup. A `.fantome` file placed in `archives` by another
  application is adopted: LTK logs `Discovered and registered archive: <name> as
  <mod-id>`. Dropping a complete package into `archives` is therefore a supported
  ingestion path, not a hopeful guess.
- On import LTK copies the package to `archives/<mod-id>.fantome`, extracts derived
  metadata to `mods/<mod-id>/mod.config.json`, appends `{id, installedAt, format}` to
  `library.json`, and registers the identifier in its folder structure. The originally
  dropped file is then deleted.
- **The stored copy is byte-for-byte identical to the file that was handed over.** A
  package's SHA-256 is therefore stable across import, which is what lets
  LeagueSkinManagerVN recognise its own previously queued content inside LTK using
  nothing but its existing ledger and archive-hash index. Filenames and LTK's logs are
  *not* usable for this: the file is renamed on import, and LTK deletes logs older than
  the current session on startup.
- Removing one mod means removing exactly that set of artifacts: the archive, the
  metadata directory, the `library.json` entry, every profile `enabledMods`,
  `modOrder`, and `layerStates` reference, the folder `modIds` reference, and the
  `wad-reports.json` entry. LTK accepts such an externally performed removal silently
  on its next start, with no orphan or repair warnings.
- LTK persists its configuration **only when something actually changes**. A complete
  open-and-close session with no user action left `settings.json`, `library.json`,
  `wad-reports.json`, and `.window-state.json` byte-identical, with unchanged
  modification times. LTK therefore does not hold these files in memory and flush them
  on exit, so an external edit made while LTK is closed cannot be silently reverted by
  stale in-process state. `library.json` is also written with the same two-space
  indentation this application uses. Editing LTK-owned files nonetheless remains
  gated on LTK being stopped, because the watcher reconciles live changes.

Because a superseded package can be located exactly, VN removes only its own outdated
skins and leaves every other mod in the library untouched. The all-or-nothing **Remove
all LTK skins** action remains available as a separate, explicitly confirmed reset.

## Tray controls

- The two status rows are not clickable. The first shows sync state, skin count, and
  patch (or the error detail when something failed); the second shows LTK's library
  count and how many skins are enabled, any drift from the current set, or live rebuild
  progress. Whether this is the installed or portable build is in the hover tooltip.
- `Open LTK Manager` (left-click default): opens LTK, or verifies and starts its
  official installer when LTK is absent, in which case the row reads
  **Install LTK Manager...**.
- `Sync skins now`: starts one manual update; disabled while a sync or shutdown is
  active. A successful sync is followed by the quiet automatic rebuild.
- `Enemy cooldown timers...`: opens the manual timer window.
- `Folders`: opens LTK's skin storage, CSLOL's `installed` folder, this app's data
  folder, or the rotating diagnostics log.
- `Rebuild LTK library now`: reconciles LTK to the baseline on demand. Requires CSLOL
  and LTK to be closed; otherwise it defers and retries later.
- `Remove all skins from LTK...`: a default-No destructive confirmation. With LTK and
  its patcher closed, it removes every skin package, extracted metadata, WAD report, and
  generated profile overlay from LTK's configured storage - including skins you added
  yourself. LTK itself, its settings, and its logs are preserved, and the next rebuild
  restores the current skin set.
- `Start with Windows`: explicit per-user HKCU toggle; disabled by default. A portable
  copy cannot take startup ownership from the installed copy.
- `Uninstall LeagueSkinManagerVN...`: stops bounded background work, starts the exact
  installed uninstaller after this process exits, and is disabled for a portable copy.
  It uses the same cleanup path as Windows Apps & Features.
- `Exit`: requests cancellation. If the bounded wait expires, the tray and
  single-instance locks remain active until background work has stopped safely.

## Data layout

All mutable data is under `%APPDATA%\LeagueSkinManagerVN`:

- `cslol-manager/`: manager files, user profiles, and installed mods.
- `cache/packages/`: content-addressed verified `.fantome` cache.
- `cache/ltk/`: the current verified official LTK NSIS installer; old exact-version
  installer files are pruned safely. `release-check.json` records the last successful
  release check so a current install can skip the network on later launches.
- `managed_skins.json`: app-owned install manifest and transaction identity.
- `ltk_archive_index.json` and `ltk_package_index.json`: file-identity/digest caches
  so repeat rebuilds stat unchanged packages instead of rehashing them.
- `migration-reports/`: timestamped rebuild results and per-skin failures.
- `cooldown-events.csv`: append-only log of manual cooldown-panel transitions.
- `logs/LeagueSkinManagerVN.log`: rotating diagnostics.

LTK's application files remain external and untouched. Its normal data root is
`%APPDATA%\dev.leaguetoolkit.manager`, or the absolute `modStoragePath` selected in
LTK's own settings.

**This application manages the skin library under that root.** A rebuild writes complete
packages into `archives`, deletes packages that are not part of the current skin set -
including any you imported yourself - removes the matching `mods/<id>` metadata and
`wad-reports.json` entry, and clears every profile's enabled selections in
`library.json`. LTK's `settings.json`, its logs, and the LTK application itself are
never modified.

The per-user setup installs program files under
`%LOCALAPPDATA%\Programs\LeagueSkinManagerVN` and registers **League Skin Manager VN**
in Windows Apps & Features. Its uninstall command points to the installed
`LeagueSkinManagerVNUninstall.exe`.

Setup and uninstall confirmations default to No. Installed and portable copies share
the same global application mutex, so setup pauses instead of replacing files while
either copy is active. The tray identifies its runtime mode; only the exact installed
executable may own Windows startup or launch the installed uninstaller.

The uninstaller requests no elevation, participates in the application mutex,
refuses to run while the service, manager, or an owned helper process is active,
and validates the exact AppData and install targets before deletion. A full uninstall
removes the startup value, Apps & Features entry, downloaded skins, cache, logs,
CSLOL profiles, other application data, and installed program files. Portable
executables outside the fixed per-user install directory are never deleted. The
separately installed official LTK Manager and its application data are also never
removed by the LeagueSkinManagerVN uninstaller; only this app's cached installer,
digest indexes, and reports are removed with its AppData. Removing skins from LTK is
always a separate explicit action and is never part of app uninstall.

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
