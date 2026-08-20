# LeagueSkinManagerVN

A single portable Windows executable that lives in the system tray. It keeps a
set of League skin mods mirrored into LTK Manager, opens a cooldown board when
a match starts, and owns every file it creates.

Not affiliated with or endorsed by Riot Games.

## What it does

Four things:

1. Follows one commit of a skin repository on GitHub.
2. Downloads and verifies every base skin package.
3. Wipes LTK Manager's skin library and reseeds it from that set.
4. Opens a cooldown board while you are in a game.

Everything else it used to do is gone.

## The tray

The tray icon is the only permanent interface.

```text
  Open LTK Manager              <- left-click default
  Sync skins now
  ---------------------------
  Cooldown timers
  Get Porofessor
  ---------------------------
  Open app folder
  ---------------------------
  [ ] Cooldown timers with game
  Cooldown display  >  Opacity: 100% / 85% / 70% / 55% / 45% / 35% / 30%
                       Size:    70% / 85% / 100% / 125%
  [ ] Start with Windows
  ---------------------------
  Uninstall...
  Exit
```

There is no status row. The icon has two colours (idle and working), the
tooltip carries the counts, and failures arrive as notifications rather than as
a mode the menu has to render. That is deliberate: a computed status line
raises the question of which condition wins when two of them hold, and answering
it grows priority rules that nobody wants to maintain.

`Open LTK Manager` reads `Install LTK Manager` when LTK is absent.
`Cooldown timers` is greyed outside a match, and also while the board is
already on screen — there is one board per game. When LTK exists but this
application did not install it, skin syncing is disabled and the tooltip says
so, while the cooldown board stays fully usable.

## First run on a new machine

Zero manual steps before skins are in place:

```text
run the .exe
  |- LTK missing -> download, verify, install (passive; it does not auto-launch)
  |- create archives/ and seed every package
  \- open LTK once so you can see the result
```

LTK finds your League installation itself. If it is already installed, the app
skips straight to syncing.

## How syncing works

```text
head = GitHub: main's HEAD commit          <- one request
if head == last synced commit: stop        <- the usual case, and it is free
tree = GitHub: one recursive tree          <- path, size, and blob SHA per file
download whatever the cache lacks          <- verified as the bytes arrive
delete archives/ and mods/
copy every cached package into archives/
record the commit                          <- only now
```

Recording the commit last is what makes an interrupted sync self-healing. There
is no journal and no rollback, because repeating the whole operation converges
from any starting state: it wipes before it seeds.

The cache is content-addressed — every file is named by its Git blob SHA, so
the filename *is* the content's identity. There is no index and no ledger, so
there is nothing that can drift out of agreement with what is on disk.

Only `skins/<champion>/<name>.fantome` is selected. Matching on exact depth is
more durable than trying to recognise chroma naming, and the repository is
2.4 GB against 53 MB of base skins, so the whole-repository archive is never
fetched.

## What it does to LTK Manager

Established by experiment against LTK Manager v1.13.0, not assumed:

| Operation | Target |
|---|---|
| read | `settings.json` → `modStoragePath` |
| write | `settings.json` → `enforceSkinhackScan` only, and only in a file LTK already wrote |
| write | `archives/*.fantome`, under any filename |
| delete | contents of `archives/` and `mods/` |
| never | `library.json`, `watcherEnabled` or any other setting, `wad-reports.json`, `profiles/`, LTK's own files |

The `settings.json` edit is skipped entirely while LTK is running, and the
file is never authored from scratch: LTK requires `firstRunComplete` and
discards a settings file lacking it.

LTK reconciles its library from disk on **every** startup, regardless of any
setting. A stale `library.json` referencing packages that no longer exist is
repaired automatically, and packages dropped into `archives/` are adopted and
renamed.

### Everything arrives switched on

LTK switches a package **on** the moment it adopts it, with or without the
file watcher, and this application does not interfere with that.

Be aware of what it means: 171 of 173 champions have more than one skin, and
Miss Fortune alone has 23, so after a sync every champion has a dozen skins
enabled at once. Which one you actually see is whatever LTK's ordering picks,
and it can change when the source updates.

Managing that is LTK's job, not this application's. Turn off what you do not
want in LTK's own library view; those choices survive until the next sync,
which wipes and reseeds the library from scratch.

This was deliberate. Clearing the selection automatically is easy to write and
cannot tell LTK's auto-enable apart from skins *you* switched on, so it ends
up deleting your choices on every launch. Telling the two apart properly needs
a content-hash ledger mapping your selection across LTK's UUID renaming —
which is exactly the bookkeeping this rebuild removed, and too much machinery
for an annoyance.

Syncing does not require LTK to be closed. If it happens to be open, its library
will look empty until it is restarted, so the app says exactly that in a
notification.

LTK ships its own updater, so this application never manages LTK versions. It
verifies the first install and then gets out of the way.

`watcherEnabled` is deliberately left alone. LTK defaults it off, seeding is
proven not to need it, and forcing it on makes packages adopt — and switch
themselves on — mid-session while you are looking at the library. Turn it on
yourself if you want live pickup of files you drop in by hand.

### Installer verification

The installer is never executed until all of this passes:

- the URL is HTTPS on an allowed GitHub host, with no credentials, no unusual
  port, and a path matching the selected tag and asset
- the transfer matches GitHub's declared size and SHA-256
- the file carries a valid Authenticode signature from `Natoken LLC`
- a redirect to an untrusted host is rejected mid-download

Any failure deletes the file and raises. Nothing is downgraded to a warning.

## Cooldown board

A click-to-start timer board for enemy ultimates and summoner spells, drawn as
a borderless overlay over the game.

> **Set League to Borderless or Windowed.** The board cannot draw over
> exclusive fullscreen. Nothing can, short of hooking DirectX the way Overwolf
> does for Porofessor, and that is not something this application will do.

Riot's Live Client Data API supplies who the enemies are, their level, and their
summoner spells. Data Dragon supplies the durations and the icons. Neither
exposes enemy cast events, which is why a click starts the timer — but nothing
is typed by hand.

Left click only: idle → counting → cancel → counting again, as a fresh timer
rather than a resumed one. **Right click does nothing anywhere on the board** —
it is how you move, so a stray one costs nothing.

An ability that cannot be tracked — an ultimate the enemy has not learned yet,
or a charge-based spell like Smite — is crossed out rather than dimmed, so its
icon stays recognisable.

One row per enemy, ordered top, jungle, mid, bot, support. Rows are identified
by champion portrait rather than by name, and level is read but never drawn;
the scoreboard already shows it and this surface exists to be small.

A counting slot darkens its own icon and draws the seconds outlined over it, so
it stays readable however faint the board is. That matters because window
opacity is uniform in Tk — it cannot fade the background while leaving the
number solid, so the contrast has to come from inside the slot.

The board has three controls in its top-right corner — opacity, size, and close
— and a readout of the current two. It hides itself whenever League is not the
foreground window, and drags from anywhere else; where you leave it is
remembered.

Ultimates are **not** uniformly three ranks, so the level mapping is not a
guess. Jayce has one rank; Elise, Karma and Nidalee have four; ten champions
have charge, resource, toggle, or repeat-cast ultimates with no meaningful flat
cooldown; and Smite recharges on charges. All of those render as disabled with
a reason rather than showing a number that would be wrong. Against patch
16.16.1 that resolves 162 of 173 ultimates.

The board is off by default and opens with `League of Legends.exe` when
enabled. **Its lifetime is the match, not the window.** Closing it with the
corner control hides it — every timer keeps running, and re-opening it from the
tray brings the same board back with the countdowns where you left them.
Everything is released when the game exits.

There is one board per game. While it is on screen the tray's `Cooldown timers`
entry is greyed out; hiding the board offers it again.

Turning the automatic toggle on *during* a match does not open it — that takes
effect next match; use `Cooldown timers` to open it now.

It lives in its own package behind four functions, on its own thread with its
own Tk root. A Data Dragon outage or a panel crash cannot affect skins, and a
sync failure cannot affect the board.

## Ownership

One rule: **if running this application is why a file exists, this application
removes it.**

| Path | On uninstall |
|---|---|
| `%APPDATA%\LeagueSkinManagerVN\` | removed |
| `%APPDATA%\dev.leaguetoolkit.manager\` | removed, if we installed LTK |
| `%LOCALAPPDATA%\LTK Manager\` | removed via its own uninstaller, if we installed it |
| `HKCU\...\CurrentVersion\Run` | removed |
| Start Menu shortcut | removed |
| Overwolf / Porofessor | never touched |
| the `.exe` itself | left for you |

A running single-file executable cannot delete itself, and the usual workaround
is exactly the pattern antivirus heuristics flag, so the app clears everything
else and tells you to bin one file.

An LTK that was already installed when this application first ran is never
removed — and never modified either. That case is refused outright rather than
handled, which is what makes ownership unambiguous.

## Porofessor

The tray opens `porofessor.gg/download` and does nothing else. Porofessor is an
Overwolf extension with no standalone signed installer to verify, and silently
installing a game-overlay platform is the class of behaviour that trips
anti-cheat heuristics.

## Data layout

```text
%APPDATA%\LeagueSkinManagerVN\
  cache\packages\   content-addressed .fantome cache, named by Git blob SHA
  cache\ltk\        the verified LTK installer, and Data Dragon metadata
  logs\             rotating diagnostics
  settings.json     the complete persisted state, a few hundred bytes
```

## Architecture

Ports and Adapters (Cockburn, 2005) over a layered import rule, in a modular
monolith. Not invented here, and enforced rather than described:
`tests/test_architecture.py` parses the import graph and fails on any upward
import, any unclassified module, and any direct use of `requests`, `psutil`,
`ctypes`, or `winreg` outside an adapter.

```text
L0  config  atomic  hashing  logging_setup  windows
L1  settings  fantome
L2  github  cache  seed  ltk  porofessor  process_watch  uninstall
L3  sync
L4  tray  cooldown/
L5  app  __main__
```

`app.py` is the composition root and the only module that knows concrete types.
Everything else talks to Protocols and plain values.

Adding an external tool means one module and one entry in the tray's action
list; nothing existing moves. Changing where skins come from touches
`github.py` alone.

## Specifications

`openspec/` holds the behaviour specs in plain Markdown, following the
[OpenSpec](https://github.com/Fission-AI/OpenSpec) convention so any assistant
can read them without this repository's chat history:

```text
openspec/
  project.md                      context, stack, architecture, conventions
  AGENTS.md                       how to work on this codebase
  specs/skin-sync/spec.md         the pipeline and its guarantees
  specs/ltk-integration/spec.md   the measured LTK contract
  specs/cooldown-board/spec.md    roster, rank rules, isolation
  specs/tray-lifecycle/spec.md    tray, first run, ownership, platform
  changes/                        in-flight proposals; archive/ when shipped
```

The LTK spec is the important one. Every claim in it was established by
experiment, and four decisions reached by reasoning alone were disproved by
those experiments. Do not relax a requirement there without re-running the
corresponding test against a copy of the LTK data root.

## Development

Python 3.10 through 3.14.

```powershell
poetry install
poetry run ruff format --check src tests build.py
poetry run ruff check src tests build.py
poetry run mypy src/league_skin_manager
poetry run pytest
```

Build the single executable:

```powershell
poetry run python build.py
```

### Do not run the executable from a synced drive

Running the built `.exe` from a Google Drive / OneDrive path makes every TLS
handshake time out after ~34 seconds and fail, so the app cannot reach GitHub
at all. The same interpreter and certificate bundle work instantly from a local
path, and raw `ssl` handshakes succeed from either — it is the sync client's
filesystem filter interfering with the frozen executable's network stack, not a
certificate or proxy problem.

Build wherever you like; put the `.exe` somewhere local before running it.

## Notice

Custom skin tools, redistributed game assets, and game-file modification can
stop working after any patch and may carry account, license, or Terms-of-Service
risk. The configured skin mirror has no declared software or content license.
Review the upstream projects and Riot's current terms before running or
distributing a build.

LTK's bundled CSLOL-derived DLL has separate redistribution conditions. This
project therefore never vendors or mirrors LTK binaries: it retrieves the
installer from the official GitHub release, verifies it, and lets LTK's own
installer and updater manage the application.
