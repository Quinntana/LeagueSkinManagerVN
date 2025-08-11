import os
import sys
import time
import shutil
import ctypes
import winreg
from ctypes import wintypes
import psutil
from config import DATA_DIR, INSTALL_DIR, PROJECT_ROOT, APP_NAME

def is_admin():
    try:
        return ctypes.windll.shell32.IsUserAnAdmin() != 0
    except Exception:
        return False

def run_as_admin():
    """
    Relaunch the script as admin. Returns True if the new process was started.
    """
    params = " ".join([f'"{p}"' for p in sys.argv])
    executable = sys.executable
    ret = ctypes.windll.shell32.ShellExecuteW(None, "runas", executable, params, None, 1)
    return ret > 32

def any_running(names):
    for p in psutil.process_iter(['name']):
        try:
            if p.info['name'] and p.info['name'].lower() in {n.lower() for n in names}:
                return True, p.info['name']
        except Exception:
            continue
    return False, None

def message_box(text, title="LeagueSkinManagerVN"):
    ctypes.windll.user32.MessageBoxW(None, text, title, 0)

def remove_from_startup():
    """Remove the startup registry entry if it exists."""
    try:
        key_path = r"Software\Microsoft\Windows\CurrentVersion\Run"
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path, 0, winreg.KEY_ALL_ACCESS)
        try:
            winreg.DeleteValue(key, APP_NAME)
            print(f"Removed startup registry entry for {APP_NAME}")
        except FileNotFoundError:
            print(f"No startup registry entry found for {APP_NAME}")
        winreg.CloseKey(key)
    except Exception as e:
        print(f"Failed to remove startup registry entry: {e}")

def remove_start_menu_shortcut():
    """
    Remove per-user Start Menu Programs shortcut/folder that main() creates:
    %APPDATA%\Microsoft\Windows\Start Menu\Programs\<APP_NAME>\*
    This will delete the .lnk (if created).
    """
    try:
        appdata = os.environ.get("APPDATA")
        if not appdata:
            print("APPDATA not set; skipping Start Menu cleanup.")
            return

        start_menu = os.path.join(appdata, r"Microsoft\Windows\Start Menu\Programs")
        shortcut_dir = os.path.join(start_menu, APP_NAME)

        if not os.path.exists(shortcut_dir):
            print("No Start Menu shortcut folder found:", shortcut_dir)
            return

        # Remove files in the folder (lnk, exe copy, etc.)
        for entry in os.listdir(shortcut_dir):
            path = os.path.join(shortcut_dir, entry)
            try:
                if os.path.isfile(path) or os.path.islink(path):
                    os.remove(path)
                elif os.path.isdir(path):
                    shutil.rmtree(path, ignore_errors=True)
            except Exception as e:
                print(f"Failed to remove {path}: {e}")

        # Try to remove the now-empty folder; if not empty, remove tree as fallback
        try:
            os.rmdir(shortcut_dir)
            print("Removed Start Menu folder:", shortcut_dir)
        except Exception:
            shutil.rmtree(shortcut_dir, ignore_errors=True)
            print("Force-removed Start Menu folder:", shortcut_dir)

    except Exception as e:
        print("remove_start_menu_shortcut failed:", e)

def main():
    if sys.platform != 'win32':
        try:
            shutil.rmtree(DATA_DIR, ignore_errors=True)
            print("Removed", DATA_DIR)
        except Exception as e:
            print("Failed to remove:", e)
        return

    running, proc_name = any_running(["LeagueSkinManagerVN.exe", "cslol-manager.exe"])
    if running:
        message_box(f"Please close {proc_name} before uninstalling.", "Uninstall aborted")
        print("Found running process:", proc_name)
        return

    if not is_admin():
        answer = ctypes.windll.user32.MessageBoxW(None,
            "This action requires administrator privileges. Elevate and continue?",
            "Administrator required", 1)
        if answer == 6:
            started = run_as_admin()
            if started:
                sys.exit(0)
            else:
                message_box("Failed to request elevation. Aborting.", "Uninstall aborted")
                return
        else:
            message_box("Uninstall cancelled.", "Cancelled")
            return

    try:
        remove_from_startup()
        remove_start_menu_shortcut()
        shutil.rmtree(DATA_DIR, ignore_errors=True)
        message_box(f"Successfully removed {DATA_DIR}", "Uninstall complete")
    except Exception as e:
        message_box(f"Failed to remove {DATA_DIR}: {e}", "Uninstall failed")

if __name__ == "__main__":
    main()
