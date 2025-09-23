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
import pythoncom
import winreg
import requests
import psutil
import socket
import wmi

from logger import setup_logger
from champions import get_champion_names
from skin_downloader import download_repo
from skin_installer import install_skins
from update_checker import check_and_update, get_installed_version, get_latest_manager_version, get_latest_lol_version, get_latest_repo_commit
from config import (
    PROJECT_ROOT, DOWNLOAD_DIR, INSTALL_DIR, LOG_DIR, DATA_DIR, UNINSTALL_APP_NAME,
    INSTALLED_DIR, LOL_VERSION_FILE, VERSION_FILE, INSTALLED_HASH_FILE, APP_NAME, SKIN_REPO_COMMIT_FILE
)

import pystray
from PIL import Image, ImageDraw

APP_MUTEX_NAME = "LeagueSkinManagerVN_Mutex_v1"

STATUS_WAITING = "Waiting for League Client"
STATUS_INSTALLING = "Installing skins"
STATUS_FOUND = "League Client detected"

COLOR_BLUE = (30, 144, 255)
COLOR_YELLOW = (255, 200, 0)
COLOR_GREEN = (50, 205, 50)

current_status = STATUS_WAITING

logger = setup_logger(__name__)

# ---------- Utilities ----------
def check_internet_connection(timeout=5.0):
    """Check internet connection using multiple endpoints."""
    test_urls = [
        "https://api.github.com",
        "https://ddragon.leagueoflegends.com",
        "https://www.google.com/generate_204"
    ]

    for url in test_urls:
        try:
            response = requests.head(url, timeout=timeout, allow_redirects=True)
            if response.status_code in (200, 204, 302, 405):
                return True, None
        except requests.exceptions.RequestException:
            continue

    if not is_connected_to_network():
        return False, "No network connection. Check Wi-Fi/Ethernet."
    if is_connected_to_network() and not is_dns_working():
        return False, "DNS resolution failed. Check DNS/router settings."
    return False, "Unable to connect. Check internet/firewall."

def is_connected_to_network():
    """Check for network connection."""
    try:
        return any(
            interface_addresses for interface, interface_addresses in psutil.net_if_addrs().items()
            if '127.0.0.1' not in [addr.address for addr in interface_addresses]
        )
    except:
        return False

def is_dns_working():
    """Check if DNS resolution works."""
    try:
        socket.gethostbyname("google.com")
        return True
    except:
        return False

def handle_internet_check():
    """Handle internet connection check with user feedback."""
    logger.info("Checking internet connectivity...")
    is_connected, error_message = check_internet_connection()

    if not is_connected:
        logger.error(f"Internet check failed: {error_message}")
        message = f"Internet required.\n\nError: {error_message}\n\nPlease check connection and retry."
        ctypes.windll.user32.MessageBoxW(None, message, "Internet Required", 0x10)
        logger.info("Exiting due to no internet")
        sys.exit(1)

    logger.info("Internet verified")
    return True

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
        set_status(STATUS_INSTALLING)
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

        try:
                latest_lol = get_latest_lol_version()
                if latest_lol:
                    with open(LOL_VERSION_FILE, 'w', encoding='utf-8') as f:
                        f.write(latest_lol)
                    logger.info("Wrote current LoL version after install: %s", latest_lol)

                latest_commit = get_latest_repo_commit()
                if latest_commit:
                    with open(SKIN_REPO_COMMIT_FILE, 'w', encoding='utf-8') as f:
                        f.write(latest_commit)
                    logger.info("Wrote current repo commit after install: %s", latest_commit)
        except Exception as e:
            logger.error("Failed to write versions after install: %s", e)

        _install_successful.set()
        logger.info("Auto-install finished. Total installed skins (approx): %d", total_installed)

    except Exception:
        logger.exception("Auto-install encountered an error")
    finally:
        _install_in_progress.clear()
        if is_process_running_by_name("LeagueClient.exe"):
            set_status(STATUS_FOUND)
        else:
            set_status(STATUS_WAITING)

# ---------- Tray / Polling ----------

tray_icon = None
tray_thread = None
polling_thread = None
last_launch_league_pid = None
_stop_threads = threading.Event()
_tray_lock = threading.Lock()

def make_colored_icon(rgb, size=64):
    img = Image.new('RGBA', (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.ellipse((8, 8, size - 8, size - 8), fill=rgb)
    return img

def _build_menu():
    return pystray.Menu(
        pystray.MenuItem(lambda *args: f"Status: {current_status}", None, enabled=False),
        pystray.MenuItem('Start CSLOL Manager', on_start_manager, default=True),
        pystray.MenuItem('Exit', on_exit)
    )

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

def set_status(new_status):
    """Thread-safe update of tray icon image and menu text."""
    global current_status, tray_icon
    with _tray_lock:
        current_status = new_status
        if tray_icon:
            if new_status == STATUS_INSTALLING:
                icon_img = make_colored_icon(COLOR_YELLOW)
            elif new_status == STATUS_FOUND:
                icon_img = make_colored_icon(COLOR_GREEN)
            else:
                icon_img = make_colored_icon(COLOR_BLUE)
            try:
                tray_icon.icon = icon_img
                tray_icon.menu = _build_menu()
            except Exception:
                logger.debug("Failed to set tray icon/menu (maybe not initialized yet)")

def event_watcher_loop():
    """Lightweight WMI query for League Client, low-CPU, standard user permissions."""
    global last_launch_league_pid
    try:
        pythoncom.CoInitialize() 
        c = wmi.WMI() 
        while not _stop_threads.is_set():
            try:
                processes = c.Win32_Process(name="LeagueClient.exe")
                if _install_in_progress.is_set():
                    set_status(STATUS_INSTALLING)
                elif processes:  
                    current_pid = processes[0].ProcessId
                    set_status(STATUS_FOUND)
                    if (last_launch_league_pid != current_pid and
                        not is_process_running_by_name("cslol-manager.exe")):
                        launch_cslol_manager()
                        last_launch_league_pid = current_pid
                else:
                    set_status(STATUS_WAITING)
                    last_launch_league_pid = None
                time.sleep(10)
            
            except Exception as e:
                logger.error("WMI query error: %s", e)
                
    except Exception as e:
        logger.exception("WMI setup failed; fall back to polling")
        polling_loop()
    finally:
        logger.info("Event watcher stopped")

def polling_loop():
    global last_launch_league_pid
    while not _stop_threads.is_set():
        league_proc = None
        for p in psutil.process_iter(["pid", "name"]):
            if p.info["name"] and p.info["name"].lower() == "leagueclient.exe":
                league_proc = p
                break

        if _install_in_progress.is_set():
            set_status(STATUS_INSTALLING)
        else:
            if league_proc:
                set_status(STATUS_FOUND)
                if (last_launch_league_pid != league_proc.pid
                    and not is_process_running_by_name("cslol-manager.exe")):
                    launch_cslol_manager()
                    last_launch_league_pid = league_proc.pid
            else:
                set_status(STATUS_WAITING)
                last_launch_league_pid = None
        _stop_threads.wait(5)

def start_tray():
    global tray_icon, tray_thread, watcher_thread
    icon_image = None
    exe_icon_path = os.path.join(PROJECT_ROOT, "icon.ico")
    if os.path.exists(exe_icon_path):
        try:
            icon_image = Image.open(exe_icon_path)
        except Exception:
            icon_image = None
    if icon_image is None:
        icon_image = make_colored_icon(COLOR_BLUE)

    tray_icon = pystray.Icon("LeagueSkinManagerVN", icon_image, "LeagueSkinManagerVN", _build_menu())

    set_status(current_status)
    
    league_proc = None
    for p in psutil.process_iter(["pid", "name"]):
        if p.info["name"] and p.info["name"].lower() == "leagueclient.exe":
            league_proc = p
            break
    if league_proc:
        set_status(STATUS_FOUND)
        if not is_process_running_by_name("cslol-manager.exe") and not _install_in_progress.is_set():
            launch_cslol_manager()
            global last_launch_league_pid
            last_launch_league_pid = league_proc.pid
    
    watcher_thread = threading.Thread(target=event_watcher_loop, daemon=True)  # Renamed from polling_thread
    watcher_thread.start()
    
    tray_icon.run()

# ---------- Startup ----------

def main():
    ensure_windows()
    if not handle_internet_check():
        return

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
        if update_results.get('skin_repo_commit_changed'):
            logger.info("Skin repo commit changed and skins were reset by update checker")
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

    installer_thread = None 
    if needs_install:
        installer_thread = threading.Thread(target=install_all_skins, kwargs={'skip_chromas': True}, daemon=True)
        installer_thread.start()
    else:
        logger.info("No install required; skipping auto-install.")
        _install_successful.set()

    try:
        start_tray()
    except Exception:
        logger.exception("Tray failed to start")
        if installer_thread is not None and installer_thread.is_alive():
            installer_thread.join()
        sys.exit(0)

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        logger.info("Interrupted by user")
    except Exception:
        logger.exception("Fatal error in main")
        raise
