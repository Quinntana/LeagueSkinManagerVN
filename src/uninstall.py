import os
import sys
import time
import shutil
import ctypes
from ctypes import wintypes
import psutil
from config import DATA_DIR, INSTALL_DIR, PROJECT_ROOT

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

def main():
    if sys.platform != 'win32':
        try:
            shutil.rmtree(DATA_DIR, ignore_errors=True)
            print("Removed", DATA_DIR)
        except Exception as e:
            print("Failed to remove:", e)
        return

    running, proc_name = any_running(["LeagueSkinManagerVN.exe", "cslol-manager.exe", "LeagueSkinManagerVNUninstall.exe"])
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
        shutil.rmtree(DATA_DIR, ignore_errors=True)
        message_box(f"Successfully removed {DATA_DIR}", "Uninstall complete")
    except Exception as e:
        message_box(f"Failed to remove {DATA_DIR}: {e}", "Uninstall failed")

if __name__ == "__main__":
    main()
