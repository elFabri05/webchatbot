"""Reset trial data.

  - Mock tracker DB (MOCK_DB_PATH, default data/isp.db): rebuilt from db/seed.sql,
    so the "last 24h" history and outage ETA are fresh again.
  - App DB (APP_DB_PATH, default data/app.db): conversations, messages and the
    tool-call log are emptied (tables kept), so the admin page matches the mock.

Usage: python scripts/reset_db.py [--skip-mock] [--skip-app] [--if-missing]
                                  [--db PATH] [--app-db PATH]
Paths default to the env vars / .env values above.
"""
import argparse
import sqlite3
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parent.parent
SEED = ROOT / "db" / "seed.sql"
APP_TABLES = ("tool_calls", "messages", "conversations")


class Paths(BaseSettings):
    model_config = SettingsConfigDict(env_file=ROOT / ".env", extra="ignore")
    mock_db_path: str = "data/isp.db"
    app_db_path: str = "data/app.db"


def _abs(p: str | Path) -> Path:
    p = Path(p)
    return p if p.is_absolute() else ROOT / p


def reset(db_path: Path) -> None:
    """Rebuild the mock tracker DB from the seed."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = db_path.with_suffix(".tmp")
    tmp.unlink(missing_ok=True)
    with sqlite3.connect(tmp) as conn:
        conn.executescript(SEED.read_text(encoding="utf-8"))
        counts = {
            t: conn.execute(f"SELECT count(*) FROM {t}").fetchone()[0]
            for t in ("customers", "lines", "nodes", "incidents", "signal_history")
        }
    tmp.replace(db_path)   # atomic swap: a running server never sees a half-built DB
    print(f"Reset {db_path}: " + ", ".join(f"{k}={v}" for k, v in counts.items()))


def clear_app(db_path: Path) -> None:
    """Empty conversation tables in place (the running app keeps its schema)."""
    if not db_path.exists():
        print(f"App DB {db_path} does not exist yet; nothing to clear")
        return
    with sqlite3.connect(db_path) as conn:
        have = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        for t in APP_TABLES:
            if t in have:
                conn.execute(f"DELETE FROM {t}")
        if "sqlite_sequence" in have:
            conn.execute("DELETE FROM sqlite_sequence")
    print(f"Cleared conversations in {db_path}")


if __name__ == "__main__":
    env = Paths()
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--db", default=env.mock_db_path, help="mock tracker DB path")
    ap.add_argument("--app-db", default=env.app_db_path, help="app conversations DB path")
    ap.add_argument("--skip-mock", action="store_true")
    ap.add_argument("--skip-app", action="store_true")
    ap.add_argument("--if-missing", action="store_true",
                    help="only seed the mock DB if it doesn't exist (container start)")
    a = ap.parse_args()
    if not a.skip_mock:
        mock = _abs(a.db)
        if a.if_missing and mock.exists():
            print(f"{mock} exists; keeping it (run `make reset-db` to refresh)")
        else:
            reset(mock)
    if not a.skip_app:
        clear_app(_abs(a.app_db))
