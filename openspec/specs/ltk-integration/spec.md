# LTK integration

The riskiest part of the system, because LTK is someone else's application
that updates itself. Everything below was established by experiment against
LTK Manager v1.13.0 and re-checked on v1.13.3 — not inferred from its source.

Four decisions reached by reasoning alone were **disproved** by those
experiments. Do not "simplify" any requirement here without re-running the
corresponding test against a copy of the LTK data root.

## Requirement: The write surface stays minimal

The application SHALL confine itself to the operations below, and SHALL skip
every settings and library edit while LTK is running.

| Operation | Target |
|---|---|
| read | `settings.json` → `modStoragePath` |
| write | `settings.json` → `enforceSkinhackScan` only |
| write | `library.json` → each profile's `enabledMods` and `layerStates` |
| write | `archives/*.fantome`, under any filename |
| delete | contents of `archives/` and `mods/` |

#### Scenario: Files are dropped under arbitrary names
- **WHEN** a `.fantome` is written into `archives/` with any filename
- **THEN** LTK adopts it, copies it to `archives/<uuid>.fantome`, extracts
  metadata to `mods/<uuid>/`, registers it, and deletes the dropped file

#### Scenario: A stale library is repaired without help
- **WHEN** `library.json` references mods whose files are gone
- **THEN** LTK logs `Removing orphaned mod entry … (files missing from disk)`
  and reconciles on its next start
- **AND** the application SHALL NOT attempt that repair itself

#### Scenario: The library is never authored from scratch
- **WHEN** `settings.json` does not exist, or lacks `firstRunComplete`
- **THEN** the application SHALL make no edit
- **BECAUSE** `firstRunComplete` is a required field in LTK's `Settings`
  struct; a file lacking it fails to parse and LTK silently restores its own
  defaults, discarding the write

## Requirement: Reconciliation is unconditional

LTK reconciles its library from disk on **every** startup regardless of
`watcherEnabled`. The watcher governs only live pickup while LTK is running.

#### Scenario: Seeding works with the watcher disabled
- **WHEN** `watcherEnabled` is `false` and packages are placed in `archives/`
- **THEN** LTK adopts all of them at its next startup

#### Scenario: The watcher setting is left alone
- **WHEN** the application applies settings
- **THEN** it SHALL NOT write `watcherEnabled`
- **BECAUSE** LTK defaults it off, seeding does not need it, and forcing it on
  makes packages adopt and self-enable mid-session while the user is looking
  at the library

## Requirement: Nothing is enabled by default

LTK switches a package **on** the moment it adopts it, with or without the
watcher. The library holds every skin in the source: 171 of 173 champions have
more than one, and Miss Fortune alone has 23. Leaving them all enabled makes a
dozen skins compete per champion, with the winner changing silently whenever
the source updates.

#### Scenario: The baseline is restored after a sync
- **WHEN** the application starts and LTK is not running
- **THEN** it SHALL empty every profile's `enabledMods` and `layerStates`
- **AND** it SHALL leave every mod in the library

#### Scenario: Skins are switched off, never removed
- **WHEN** enabled mods are cleared
- **THEN** `mods`, `archives/`, and every other `library.json` field are
  unchanged

## Requirement: Syncing does not require LTK to be closed

#### Scenario: Mutating storage while LTK runs
- **WHEN** `archives/` and `mods/` are emptied and reseeded while LTK is open
- **THEN** no file is locked and no error occurs
- **BUT** the new packages are **not** adopted until LTK restarts
- **AND** LTK's library reads as empty until then
- **SO** the application SHALL notify: *"Skins updated — restart LTK Manager
  to load them."*

## Requirement: The first install is verified, and only the first

LTK ships Tauri's own updater with a hardcoded endpoint and minisign key. The
application SHALL NOT compare versions, cache release metadata, or re-install.

#### Scenario: An installer is executed
- **WHEN** LTK is absent and an install is requested
- **THEN** the download URL SHALL be HTTPS on an allowed GitHub host, with no
  credentials, no unusual port, and a path matching the selected tag and asset
- **AND** the transfer SHALL match GitHub's declared size and SHA-256
- **AND** the file SHALL carry a valid Authenticode signature from
  `Natoken LLC`, checked via the fixed system PowerShell
- **AND** any failure SHALL delete the file and raise, never warn
- **AND** the installer SHALL run with `/P` alone — `/R` would start LTK
  before seeding

#### Scenario: A redirect leaves the allowed hosts
- **WHEN** the response's final URL is not an allowed GitHub host
- **THEN** the download SHALL be rejected mid-transfer

## Requirement: Only an LTK we installed is ever touched

#### Scenario: LTK was already present
- **WHEN** LTK is installed and `ltk_installed_by_app` is false
- **THEN** skin syncing SHALL be disabled with an explanatory tooltip
- **AND** LTK's library SHALL NOT be modified
- **AND** the cooldown board SHALL remain fully usable

#### Scenario: Ownership is recorded at install time
- **WHEN** the application installs LTK
- **THEN** it SHALL record `ltk_installed_by_app` immediately
- **BECAUSE** the fact cannot be derived later, and it decides whether
  uninstall removes LTK
