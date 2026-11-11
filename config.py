"""Local configuration. Secrets live in `.env` (never committed) or environment variables."""
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent
ENV_FILE = ROOT / ".env"


def load_env(path=ENV_FILE):
    """Minimal KEY=VALUE loader; real environment variables take priority."""
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip().strip('"').strip("'")
        if key and value and not os.environ.get(key):
            os.environ[key] = value


def turso_settings():
    """Return (url, token) when Turso is configured, otherwise None (local SQLite)."""
    url = os.environ.get("TURSO_DATABASE_URL", "").strip()
    token = os.environ.get("TURSO_AUTH_TOKEN", "").strip()
    if not url:
        return None
    if not token:
        raise RuntimeError("TURSO_DATABASE_URL is set but TURSO_AUTH_TOKEN is missing. Add it to .env.")
    return url, token
