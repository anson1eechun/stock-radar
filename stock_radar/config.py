import os
from functools import lru_cache
from pathlib import Path

import yaml
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent


@lru_cache(maxsize=1)
def cfg() -> dict:
    load_dotenv(ROOT / ".env")
    path = Path(os.environ.get("STOCK_RADAR_CONFIG", ROOT / "config.yaml"))
    with open(path, encoding="utf-8") as f:
        data = yaml.safe_load(f)
    db_path = Path(os.environ.get("STOCK_RADAR_DB", data["db_path"]))
    data["db_path"] = str(db_path if db_path.is_absolute() else ROOT / db_path)
    return data


def secret(name: str) -> str | None:
    cfg()
    return os.environ.get(name) or None
