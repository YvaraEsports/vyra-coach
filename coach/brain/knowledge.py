"""Lädt die Wissensbasis (Modi, Maps) aus data/knowledge/*.yaml."""
from __future__ import annotations

from pathlib import Path

import yaml

from ..config import DATA_DIR

KDIR = DATA_DIR / "knowledge"

MODE_ALIASES = {
    "hp": "hardpoint", "hardpoint": "hardpoint",
    "snd": "snd", "s&d": "snd", "search": "snd", "search and destroy": "snd", "suchen und zerstören": "snd",
    "control": "control", "ctrl": "control", "kontrolle": "control",
    "hijack": "hijack", "hi jack": "hijack",
}


def norm_mode(mode: str | None) -> str:
    return MODE_ALIASES.get((mode or "hardpoint").strip().lower(), (mode or "hardpoint").strip().lower())


class Knowledge:
    def __init__(self, kdir: Path = KDIR) -> None:
        self.modes = self._load(kdir / "modes.yaml")
        self.maps = self._load(kdir / "maps.yaml")

    @staticmethod
    def _load(p: Path) -> dict:
        return yaml.safe_load(p.read_text(encoding="utf-8")) or {} if p.exists() else {}

    def find_map(self, name: str | None) -> dict:
        if not name:
            return {}
        key = name.strip().lower()
        for k, v in self.maps.items():
            if key in (k.lower(), str(v.get("name", "")).lower()):
                return v
        return {}

    def context(self, mode: str, map_name: str | None = None) -> str:
        m = self.modes.get(mode, {})
        out = [f"MODE {m.get('name', mode)} (target {m.get('target')})"]
        out += [f"- principle: {p}" for p in m.get("principles", [])]
        out += [f"- common mistake: {p}" for p in m.get("common_mistakes", [])]
        mp = self.find_map(map_name)
        if mp:
            out.append(f"MAP {mp.get('name', map_name)}: callouts {', '.join(mp.get('callouts', []))}")
            mode_info = mp.get(mode, {})
            if mode_info:
                out.append(f"map/{mode} notes: {yaml.safe_dump(mode_info, allow_unicode=True)}")
        return "\n".join(out)
