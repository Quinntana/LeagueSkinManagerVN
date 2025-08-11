import os
import sys

if getattr(sys, 'frozen', False):
    PROJECT_ROOT = os.path.dirname(sys.executable)
else:
    PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

APPDATA = os.environ.get('APPDATA') if sys.platform == 'win32' else os.path.join(PROJECT_ROOT, 'data')
APP_NAME = "LeagueSkinManagerVN"
UNINSTALL_APP_NAME = "LeagueSkinManagerVNUninstall"
DATA_DIR = os.path.join(APPDATA, APP_NAME)

DOWNLOAD_DIR = os.path.join(DATA_DIR, "skins")
INSTALL_DIR = os.path.join(DATA_DIR, "cslol-manager")
INSTALLED_DIR = os.path.join(INSTALL_DIR, "installed")
LOG_DIR = os.path.join(DATA_DIR, "logs")

PROFILE_FILE = os.path.join(INSTALLED_DIR, "default_profile.txt")
LOL_VERSION_FILE = os.path.join(DATA_DIR, "lol_version.txt")
VERSION_FILE = os.path.join(INSTALL_DIR, "version.txt")
REPO_ZIP_PATH = os.path.join(DOWNLOAD_DIR, "lol-skins-main.zip")
INSTALLED_HASH_FILE = os.path.join(DATA_DIR, "installed_hash.txt")


LOL_VERSION_URL = "https://ddragon.leagueoflegends.com/api/versions.json"
CHAMPION_DATA_URL = "https://ddragon.leagueoflegends.com/cdn/{version}/data/en_US/champion.json"
GITHUB_RELEASES_URL = "https://api.github.com/repos/LeagueToolkit/cslol-manager/releases/latest"
SKINS_REPO_URL = "https://github.com/darkseal-org/lol-skins/archive/refs/heads/main.zip"

os.makedirs(DOWNLOAD_DIR, exist_ok=True)
os.makedirs(INSTALL_DIR, exist_ok=True)
os.makedirs(INSTALLED_DIR, exist_ok=True)
os.makedirs(LOG_DIR, exist_ok=True)
