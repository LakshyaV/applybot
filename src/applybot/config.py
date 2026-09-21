from __future__ import annotations

from functools import lru_cache
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = ROOT / "data"
DB_PATH = DATA_DIR / "jobs.db"
PROFILE_PATH = ROOT / "profile" / "profile.md"


@lru_cache
def load_config(path: Path | None = None) -> dict:
    with open(path or ROOT / "config.yaml") as f:
        return yaml.safe_load(f)


def load_profile(path: Path | None = None) -> dict:
    """YAML front-matter of profile.md (the machine-readable facts)."""
    text = (path or PROFILE_PATH).read_text()
    if not text.startswith("---"):
        raise ValueError("profile.md must start with YAML front-matter")
    _, front, _ = text.split("---", 2)
    return yaml.safe_load(front)
