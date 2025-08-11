import sys
import os
import time
import threading
import subprocess
import ctypes
from ctypes import wintypes
import zlib
import logging
import shutil
import winreg

import psutil
from logger import setup_logger
from champions import get_champion_names
from skin_downloader import download_repo
from skin_installer import install_skins
from update_checker import check_and_update, get_installed_version, get_latest_manager_version
from config import (
    PROJECT_ROOT, DOWNLOAD_DIR, INSTALL_DIR, LOG_DIR, DATA_DIR, UNINSTALL_APP_NAME,
    INSTALLED_DIR, LOL_VERSION_FILE, VERSION_FILE, INSTALLED_HASH_FILE, APP_NAME
)

import pystray
from PIL import Image, ImageDraw

APP_MUTEX_NAME = "LeagueSkinManagerVN_Mutex_v1"

logger = setup_logger(__name__)

# ---------- Utilities ----------
def add_to_startup():
    """Add the executable to Windows startup registry if not already present."""
    try:
        exe_path = sys.executable
        key_path = r"Software\Microsoft\Windows\CurrentVersion\Run"
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path, 0, winreg.KEY_ALL_ACCESS)

        try:
            value, _ = winreg.QueryValueEx(key, APP_NAME)
            if value == exe_path:
                logger.info("Already added to startup.")
                return
        except FileNotFoundError:
            pass

        winreg.SetValueEx(key, APP_NAME, 0, winreg.REG_SZ, exe_path)
        winreg.CloseKey(key)
        logger.info("Added to Windows startup: %s", exe_path)
    except Exception as e:
        logger.error("Failed to add to startup: %s", e)

def ensure_searchable_in_startmenu():
    """
    Create a per-user Start Menu shortcut for the running executable so Windows Search
    and the Start menu can find the app.
    """
    try:
        start_menu = os.path.join(os.environ.get('APPDATA', ''), r"Microsoft\Windows\Start Menu\Programs")
        if not start_menu:
            logger.warning("APPDATA not set; cannot create Start Menu shortcut.")
            return False

        shortcut_dir = os.path.join(start_menu, APP_NAME)
        os.makedirs(shortcut_dir, exist_ok=True)
        shortcut_path = os.path.join(shortcut_dir, f"{APP_NAME}.lnk")

        if os.path.exists(shortcut_path):
            logger.info("Start Menu shortcut already exists: %s", shortcut_path)
            return True

        exe_path = sys.executable
        work_dir = os.path.dirname(exe_path)
        icon_path = os.path.join(work_dir, "icon.ico") if os.path.exists(os.path.join(work_dir, "icon.ico")) else exe_path

        try:
            from win32com.client import Dispatch
            shell = Dispatch('WScript.Shell')
            shortcut = shell.CreateShortcut(shortcut_path)
            shortcut.TargetPath = exe_path
            shortcut.WorkingDirectory = work_dir
            shortcut.IconLocation = icon_path
            shortcut.Description = "LeagueSkinManagerVN"
            shortcut.Save()
            logger.info("Created Start Menu shortcut: %s", shortcut_path)
            return True
        except Exception as e:
            logger.warning("Failed to create .lnk via pywin32: %s.", e)

    except Exception:
        logger.exception("Failed to ensure Start Menu shortcut")
        return False

def ensure_windows():
    if sys.platform != "win32":
        logger.error("This executable is Windows-only.")
        print("Error: This executable only runs on Windows.")
        sys.exit(1)

def ensure_paths():
    for d in (DOWNLOAD_DIR, INSTALL_DIR, LOG_DIR, INSTALLED_DIR):
        os.makedirs(d, exist_ok=True)

def create_mutex():
    """Create a named mutex to ensure single instance (Win32)."""
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    CreateMutexW = kernel32.CreateMutexW
    CreateMutexW.argtypes = [wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR]
    CreateMutexW.restype = wintypes.HANDLE

    mutex = CreateMutexW(None, False, APP_MUTEX_NAME)
    if not mutex:
        err = ctypes.get_last_error()
        logger.error(f"CreateMutexW failed: {err}")
        return None

    last_error = ctypes.get_last_error()
    if last_error == 183:
        return None
    return mutex

def exit_if_already_running():
    mutex = create_mutex()
    if not mutex:
        if os.path.exists(INSTALLED_HASH_FILE):
            exe_path = os.path.join(INSTALL_DIR, "cslol-manager.exe")
            if os.path.exists(exe_path):
                try:
                    subprocess.Popen([exe_path], shell=False)
                    print("Another instance is running. Launching CSLOL-Manager. Closing in 3 seconds...")
                    logger.info("Another instance detected; launched CSLOL Manager and exiting.")
                except Exception:
                    logger.exception("Another instance detected but failed to launch CSLOL Manager.")
                    print("Another instance is running. Closing in 3 seconds...")
            else:
                print("Another instance is running. Closing in 3 seconds...")
                logger.warning("Another instance detected; CSLOL Manager exe not found.")
        else:
            print("Another instance is running. Closing in 3 seconds...")
            logger.warning("Another instance detected during install; exiting.")
        time.sleep(3)
        sys.exit(0)
    return mutex

def simple_folder_hash(path):
    """Very fast (not cryptographically secure) hash of folder names in INSTALLED_DIR."""
    try:
        entries = []
        if os.path.exists(path):
            for name in os.listdir(path):
                if os.path.isdir(os.path.join(path, name)):
                    entries.append(name)
        entries.sort()
        s = "|".join(entries).encode("utf-8", errors="ignore")
        return format(zlib.crc32(s) & 0xFFFFFFFF, '08x')
    except Exception as e:
        logger.exception("Hashing failed")
        return None

def read_hash():
    try:
        with open(INSTALLED_HASH_FILE, "r") as f:
            return f.read().strip()
    except Exception:
        return None

def write_hash(h):
    try:
        with open(INSTALLED_HASH_FILE, "w") as f:
            f.write(str(h))
    except Exception:
        logger.exception("Failed to write hash file")

def is_process_running_by_name(name):
    name = name.lower()
    for p in psutil.process_iter(["name"]):
        try:
            if p.info["name"] and p.info["name"].lower() == name:
                return True
        except Exception:
            continue
    return False

def launch_cslol_manager():
    exe_path = os.path.join(INSTALL_DIR, "cslol-manager.exe")
    if os.path.exists(exe_path):
        try:
            subprocess.Popen([exe_path], shell=False)
            logger.info("Launched CSLOL Manager.")
            return True
        except Exception as e:
            logger.exception(f"Failed to launch CSLOL Manager: {e}")
            return False
    else:
        logger.warning("CSLOL Manager exe not found.")
        return False

# ---------- Installation Logic ----------

_install_lock = threading.Lock()
_install_in_progress = threading.Event()
_install_successful = threading.Event()

def install_all_skins(skip_chromas=True):
    """Download repo (if needed) and install every champion's skins (skip chromas by default)."""
    if _install_in_progress.is_set():
        return
    _install_in_progress.set()
    try:
        logger.info("Starting auto-install of all champion skins (skip chromas=%s)", skip_chromas)
        if not download_repo():
            logger.error("Failed to download skins repository.")
            return

        champions = get_champion_names()
        if not champions:
            logger.warning("Could not fetch champion list; aborting install.")
            return

        total_installed = 0
        for i, champ in enumerate(champions, 1):
            logger.info("Installing (%d/%d): %s", i, len(champions), champ)
            installed = install_skins(champ, skip_chromas)
            total_installed += (installed or 0)

        h = simple_folder_hash(INSTALLED_DIR)
        if h:
            write_hash(h)
            logger.info("Wrote installed hash %s", h)
        _install_successful.set()
        logger.info("Auto-install finished. Total installed skins (approx): %d", total_installed)

        try:
            if os.path.exists(os.path.join(INSTALL_DIR, "cslol-manager.exe")):
                if not is_process_running_by_name("cslol-manager.exe"):
                    launch_cslol_manager()
        except Exception:
            logger.exception("Failed to auto-launch cslol-manager after skin installation")

    except Exception:
        logger.exception("Auto-install encountered an error")
    finally:
        _install_in_progress.clear()

# ---------- Tray / Polling ----------

tray_icon = None
tray_thread = None
polling_thread = None
_stop_threads = threading.Event()

def make_default_icon():
    img = Image.new('RGBA', (64, 64), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.ellipse((8, 8, 56, 56), fill=(30, 144, 255))
    return img

def on_start_manager(icon, item):
    launch_cslol_manager()

def on_exit(icon, item):
    logger.info("User requested exit from tray")
    _stop_threads.set()
    try:
        icon.stop()
    except Exception:
        pass
    time.sleep(0.5)
    sys.exit(0)

def polling_loop():
    logger.info("Polling thread started (looking for LeagueClient.exe every 5s)")
    while not _stop_threads.is_set():
        try:
            if is_process_running_by_name("LeagueClient.exe"):
                logger.debug("LeagueClient.exe detected")
                if os.path.exists(os.path.join(INSTALL_DIR, "cslol-manager.exe")) and not is_process_running_by_name("cslol-manager.exe"):
                    logger.info("Launching CSLOL Manager because LeagueClient.exe is active")
                    launch_cslol_manager()
        except Exception:
            logger.exception("Polling loop error")
        _stop_threads.wait(5)

def start_tray():
    global tray_icon, tray_thread, polling_thread
    icon_image = None
    exe_icon_path = os.path.join(PROJECT_ROOT, "icon.ico")
    if os.path.exists(exe_icon_path):
        try:
            icon_image = Image.open(exe_icon_path)
        except Exception:
            icon_image = None
    if icon_image is None:
        icon_image = make_default_icon()

    menu = pystray.Menu(
        pystray.MenuItem('Start CSLOL Manager', on_start_manager),
        pystray.MenuItem('Exit', on_exit)
    )
    tray_icon = pystray.Icon("LeagueSkinManagerVN", icon_image, "LeagueSkinManagerVN", menu)

    polling_thread = threading.Thread(target=polling_loop, daemon=True)
    polling_thread.start()

    tray_icon.run()

# ---------- Startup ----------

def main():
    ensure_windows()
    mutex = exit_if_already_running()
    ensure_paths()
    add_to_startup()
    ensure_searchable_in_startmenu()

    logger.info("Checking for updates")
    try:
        update_results = check_and_update()
        if update_results.get('manager_updated'):
            logger.info("CSLOL Manager updated by update checker")
        if update_results.get('lol_version_changed'):
            logger.info("LoL version changed and skins were reset by update checker")
    except Exception:
        logger.exception("Update check failed")

    current_hash = read_hash()
    new_hash = simple_folder_hash(INSTALLED_DIR)
    needs_install = False
    if current_hash is None or new_hash is None:
        needs_install = True
    elif current_hash != new_hash:
        needs_install = True

    logger.info("Installed hash (file)=%s computed=%s needs_install=%s", current_hash, new_hash, needs_install)

    if needs_install:
        installer_thread = threading.Thread(target=install_all_skins, kwargs={'skip_chromas': True}, daemon=True)
        installer_thread.start()
    else:
        logger.info("No install required; skipping auto-install.")
        _install_successful.set()

    try:
        if os.path.exists(INSTALLED_HASH_FILE):
            if os.path.exists(os.path.join(INSTALL_DIR, "cslol-manager.exe")):
                if not is_process_running_by_name("cslol-manager.exe"):
                    launch_cslol_manager()
    except Exception:
        logger.exception("Launch CSLol-Manager.exe failed during subsequent launch.")

    try:
        start_tray()
    except Exception:
        logger.exception("Tray failed to start")
        while installer_thread.is_alive():
            time.sleep(1)
        sys.exit(0)

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        logger.info("Interrupted by user")
    except Exception:
        logger.exception("Fatal error in main")
        raise
