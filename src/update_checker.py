import os
import requests
import shutil
import zipfile
import tempfile
from config import (
    INSTALL_DIR,
    GITHUB_RELEASES_URL,
    DATA_DIR,
    VERSION_FILE,
    LOL_VERSION_FILE,
    DOWNLOAD_DIR,
    LOL_VERSION_URL,
)
from logger import setup_logger

logger = setup_logger(__name__)

def get_installed_version():
    try:
        with open(VERSION_FILE) as f:
            return f.read().strip()
    except FileNotFoundError:
        return None

def get_latest_manager_version():
    try:
        resp = requests.get(GITHUB_RELEASES_URL, timeout=10)
        resp.raise_for_status()
        return resp.json().get("tag_name", "")
    except Exception as e:
        logger.error(f"Manager version check failed: {e}")
        return None

def get_latest_lol_version():
    try:
        versions = requests.get(LOL_VERSION_URL, timeout=5).json()
        return versions[0] if versions else None
    except Exception as e:
        logger.error(f"LoL version fetch failed: {e}")
        return None

def download_asset(asset_url, temp_dir):
    temp_file = os.path.join(temp_dir, os.path.basename(asset_url))
    try:
        with requests.get(asset_url, stream=True, timeout=30) as response:
            response.raise_for_status()
            with open(temp_file, "wb") as f:
                for chunk in response.iter_content(chunk_size=8192):
                    f.write(chunk)
        return temp_file
    except Exception as e:
        logger.error(f"Download failed: {e}")
        return None

def install_update(temp_file, new_version):
    """
    Install update from downloaded file and record version.
    NOTE: Preserves the original extraction behavior (extract into DATA_DIR)
    to avoid nested folders introduced by repo zip internal structure.
    """
    try:
        for item in os.listdir(INSTALL_DIR):
            if item != "installed":
                path = os.path.join(INSTALL_DIR, item)
                if os.path.isdir(path):
                    shutil.rmtree(path)
                else:
                    try:
                        os.remove(path)
                    except Exception:
                        pass

        with zipfile.ZipFile(temp_file, "r") as zip_ref:
            zip_ref.extractall(DATA_DIR)

        with open(VERSION_FILE, "w", encoding="utf-8") as f:
            f.write(new_version)

        logger.info("Installed update and wrote version %s", new_version)
        return True
    except Exception as e:
        logger.error(f"Installation failed: {e}")
        return False

def check_and_update():
    results = {'manager_updated': False, 'lol_version_changed': False}

    current_mgr = get_installed_version()
    latest_mgr = get_latest_manager_version()
    if latest_mgr and current_mgr != latest_mgr:
        logger.info("Manager update detected: %s -> %s", current_mgr, latest_mgr)
        try:
            release_data = requests.get(GITHUB_RELEASES_URL, timeout=10).json()
            asset_url = next(
                (a["browser_download_url"] for a in release_data.get("assets", []) if a["name"].endswith(".zip")),
                None
            )
            if asset_url:
                with tempfile.TemporaryDirectory() as tmp:
                    tmp_file = download_asset(asset_url, tmp)
                    if tmp_file and install_update(tmp_file, latest_mgr):
                        results['manager_updated'] = True
                        logger.info("Manager updated to %s", latest_mgr)
            else:
                logger.warning("No zip asset found in latest release (%s).", latest_mgr)

            # Delete cslol-diag.exe
            diag_path = os.path.join(INSTALL_DIR, "cslol-tools", "cslol-diag.exe")
            if os.path.exists(diag_path):
                os.remove(diag_path)
                logger.info(f"Deleted {diag_path}")

        except Exception as e:
            logger.exception("Manager update process failed: %s", e)

    current_lol = None
    if os.path.exists(LOL_VERSION_FILE):
        with open(LOL_VERSION_FILE, 'r', encoding='utf-8') as f:
            current_lol = f.read().strip()

    latest_lol = get_latest_lol_version()
    if latest_lol and current_lol != latest_lol:
        logger.info("LoL version changed: %s -> %s", current_lol, latest_lol)
        try:
            shutil.rmtree(DOWNLOAD_DIR, ignore_errors=True)
            os.makedirs(DOWNLOAD_DIR, exist_ok=True)
            installed_dir = os.path.join(INSTALL_DIR, "installed")
            shutil.rmtree(installed_dir, ignore_errors=True)
            os.makedirs(installed_dir, exist_ok=True)
            with open(LOL_VERSION_FILE, 'w', encoding='utf-8') as f:
                f.write(latest_lol)
            results['lol_version_changed'] = True
        except Exception:
            logger.exception("Failed to reset skins on LoL version change")

    return results
