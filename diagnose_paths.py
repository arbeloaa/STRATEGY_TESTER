import sys
import platform
from pathlib import Path

# Add project root to sys.path to enable loading the config package
_PROJECT_ROOT = Path(__file__).resolve().parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from config.paths import (
    PROJECT_ROOT,
    DATA_DIR,
    CONFIG_DIR,
    REPORTS_DIR,
    LOGS_DIR,
    FEATURE_CACHE_DB,
    MARKET_DATA_DB
)

def run_diagnostics():
    print("=" * 60)
    print("PATH DIAGNOSTICS")
    print("=" * 60)
    print(f"Operating System         : {platform.system()} ({platform.release()})")
    print(f"Project Root             : {PROJECT_ROOT}")
    print(f"Data Directory           : {DATA_DIR}")
    print(f"Config Directory         : {CONFIG_DIR}")
    print(f"Reports Directory        : {REPORTS_DIR}")
    print(f"Logs Directory           : {LOGS_DIR}")
    print("-" * 60)
    
    # Feature cache database
    print(f"Feature Cache DB Path    : {FEATURE_CACHE_DB}")
    print(f"  - Exists               : {FEATURE_CACHE_DB.exists()}")
    print(f"  - Is Symbolic Link     : {FEATURE_CACHE_DB.is_symlink()}")
    if FEATURE_CACHE_DB.is_symlink():
        try:
            print(f"  - Target Resolved      : {FEATURE_CACHE_DB.resolve()}")
        except Exception as e:
            print(f"  - Target Resolved (Err): {e}")
            
    # Market data database
    print(f"Market Data DB Path      : {MARKET_DATA_DB}")
    print(f"  - Exists               : {MARKET_DATA_DB.exists()}")
    print(f"  - Is Symbolic Link     : {MARKET_DATA_DB.is_symlink()}")
    if MARKET_DATA_DB.is_symlink():
        try:
            print(f"  - Target Resolved      : {MARKET_DATA_DB.resolve()}")
        except Exception as e:
            print(f"  - Target Resolved (Err): {e}")
    print("=" * 60)

if __name__ == "__main__":
    run_diagnostics()
