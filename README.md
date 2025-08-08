# LeagueSkinManagerVN

> **A lightweight Windows service for automating League of Legends custom skin installation**
> Built with Python, Poetry, and PyInstaller.
> Includes background service (`LeagueSkinManagerVN.exe`) and standalone UI (`cslol-manager.exe`).

---

## 📌 Features

- **Automatic skin installation** — installs *all champion skins* (excluding chromas) without user input.
- **Fast install check** — uses a lightweight folder-hash to detect missing or outdated skins.
- **Single-instance service** — prevents multiple background services from running at once.
- **Tray integration** — hide to system tray with:
  - `Start CSLOL Manager`
  - `Exit`
- **CSLOL Manager integration** — launches automatically after skin installation or when League Client is detected.
- **Robust version control**:
  - Writes LoL version only after successful skin installation.
  - Writes CSLOL Manager version only after successful install/update.
- **Windows startup support** — can run silently in the background.

---

## 🖥️ Executables

| Executable                 | Role                                                                                   |
|----------------------------|----------------------------------------------------------------------------------------|
| `LeagueSkinManagerVN.exe`  | Background service — auto-installs skins, launches CSLOL Manager, manages tray icon.  |
| `cslol-manager.exe`        | Standalone UI for skin management. Installed automatically from GitHub release.       |
| `LeagueSkinManagerVNUninstall.exe` | Cleans all data, requires no network connection, prompts if other instances running. |

---

## 📂 Data & Paths

All data is stored in: %APPDATA%\LeagueSkinManagerVN\

---

## 🚀 Requirements

- **Windows** (x64)
- Python 3.10+
- [Poetry](https://python-poetry.org/)
