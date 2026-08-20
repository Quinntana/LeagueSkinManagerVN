# Tray, lifecycle, and ownership

## Purpose

Define the application's only permanent interface, what happens on a first
run, and what it owns. The ownership rule is the one that decides every other
question: if running this application is why a file exists, it removes it.

## Requirements

### Requirement: The tray is not a state machine

The tray SHALL convey state through a two-colour icon and a tooltip, and SHALL surface failures as notifications rather than as persistent modes. The menu SHALL contain only actions.

The previous design put a computed status line at the top of the menu, which
raised the question of which condition wins when two hold, and grew priority
rules to answer it.

#### Scenario: Conveying state
- **THEN** the icon SHALL have exactly two colours, idle and working
- **AND** those colours SHALL differ in **lightness** as well as hue, so they
  remain distinguishable in greyscale and for colour-vision deficiencies
- **AND** detail SHALL live in the tooltip
- **AND** failures SHALL be notifications, not persistent modes
- **AND** the menu SHALL contain only actions

#### Scenario: Actions that change
- **WHEN** LTK is absent **THEN** the first item reads *Install LTK Manager*
- **WHEN** a sync is running **THEN** sync and uninstall are disabled
- **WHEN** no match is active **THEN** cooldown timers is disabled
- **WHEN** syncing is blocked **THEN** the cooldown board stays usable

### Requirement: First launch needs no manual steps

A first run SHALL install LTK if absent, seed the skin set, and open LTK once, without prompting the user at any point.

#### Scenario: A fresh machine
- **WHEN** the application is run for the first time
- **THEN** the tray SHALL appear immediately, with nothing blocking it
- **AND** LTK SHALL be installed if absent, verified, without auto-launching
- **AND** the skin set SHALL be seeded
- **AND** LTK SHALL then be opened **once**, so the result is visible
- **AND** no prompt SHALL be shown at any point

### Requirement: The executable carries no state

All application data SHALL live under %APPDATA% and SHALL NOT depend on the executable's location. A moved executable SHALL have its shortcut and any existing startup entry re-pointed.

#### Scenario: Where data lives
- **THEN** all data SHALL live under `%APPDATA%\LeagueSkinManagerVN\`
- **AND** SHALL NOT depend on the executable's location

#### Scenario: The executable is moved
- **THEN** the Start Menu shortcut SHALL be rewritten on the next launch
- **AND** an existing startup entry SHALL be re-pointed at the new path
- **BUT** an absent startup entry SHALL stay absent, because that means the
  user never asked for it

### Requirement: Everything caused is removed

Uninstall SHALL remove every file whose existence is due to running this application, including LTK and its data when this application installed it, and SHALL shut down logging first.

If running this application is why a file exists, this application removes it.

#### Scenario: Uninstalling
- **THEN** `%APPDATA%\LeagueSkinManagerVN\`, the startup entry, and the Start
  Menu shortcut SHALL be removed
- **AND** LTK's data root and LTK itself SHALL be removed **only if** this
  application installed it
- **AND** the executable SHALL be left for the user, because a running
  single-file process cannot delete itself and the usual workaround is what
  antivirus heuristics flag
- **AND** logging SHALL be shut down first, or Windows refuses to delete the
  directory holding the open log

#### Scenario: Uninstalling mid-operation
- **WHEN** a sync is running
- **THEN** uninstall SHALL refuse and say so

### Requirement: Porofessor is not managed

The application SHALL open Porofessor's download page and SHALL NOT detect, install, launch, or remove it.

#### Scenario: The tray entry
- **THEN** it SHALL open `porofessor.gg/download` and do nothing else
- **BECAUSE** Porofessor is an Overwolf extension with no standalone signed
  installer to verify, and silently installing a game-overlay platform is the
  class of behaviour that trips anti-cheat heuristics

### Requirement: Platform access is confined

Registry access SHALL be limited to one HKCU value, PowerShell SHALL be resolved from the system directory, paths SHALL be passed through the environment rather than interpolated into scripts, and the single-instance mutex SHALL use the Local namespace.

#### Scenario: Registry
- **THEN** only `HKCU\…\CurrentVersion\Run` SHALL be touched, one value named
  after the application, opened without delete-key rights

#### Scenario: PowerShell
- **THEN** the fixed system copy SHALL be resolved via `GetWindowsDirectoryW`,
  never through `PATH`
- **AND** paths SHALL be passed through the environment, never interpolated
  into a script body
- **BECAUSE** a single quote in a path — `C:\Users\Bob's PC` is an ordinary
  folder name — would otherwise close the string and execute the remainder

#### Scenario: Single instance
- **THEN** the mutex SHALL use the `Local\` namespace, not `Global\`, so a
  second logged-in user can run their own copy

### Requirement: Known operational constraint

The built executable SHALL be run from local storage; on a synced drive every TLS handshake fails.

#### Scenario: Running from a synced drive
- **WHEN** the built executable is run from a Google Drive or OneDrive path
- **THEN** every TLS handshake times out after ~34 seconds and fails
- **AND** the same executable works instantly from a local path
- **SO** build anywhere, but run it from local storage
