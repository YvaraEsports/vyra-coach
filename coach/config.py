"""Konfiguration laden: config.example.yaml (Defaults) + config.yaml (eigene Werte) + .env."""
from __future__ import annotations

import copy
import os
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
REPORTS_DIR = ROOT / "reports"


def deep_merge(base: dict, override: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = v
    return out


class Config(dict):
    """dict mit Punkt-Zugriff auf Pfade: cfg.get_path("llm.model")."""

    def get_path(self, path: str, default: Any = None) -> Any:
        node: Any = self
        for part in path.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node

    def set_path(self, path: str, value: Any) -> None:
        parts = path.split(".")
        node = self
        for part in parts[:-1]:
            node = node.setdefault(part, {})
        node[parts[-1]] = value


def load_config(path: str | Path | None = None) -> Config:
    try:
        from dotenv import load_dotenv

        load_dotenv(ROOT / ".env")
    except ImportError:  # pragma: no cover
        pass
    defaults = yaml.safe_load((ROOT / "config.example.yaml").read_text(encoding="utf-8")) or {}
    user_path = Path(path) if path else ROOT / "config.yaml"
    user = {}
    if user_path.exists():
        user = yaml.safe_load(user_path.read_text(encoding="utf-8")) or {}
    cfg = Config(deep_merge(defaults, user))
    cfg["_path"] = str(user_path)
    cfg["discord_token"] = os.environ.get("DISCORD_TOKEN", "")
    return cfg


def save_user_value(cfg: Config, dotted: str, value: Any) -> None:
    """Einen Wert dauerhaft in config.yaml schreiben (z. B. nach Kalibrierung)."""
    p = Path(cfg.get("_path") or ROOT / "config.yaml")
    user = yaml.safe_load(p.read_text(encoding="utf-8")) if p.exists() else {}
    user = Config(user or {})
    user.set_path(dotted, value)
    p.write_text(yaml.safe_dump(dict(user), allow_unicode=True, sort_keys=False), encoding="utf-8")
    cfg.set_path(dotted, value)
