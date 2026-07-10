"""Test environment: stub required env vars BEFORE any bot/plugin import.

bot.py and the plugin modules read env at import time (DATA_DIR, LOG_DIR,
DISCORD_TOKEN, ...), so this must run first — conftest.py is imported by
pytest before test modules are collected.
"""

import os
import sys
import tempfile
from pathlib import Path

_TMP = tempfile.mkdtemp(prefix="marvin-tests-")

os.environ.setdefault("DISCORD_TOKEN", "test-token")
os.environ.setdefault("ANTHROPIC_API_KEY", "test-key")
os.environ.setdefault("DATA_DIR", str(Path(_TMP) / "data"))
os.environ.setdefault("LOG_DIR", str(Path(_TMP) / "logs"))
os.environ.setdefault("MAIN_CHANNEL_IDS", "1111")

# Repo root on sys.path so `import bot` / `import plugins` work from tests/.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
