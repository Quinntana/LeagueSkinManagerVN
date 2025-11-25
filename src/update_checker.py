import os
import subprocess
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
    SKINS_REPO_COMMIT_URL,
    SKIN_REPO_COMMIT_FILE,
    PROFILES_DIR
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

def get_latest_repo_commit():
    """Fetch latest commit SHA from darkseal repo."""
    try:
        headers = {"Accept": "application/vnd.github+json"}
        resp = requests.get(SKINS_REPO_COMMIT_URL, headers=headers, timeout=10)
        resp.raise_for_status()
        return resp.json().get("sha")
    except Exception as e:
        logger.error(f"Repo commit fetch failed: {e}")
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
    to avoid nested folders introduced by repo zip internal structure.
    """
    try:
        for item in os.listdir(INSTALL_DIR):
            if item != "installed":  
                path = os.path.join(INSTALL_DIR, item)
                if item == "profiles" and os.path.isdir(path):
                    for profile_item in os.listdir(path):
                        profile_path = os.path.join(path, profile_item)
                        if not profile_item.endswith(".profile"):
                            try:
                                if os.path.isdir(profile_path):
                                    shutil.rmtree(profile_path)
                                else:
                                    os.remove(profile_path)
                                logger.info("Deleted non-profile item in profiles: %s", profile_item)
                            except Exception as e:
                                logger.error("Failed to delete non-profile item %s: %s", profile_item, e)
                else:
                    try:
                        if os.path.isdir(path):
                            shutil.rmtree(path)
                        else:
                            os.remove(path)
                        logger.info("Deleted item during manager update: %s", item)
                    except Exception as e:
                        logger.error("Failed to delete item %s during manager update: %s", item, e)

        if temp_file.endswith(".zip"):
            logger.info("Extracting update via ZipFile...")
            with zipfile.ZipFile(temp_file, "r") as zip_ref:
                zip_ref.extractall(DATA_DIR)
        
        elif temp_file.endswith(".exe"):
            logger.info("Extracting update via SFX Exe...")
            output_flag = f"-o{os.path.abspath(DATA_DIR)}"
            
            process = subprocess.run(
                [temp_file, "-y", output_flag],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                creationflags=subprocess.CREATE_NO_WINDOW  
            )
            
            if process.returncode != 0:
                logger.error(f"SFX extraction failed. Code: {process.returncode}, Err: {process.stderr.decode()}")
                return False

        with open(VERSION_FILE, "w", encoding="utf-8") as f:
            f.write(new_version)

        logger.info("Installed update and wrote version %s", new_version)
        return True
    except Exception as e:
        logger.error(f"Installation failed: {e}")
        return False

def reset_skins_and_update_file(file_path, new_value, change_key):
    try:
        shutil.rmtree(DOWNLOAD_DIR, ignore_errors=True)
        os.makedirs(DOWNLOAD_DIR, exist_ok=True)
        installed_dir = os.path.join(INSTALL_DIR, "installed")
        shutil.rmtree(installed_dir, ignore_errors=True)
        os.makedirs(installed_dir, exist_ok=True)
        return True
    except Exception as e:
        logger.exception(f"Failed to reset skins on {change_key}")
        return False

def check_and_update():
    results = {'manager_updated': False, 'lol_version_changed': False, 'skin_repo_commit_changed': False}

    current_mgr = get_installed_version()
    latest_mgr = get_latest_manager_version()
    if latest_mgr and current_mgr != latest_mgr:
        logger.info("Manager update detected: %s -> %s", current_mgr, latest_mgr)
        try:
            release_data = requests.get(GITHUB_RELEASES_URL, timeout=10).json()
            assets = release_data.get("assets", [])

            asset_obj = next((a for a in assets if a["name"].endswith(".zip")), None)

            if not asset_obj:
                asset_obj = next((a for a in assets if a["name"].endswith(".exe") and "manager" in a["name"].lower()), None)

            if asset_obj:
                asset_url = asset_obj["browser_download_url"]
                asset_name = asset_obj["name"]
                logger.info(f"Downloading update asset: {asset_name}")

                with tempfile.TemporaryDirectory() as tmp:
                    tmp_file = download_asset(asset_url, tmp)
                    
                    if tmp_file and install_update(tmp_file, latest_mgr):
                        results['manager_updated'] = True
                        logger.info("Manager updated to %s", latest_mgr)
            else:
                logger.warning("No suitable update asset (.zip or .exe) found for release %s", latest_mgr)

        except Exception as e:
            logger.exception("Manager update process failed: %s", e)

    current_lol = None
    lol_file_exists = os.path.exists(LOL_VERSION_FILE)
    if lol_file_exists:
        with open(LOL_VERSION_FILE, 'r', encoding='utf-8') as f:
            current_lol = f.read().strip()

    latest_lol = get_latest_lol_version()
    if latest_lol and lol_file_exists and current_lol != latest_lol or not lol_file_exists:  
        logger.info("LoL version changed: %s -> %s", current_lol, latest_lol)
        if reset_skins_and_update_file(LOL_VERSION_FILE, latest_lol, 'lol_version_changed'):
            results['lol_version_changed'] = True
        else:
            results['lol_version_changed'] = False

    current_repo_commit = None
    commit_file_exists = os.path.exists(SKIN_REPO_COMMIT_FILE)
    if commit_file_exists:
        with open(SKIN_REPO_COMMIT_FILE, 'r', encoding='utf-8') as f:
            current_repo_commit = f.read().strip()

    latest_repo_commit = get_latest_repo_commit()
    if latest_repo_commit and commit_file_exists and current_repo_commit != latest_repo_commit or not commit_file_exists:  
        logger.info("Repo commit changed: %s -> %s", current_repo_commit, latest_repo_commit)
        if reset_skins_and_update_file(SKIN_REPO_COMMIT_FILE, latest_repo_commit, 'skin_repo_commit_changed'):
            results['skin_repo_commit_changed'] = True
        else:
            results['skin_repo_commit_changed'] = False

    return results
