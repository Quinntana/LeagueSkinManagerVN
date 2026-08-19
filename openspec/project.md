# Project context

## What this is

A single portable Windows tray executable that mirrors a set of League of
Legends skin packages into LTK Manager, opens an enemy-cooldown board during a
match, and owns every file it creates.

It is **not** a skin manager. It is a tray-resident supervisor of external
tools, one of which happens to need a skin pipeline. Every rewrite before this
one happened because the code was organised around whichever backend was
current, so when the backend changed everything changed. Which external tool
is in use must stay a plug-in point, never a structural assumption.

## Stack

- Python 3.10–3.14, Windows only
- `requests`, `psutil`, `pystray`, `Pillow`, `tkinter`
- Built with PyInstaller into one `--onefile --noconsole` executable
- `ruff format` · `ruff check` · `mypy --strict` · `pytest` (80% coverage gate)

## Architecture

Ports and Adapters (Cockburn, 2005) over a layered import rule, in a modular
monolith. Enforced by `tests/test_architecture.py`, which parses the import
graph and fails on any upward import, any module without a layer assignment,
and any direct use of `requests`/`psutil`/`ctypes`/`winreg` outside an adapter.

```
L0  config  atomic  hashing  logging_setup  windows
L1  settings  fantome
L2  github  cache  seed  ltk  porofessor  process_watch  uninstall
L3  sync
L4  tray  cooldown/
L5  app  __main__
```

`app.py` is the composition root and the only module that knows concrete
types. Everything else talks to Protocols and plain values.

`cooldown/` is the one enforced internal boundary: it exposes exactly
`open_panel`, `close_panel`, `is_open`, `apply_display`. Nothing outside may
reach past those four.

## Conventions

- Ports exist for external systems and test seams, not for internal helpers.
  A Protocol with one implementation and no seam is ceremony, not architecture.
- Adapters return values and log; they do not raise across layers.
- UI callbacks never propagate exceptions — a raising callback would take the
  tray's event loop with it.
- Anything asserted about LTK's on-disk behaviour must be **measured**, not
  inferred. See `specs/ltk-integration/spec.md`; four decisions reached by
  reasoning alone were disproved by experiment.

## Things that are deliberately absent

- No CSLOL backend, no installed-versus-portable split, no separate setup or
  uninstaller executable, no status-row state machine, no journalled
  transactions, no digest indexes, no install manifest.
- Uninstall is a tray action, not a program.
- The application never manages LTK versions; LTK ships its own updater.
