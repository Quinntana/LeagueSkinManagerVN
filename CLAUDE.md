# LeagueSkinManagerVN

**OpenSpec drives this project.** `openspec/config.yaml` holds the context that
is injected into every OpenSpec request; the four capability specs in
`openspec/specs/` hold the behaviour. Use the `/opsx:` commands — `propose`,
`apply`, `archive`, `explore`, `update`, `sync` — for any non-trivial change.
Everything below is a summary pointing at those.

## What this is

One portable Windows tray executable. It mirrors a set of League skin packages
into LTK Manager, opens an enemy-cooldown board during a match, and owns every
file it creates. Windows only, Python 3.10–3.14, built with PyInstaller into a
single `--onefile --noconsole` binary.

It is **not** a skin manager. It is a tray-resident supervisor of external
tools, one of which needs a skin pipeline. Every rewrite before this one
happened because the code was organised around whichever backend was current;
which external tool is in use must stay a plug-in point, never a structural
assumption.

## The rule that matters most

**Anything asserted about LTK's on-disk behaviour must be measured, not
inferred.** During this rebuild, four decisions reached by careful reasoning
were disproved by experiment, and two further facts surfaced that no amount of
reading would have produced. `openspec/specs/ltk-integration/spec.md` records
all of them. Do not relax a requirement there without re-running the
corresponding test against a copy of LTK's data root.

The procedure: copy `%APPDATA%\dev.leaguetoolkit.manager`, mutate, launch LTK,
read its log under `logs/`, restore the copy.

## Gates

```powershell
poetry run ruff format --check src tests build.py
poetry run ruff check src tests build.py
poetry run mypy src/league_skin_manager
poetry run pytest
```

All four must pass; coverage floor is 80%. `tests/test_architecture.py`
enforces the layered import rule — a new module without a layer assignment
fails the suite.

## Things that will bite you

- **Do not run the built `.exe` from a Google Drive / OneDrive path.** Every
  TLS handshake times out after ~34s and fails. The same binary works
  instantly from local storage. Build anywhere; run it locally.
- `.venv-build` lives on the synced drive, so its `requests` cannot reach
  GitHub either. Use the system Python for any live network check.
- Nothing outside `cooldown/` may import its internals; it exposes exactly
  four functions.
- Adapters may import `requests`/`psutil`/`ctypes`/`winreg`. Nothing else may.

## Deliberately absent — do not helpfully re-add

- CSLOL as a backend, an installed-vs-portable split, separate setup and
  uninstaller executables, a tray status-row state machine, journalled
  transactions, digest indexes, an install manifest.
- Automatic clearing of LTK's enabled mods. It cannot tell LTK's auto-enable
  apart from skins the user chose, so it deletes their selections every
  launch. See the reasoning in `openspec/specs/ltk-integration/spec.md`.
- LTK version management. LTK ships its own updater; this application verifies
  the first install and then gets out of the way.

## Validate against real data where it exists

Unit tests are necessary and not sufficient. The checks that caught real bugs
ran against live sources: cache filenames are upstream Git blob SHAs, so
recomputing them is a free ~1,900-case fixture; package validation must accept
exactly what LTK accepts; the Data Dragon rank rules should be run over all
173 champions.
