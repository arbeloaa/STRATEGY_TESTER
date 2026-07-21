import os
from pathlib import Path

# Define the project root relative to this file's location
PROJECT_ROOT = Path(__file__).resolve().parent.parent

# Support dynamic data directory override via environment variable
data_dir_env = os.getenv("STRATEGY_DATA_DIR")
DATA_DIR = (
    Path(data_dir_env).expanduser()
    if data_dir_env
    else PROJECT_ROOT / "data"
)

# Shared centralized directories
CONFIG_DIR = PROJECT_ROOT / "config"
REPORTS_DIR = PROJECT_ROOT / "reports"
LOGS_DIR = PROJECT_ROOT / "logs"

# Database path constants
FEATURE_CACHE_DB = DATA_DIR / "feature_cache.db"
MARKET_DATA_DB = DATA_DIR / "market_data.db"

# Ensure runtime directories exist
DATA_DIR.mkdir(parents=True, exist_ok=True)
CONFIG_DIR.mkdir(parents=True, exist_ok=True)
REPORTS_DIR.mkdir(parents=True, exist_ok=True)
LOGS_DIR.mkdir(parents=True, exist_ok=True)

# ---------------------------------------------------------------------------
# Cloud-sync guard
# ---------------------------------------------------------------------------
# The live SQLite caches (feature_cache.db, market_data.db) must be written by
# ONE authoritative local copy. A cloud-sync client (Google Drive, Dropbox,
# iCloud) reading/uploading the file mid-write can produce corrupted or
# "conflicted copy" forks -- this has already happened once (see the
# feature_cache N.db .shm/.wal fragments in this Mac's Google Drive Trash).
# Google Drive's own folder-backup ("My Drive"/CloudStorage) is fine as an
# idle-time backup target -- it must never be the path the code reads/writes.
_CLOUD_SYNC_MARKERS = ("CloudStorage", "Google Drive", "Dropbox", "iCloud Drive", "OneDrive")


def _assert_not_cloud_synced(path: Path, label: str) -> None:
    resolved = str(path.resolve())
    for marker in _CLOUD_SYNC_MARKERS:
        if marker in resolved:
            raise RuntimeError(
                f"{label} resolves to a cloud-sync path ({resolved}). "
                f"The authoritative DB must live outside any cloud-sync folder "
                f"(e.g. unset STRATEGY_DATA_DIR, or move the project out of "
                f"Google Drive/Dropbox/iCloud). Use cloud storage only as an "
                f"idle-time backup copy, never as the live read/write path."
            )


_assert_not_cloud_synced(FEATURE_CACHE_DB, "FEATURE_CACHE_DB")
_assert_not_cloud_synced(MARKET_DATA_DB, "MARKET_DATA_DB")
