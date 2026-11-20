"""Taktik-Bibliothek: Wissen, das Yvara AI aus Pro-Matches, Streams, VODs, Highlights
und euren Scrims lernt. Teamübergreifend (data/library/library.db), weil Pro-Wissen
für alle Teams gilt. Aus vielen Einzel-Lektionen entstehen pro Modus "Playbooks".
"""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

from ..config import DATA_DIR

LIB_PATH = DATA_DIR / "library" / "library.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS sources (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, kind TEXT, ref TEXT, title TEXT, status TEXT,
  progress REAL DEFAULT 0, lessons INTEGER DEFAULT 0, note TEXT
);
CREATE TABLE IF NOT EXISTS lessons (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, source_id INTEGER, t_video REAL,
  mode TEXT, map TEXT, team TEXT, situation TEXT, action TEXT, why TEXT, lesson TEXT, tags TEXT, confidence REAL
);
CREATE TABLE IF NOT EXISTS queue (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, url TEXT UNIQUE, title TEXT, origin TEXT,
  status TEXT DEFAULT 'queued', duration REAL
);
CREATE TABLE IF NOT EXISTS ideas (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, team TEXT, mode TEXT, map TEXT, name TEXT, data TEXT
);
CREATE TABLE IF NOT EXISTS callouts (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, map TEXT, name TEXT, zone TEXT, aliases TEXT, note TEXT,
  source TEXT, hits INTEGER DEFAULT 1, UNIQUE(map, name)
);
CREATE TABLE IF NOT EXISTS playbooks (
  mode TEXT PRIMARY KEY, ts REAL, data TEXT, lessons_used INTEGER
);
"""


class Library:
    def __init__(self, path: Path = LIB_PATH) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(path), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self._migrate()
        self.conn.commit()

    def _migrate(self) -> None:
        """Neue Spalten in bestehende Datenbanken einfügen – alte Daten bleiben erhalten."""
        have = {r[1] for r in self.conn.execute("PRAGMA table_info(sources)").fetchall()}
        for col, decl in (("duration", "REAL DEFAULT 0"), ("watched_s", "REAL DEFAULT 0"), ("ended", "REAL")):
            if col not in have:
                self.conn.execute(f"ALTER TABLE sources ADD COLUMN {col} {decl}")

    def _exec(self, sql: str, args: tuple = ()):
        cur = self.conn.execute(sql, args)
        self.conn.commit()
        return cur

    def _rows(self, sql: str, args: tuple = ()) -> list[dict]:
        return [dict(r) for r in self.conn.execute(sql, args).fetchall()]

    # ------------------------------------------------------------ sources
    def add_source(self, kind: str, ref: str, title: str, note: str = "") -> int:
        return self._exec("INSERT INTO sources (ts, kind, ref, title, status, note) VALUES (?,?,?,?,?,?)",
                          (time.time(), kind, ref, title, "running", note)).lastrowid

    def update_source(self, sid: int, **fields) -> None:
        keys = ", ".join(f"{k}=?" for k in fields)
        self._exec(f"UPDATE sources SET {keys} WHERE id=?", (*fields.values(), sid))

    def sources(self, limit: int = 50) -> list[dict]:
        return self._rows("SELECT * FROM sources ORDER BY id DESC LIMIT ?", (limit,))

    def add_watched(self, sid: int, seconds: float) -> None:
        self._exec("UPDATE sources SET watched_s = COALESCE(watched_s, 0) + ? WHERE id=?", (float(seconds), sid))

    def totals(self) -> dict:
        q = lambda sql: self.conn.execute(sql).fetchone()[0] or 0  # noqa: E731
        return {
            "sources": q("SELECT COUNT(*) FROM sources"),
            "done": q("SELECT COUNT(*) FROM sources WHERE status='done'"),
            "watched_s": q("SELECT SUM(watched_s) FROM sources"),
            "lessons": q("SELECT COUNT(*) FROM lessons"),
            "ideas": q("SELECT COUNT(*) FROM ideas"),
            "playbooks": q("SELECT COUNT(*) FROM playbooks"),
            "modes": {r[0] or "?": r[1] for r in self.conn.execute(
                "SELECT mode, COUNT(*) FROM lessons GROUP BY mode ORDER BY 2 DESC").fetchall()},
        }

    def history(self, limit: int = 50, per_source: int = 8) -> list[dict]:
        """Lernverlauf: jede geschaute Quelle mit Dauer, geschauter Zeit und ihren Lektionen."""
        out = self.sources(limit)
        for src in out:
            src["top"] = self._rows(
                "SELECT id, mode, map, lesson, confidence FROM lessons WHERE source_id=? "
                "ORDER BY confidence DESC, id DESC LIMIT ?", (src["id"], per_source))
        return out

    # ------------------------------------------------------------ lessons
    def add_lessons(self, source_id: int | None, t_video: float, items: list[dict]) -> int:
        n = 0
        for it in items:
            if not it.get("lesson"):
                continue
            self._exec(
                "INSERT INTO lessons (ts, source_id, t_video, mode, map, team, situation, action, why, lesson, tags, confidence) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (time.time(), source_id, t_video, (it.get("mode") or "").lower(), it.get("map", ""), it.get("team", ""),
                 it.get("situation", ""), it.get("action", ""), it.get("why", ""), it["lesson"],
                 json.dumps(it.get("tags", []), ensure_ascii=False), float(it.get("confidence", 0.5))),
            )
            n += 1
        if source_id and n:
            self._exec("UPDATE sources SET lessons = lessons + ? WHERE id=?", (n, source_id))
        return n

    def lessons(self, mode: str | None = None, limit: int = 300) -> list[dict]:
        if mode:
            return self._rows("SELECT * FROM lessons WHERE mode=? ORDER BY confidence DESC, id DESC LIMIT ?", (mode, limit))
        return self._rows("SELECT * FROM lessons ORDER BY id DESC LIMIT ?", (limit,))

    def count(self, mode: str | None = None) -> int:
        if mode:
            return self.conn.execute("SELECT COUNT(*) FROM lessons WHERE mode=?", (mode,)).fetchone()[0]
        return self.conn.execute("SELECT COUNT(*) FROM lessons").fetchone()[0]

    def delete_lesson(self, lid: int) -> None:
        self._exec("DELETE FROM lessons WHERE id=?", (lid,))

    def relevant(self, mode: str, map_name: str = "", query: str = "", limit: int = 12) -> list[dict]:
        rows = self.lessons(mode, limit=500)
        words = {w for w in f"{map_name} {query}".lower().split() if len(w) > 3}

        def score(r):
            text = f"{r['map']} {r['situation']} {r['lesson']} {r['tags']}".lower()
            return (sum(w in text for w in words) + (2 if map_name and map_name.lower() in r["map"].lower() else 0),
                    r["confidence"])

        return sorted(rows, key=score, reverse=True)[:limit]

    # ----------------------------------------------------------- playbooks
    def set_playbook(self, mode: str, data: dict, used: int) -> None:
        self._exec("INSERT INTO playbooks (mode, ts, data, lessons_used) VALUES (?,?,?,?) "
                   "ON CONFLICT(mode) DO UPDATE SET ts=excluded.ts, data=excluded.data, lessons_used=excluded.lessons_used",
                   (mode, time.time(), json.dumps(data, ensure_ascii=False), used))

    def playbook(self, mode: str) -> dict:
        row = self.conn.execute("SELECT data FROM playbooks WHERE mode=?", (mode,)).fetchone()
        return json.loads(row["data"]) if row else {}

    def playbooks(self) -> dict[str, dict]:
        return {r["mode"]: json.loads(r["data"]) for r in self._rows("SELECT mode, data FROM playbooks")}

    def context(self, mode: str, map_name: str = "", query: str = "") -> str:
        """Kompakter Wissensblock für Prompts (Playbook + passende Lektionen)."""
        out = []
        pb = self.playbook(mode)
        if pb:
            out.append("LEARNED PLAYBOOK (from pro matches / VODs / scrims):")
            for k in ("principles", "setups", "rotations", "mistakes_to_avoid", "meta_notes"):
                for x in pb.get(k, [])[:8]:
                    out.append(f"- [{k}] {x}")
        rel = self.relevant(mode, map_name, query)
        if rel:
            out.append("LEARNED LESSONS:")
            out += [f"- ({r['team'] or 'pro'}{', ' + r['map'] if r['map'] else ''}) {r['lesson']}" for r in rel]
        return "\n".join(out)

    # ------------------------------------------------------- lern-queue
    def enqueue(self, url: str, title: str = "", origin: str = "manual", duration: float | None = None) -> bool:
        if self.conn.execute("SELECT 1 FROM sources WHERE ref=? AND status='done'", (url,)).fetchone():
            return False
        try:
            self._exec("INSERT INTO queue (ts, url, title, origin, duration) VALUES (?,?,?,?,?)",
                       (time.time(), url, title, origin, duration))
            return True
        except sqlite3.IntegrityError:
            return False

    def next_queued(self) -> dict | None:
        # Manuell geschickte Links zuerst
        row = self.conn.execute("SELECT * FROM queue WHERE status='queued' ORDER BY origin!='manual', id LIMIT 1").fetchone()
        return dict(row) if row else None

    def set_queue_status(self, qid: int, status: str) -> None:
        self._exec("UPDATE queue SET status=? WHERE id=?", (status, qid))

    def queue(self, limit: int = 50) -> list[dict]:
        return self._rows("SELECT * FROM queue ORDER BY status!='queued', id DESC LIMIT ?", (limit,))

    def known_urls(self) -> set[str]:
        return {r["url"] for r in self._rows("SELECT url FROM queue")} | {r["ref"] for r in self._rows("SELECT ref FROM sources")}

    def mode_counts(self) -> dict[str, int]:
        return {r["mode"] or "?": r["n"] for r in self._rows("SELECT mode, COUNT(*) AS n FROM lessons GROUP BY mode")}

    # ----------------------------------------------------- eigene ideen
    def add_idea(self, team: str, idea: dict) -> None:
        self._exec("INSERT INTO ideas (ts, team, mode, map, name, data) VALUES (?,?,?,?,?,?)",
                   (time.time(), team, idea.get("mode", ""), idea.get("map", ""), idea.get("name", ""),
                    json.dumps(idea, ensure_ascii=False)))

    def ideas(self, team: str | None = None, limit: int = 50) -> list[dict]:
        rows = (self._rows("SELECT * FROM ideas WHERE team=? ORDER BY id DESC LIMIT ?", (team, limit)) if team
                else self._rows("SELECT * FROM ideas ORDER BY id DESC LIMIT ?", (limit,)))
        for r in rows:
            r["data"] = json.loads(r["data"])
        return rows

    # ------------------------------------------------------------ callouts
    @staticmethod
    def _map_key(map_name: str | None) -> str:
        return (map_name or "").strip().lower()

    def add_callout(self, map_name: str, name: str, zone: str = "", aliases: list[str] | None = None,
                    note: str = "", source: str = "manual") -> None:
        """Callout speichern. Gibt es ihn schon, werden Zone/Aliase ergänzt (manuelle Einträge haben Vorrang)."""
        name = (name or "").strip()
        if not name:
            return
        key = self._map_key(map_name)
        zone = (zone or "").strip().upper()[:6]
        aliases = [a.strip() for a in (aliases or []) if a and a.strip() and a.strip().lower() != name.lower()]
        row = self.conn.execute("SELECT * FROM callouts WHERE map=? AND lower(name)=lower(?)", (key, name)).fetchone()
        if not row:
            self._exec("INSERT INTO callouts (ts, map, name, zone, aliases, note, source) VALUES (?,?,?,?,?,?,?)",
                       (time.time(), key, name, zone, json.dumps(aliases, ensure_ascii=False), note, source))
            return
        old_alias = json.loads(row["aliases"] or "[]")
        merged = old_alias + [a for a in aliases if a.lower() not in {x.lower() for x in old_alias}]
        keep_manual = row["source"] == "manual" and source != "manual"
        new_zone = row["zone"] if (keep_manual and row["zone"]) or not zone else zone
        self._exec("UPDATE callouts SET ts=?, zone=?, aliases=?, note=?, hits=hits+1, source=? WHERE id=?",
                   (time.time(), new_zone, json.dumps(merged[:10], ensure_ascii=False),
                    row["note"] if keep_manual and row["note"] else (note or row["note"]),
                    row["source"] if keep_manual else source, row["id"]))

    def callouts(self, map_name: str | None = None) -> list[dict]:
        if map_name is None:
            rows = self._rows("SELECT * FROM callouts ORDER BY map, name")
        else:
            rows = self._rows("SELECT * FROM callouts WHERE map=? ORDER BY hits DESC, name", (self._map_key(map_name),))
        for r in rows:
            r["aliases"] = json.loads(r["aliases"] or "[]")
        return rows

    def callout_maps(self) -> list[str]:
        return [r["map"] for r in self._rows("SELECT DISTINCT map FROM callouts ORDER BY map")]

    def delete_callout(self, cid: int) -> None:
        self._exec("DELETE FROM callouts WHERE id=?", (cid,))
