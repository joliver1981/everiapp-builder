import os
import sys
import tempfile
from pathlib import Path

import pytest

# Make `src` importable as `src.…`
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# Use a temp database + temp app data dir for tests so we never touch the real one.
_TMP = Path(tempfile.gettempdir()) / "aihub-tests"
_TMP.mkdir(parents=True, exist_ok=True)
os.environ.setdefault("DATABASE_URL", f"sqlite+aiosqlite:///{_TMP / 'test.db'}")
os.environ.setdefault("APP_DATA_DIR", str(_TMP / "apps"))
os.environ.setdefault("DEBUG", "true")
# Valid 32-byte url-safe-base64 Fernet key (only used for tests)
os.environ.setdefault(
    "MASTER_ENCRYPTION_KEY", "Zm9vYmFyZm9vYmFyZm9vYmFyZm9vYmFyZm9vYmFyZm8="
)


@pytest.fixture
def tmp_app_dir(tmp_path):
    return tmp_path
