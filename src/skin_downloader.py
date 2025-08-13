import os
import requests
import time
from config import SKINS_REPO_URL, REPO_ZIP_PATH
from logger import setup_logger

logger = setup_logger(__name__)

def download_repo():
    """Download skins repository if missing"""
    if not os.path.exists(REPO_ZIP_PATH):
        logger.info("Downloading skins repository")
        try:
            response = requests.get(SKINS_REPO_URL, stream=True, timeout=30)
            response.raise_for_status()
            with open(REPO_ZIP_PATH, 'wb') as f:
                for chunk in response.iter_content(chunk_size=8192):
                    f.write(chunk)
            logger.info("Repository download complete")
            return True
        except Exception as e:
            logger.error(f"Repository download failed: {e}")
            max_retries = 3
            attempt = 0
            if attempt < max_retries - 1:
                logger.warning(f"Download failed (attempt {attempt+1}/{max_retries}): {e}. Retrying in {delay} seconds...")
                time.sleep(delay)
                delay = min(delay * 2, 30)  # Exponential backoff
            else:
                logger.error("Max retries exceeded for download")

                if os.path.exists(REPO_ZIP_PATH):
                    try:
                        os.remove(REPO_ZIP_PATH)
                        logger.info("Removed partial download: %s", REPO_ZIP_PATH)
                    except Exception as e:
                        logger.error("Failed to remove partial download: %s", e)
                return False
    return True
