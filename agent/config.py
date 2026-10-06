import os
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = ROOT / "config"
EXAMPLE_DIR = CONFIG_DIR / "examples"
DATA_DIR = ROOT / "data"
STOP_FILE = ROOT / "STOP"


def load_yaml(path: Path) -> Any:
    if not path.exists():
        raise SystemExit(f"{path} is missing. Copy config/examples/{path.name} to {path} and fill it in.")
    with open(path, encoding="utf-8") as handle:
        return yaml.safe_load(handle)


def load_settings(config_dir: Path = CONFIG_DIR) -> dict:
    return load_yaml(config_dir / "settings.yaml")


def load_companies(config_dir: Path = CONFIG_DIR) -> list[dict]:
    return load_yaml(config_dir / "companies.yaml")


def load_env(path: Path = ROOT / ".env") -> None:
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip("'\""))
