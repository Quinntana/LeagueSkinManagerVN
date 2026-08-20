## MODIFIED Requirements

### Requirement: The write surface stays minimal

The application SHALL confine itself to the operations tabulated below, and SHALL skip every settings edit while LTK is running. A managed setting SHALL be applied at a moment when LTK's settings file exists and LTK is not running, and a skipped application SHALL be recorded rather than passed over in silence.

The application SHALL confine itself to the operations below, and SHALL skip
every settings and library edit while LTK is running.

| Operation | Target |
|---|---|
| read | `settings.json` → `modStoragePath` |
| write | `settings.json` → `enforceSkinhackScan` only |
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

#### Scenario: A freshly installed LTK has no settings file yet
- **WHEN** LTK has been installed by this application and has never run
- **THEN** applying a managed setting SHALL be deferred rather than attempted
- **AND** it SHALL be applied once LTK has written its settings file and is not
  running
- **BECAUSE** LTK creates `settings.json` on its first run, after which it
  contains LTK's defaults and not this application's managed values

#### Scenario: A skipped application is visible
- **WHEN** a managed setting cannot be applied, for any reason
- **THEN** the reason SHALL be logged
- **BECAUSE** the previous silent failure left `enforceSkinhackScan` enabled
  through a complete first run with no trace in the log
