"""Config loading with dotted-key access and per-run overrides."""
from __future__ import annotations

from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:  # Python < 3.11
    import tomli as tomllib


class Config:
    def __init__(self, data: dict, base: Path):
        self.data = data
        self.base = Path(base)

    @classmethod
    def load(cls, path, overrides=()):
        path = Path(path).resolve()
        if not path.exists():
            raise SystemExit(f"Config file not found: {path}")
        with open(path, "rb") as f:
            data = tomllib.load(f)
        for item in overrides:
            _apply_override(data, item)
        return cls(data, path.parent)

    def get(self, dotted: str, default=None):
        node = self.data
        for part in dotted.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node

    def resolve(self, p) -> Path:
        p = Path(p)
        return p if p.is_absolute() else (self.base / p).resolve()


def _apply_override(data: dict, item: str):
    if "=" not in item:
        raise SystemExit(f"--set expects key=value, got: {item}")
    key, raw = item.split("=", 1)
    try:
        value = tomllib.loads(f"v = {raw}")["v"]
    except Exception:
        value = raw  # bare string
    node = data
    parts = key.strip().split(".")
    for part in parts[:-1]:
        node = node.setdefault(part, {})
    node[parts[-1]] = value
