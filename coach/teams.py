"""Team-Projekte: jedes Team (z. B. "Yvara Main", "Yvara Z8") hat eigenes Gedächtnis,
eigene Chats, Profile, Stats und Reports. Alles lokal unter data/teams/<team>/.
"""
from __future__ import annotations

import re
from pathlib import Path

import yaml

from .config import DATA_DIR
from .stats.db import DB

TEAMS_DIR = DATA_DIR / "teams"
ACTIVE_FILE = DATA_DIR / "active_team.txt"


def slugify(name: str) -> str:
    s = re.sub(r"[^\w]+", "-", name.strip().lower()).strip("-")
    return s or "team"


class Team:
    def __init__(self, folder: Path) -> None:
        self.dir = folder
        self.meta_path = folder / "team.yaml"
        self.meta: dict = yaml.safe_load(self.meta_path.read_text(encoding="utf-8")) if self.meta_path.exists() else {}
        self.db = DB(folder / "coach.db")
        self.reports = folder / "reports"

    @property
    def slug(self) -> str:
        return self.dir.name

    @property
    def name(self) -> str:
        return self.meta.get("name", self.slug)

    @property
    def roster(self) -> list[dict]:
        return self.meta.get("players", []) or []

    @property
    def notes(self) -> str:
        return self.meta.get("notes", "") or ""

    def save(self) -> None:
        self.meta_path.write_text(yaml.safe_dump(self.meta, allow_unicode=True, sort_keys=False), encoding="utf-8")

    def info(self) -> dict:
        return {"slug": self.slug, "name": self.name, "players": self.roster, "notes": self.notes}


class TeamManager:
    def __init__(self, cfg, root: Path = TEAMS_DIR) -> None:
        self.cfg = cfg
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)
        self._cache: dict[str, Team] = {}
        if not self.list():
            # Erstes Team aus config.yaml anlegen
            self.create(cfg.get_path("team.name", "Main Team"), cfg.get_path("team.players", []))
        active = ACTIVE_FILE.read_text(encoding="utf-8").strip() if ACTIVE_FILE.exists() else ""
        self.active: Team = self.get(active) or self.get(self.list()[0]["slug"])

    def list(self) -> list[dict]:
        out = []
        for d in sorted(self.root.iterdir()):
            if d.is_dir() and (d / "team.yaml").exists():
                meta = yaml.safe_load((d / "team.yaml").read_text(encoding="utf-8")) or {}
                out.append({"slug": d.name, "name": meta.get("name", d.name)})
        return out

    def get(self, key: str | None) -> Team | None:
        if not key:
            return None
        slug = slugify(key)
        for t in self.list():
            if slug in (t["slug"], slugify(t["name"])):
                if t["slug"] not in self._cache:
                    self._cache[t["slug"]] = Team(self.root / t["slug"])
                return self._cache[t["slug"]]
        return None

    def create(self, name: str, players: list | None = None, notes: str = "") -> Team:
        folder = self.root / slugify(name)
        folder.mkdir(parents=True, exist_ok=True)
        meta_path = folder / "team.yaml"
        if not meta_path.exists():
            meta_path.write_text(yaml.safe_dump({"name": name, "players": players or [], "notes": notes},
                                                allow_unicode=True, sort_keys=False), encoding="utf-8")
        return self.get(name)

    def activate(self, key: str) -> Team | None:
        team = self.get(key)
        if team:
            self.active = team
            ACTIVE_FILE.write_text(team.slug, encoding="utf-8")
        return team
