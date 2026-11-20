"""SQLite-Datenbank: Matches, Runden, Events, Beobachtungen, Comms, Reviews, Profile."""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS matches (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  started REAL, ended REAL, mode TEXT, map TEXT, opponent TEXT,
  score_ours INTEGER, score_enemy INTEGER, won INTEGER, plan TEXT
);
CREATE TABLE IF NOT EXISTS rounds (
  id INTEGER PRIMARY KEY AUTOINCREMENT, match_id INTEGER, round_no INTEGER,
  won INTEGER, ts REAL, review TEXT
);
CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY AUTOINCREMENT, match_id INTEGER, ts REAL, kind TEXT, data TEXT
);
CREATE TABLE IF NOT EXISTS observations (
  id INTEGER PRIMARY KEY AUTOINCREMENT, match_id INTEGER, ts REAL, round_no INTEGER,
  text TEXT, enemy_tendency TEXT, feedback TEXT
);
CREATE TABLE IF NOT EXISTS comms (
  id INTEGER PRIMARY KEY AUTOINCREMENT, match_id INTEGER, ts REAL, source TEXT, lang TEXT, text TEXT
);
CREATE TABLE IF NOT EXISTS reviews (
  id INTEGER PRIMARY KEY AUTOINCREMENT, match_id INTEGER, ts REAL, kind TEXT, data TEXT
);
CREATE TABLE IF NOT EXISTS profiles (
  kind TEXT, name TEXT, data TEXT, updated REAL, PRIMARY KEY (kind, name)
);
CREATE TABLE IF NOT EXISTS memories (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, text TEXT, source TEXT
);
CREATE TABLE IF NOT EXISTS chat_sessions (
  id INTEGER PRIMARY KEY AUTOINCREMENT, created REAL, title TEXT
);
CREATE TABLE IF NOT EXISTS chat_messages (
  id INTEGER PRIMARY KEY AUTOINCREMENT, session_id INTEGER, ts REAL, role TEXT, text TEXT, lang TEXT
);
CREATE TABLE IF NOT EXISTS clips (
  id INTEGER PRIMARY KEY AUTOINCREMENT, match_id INTEGER, ts REAL, round_no INTEGER, note TEXT, image TEXT
);
CREATE TABLE IF NOT EXISTS ratings (
  id INTEGER PRIMARY KEY AUTOINCREMENT, match_id INTEGER, ts REAL, player TEXT, rating REAL, mvp INTEGER
);
CREATE TABLE IF NOT EXISTS notes (
  id INTEGER PRIMARY KEY AUTOINCREMENT, match_id INTEGER, ts REAL, author TEXT, text TEXT
);
"""


class DB:
    def __init__(self, path: str | Path) -> None:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(path), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def _exec(self, sql: str, args: tuple = ()) -> sqlite3.Cursor:
        cur = self.conn.execute(sql, args)
        self.conn.commit()
        return cur

    # --- Matches -----------------------------------------------------------
    def start_match(self, mode: str, map_name: str, opponent: str) -> int:
        return self._exec(
            "INSERT INTO matches (started, mode, map, opponent, score_ours, score_enemy) VALUES (?,?,?,?,0,0)",
            (time.time(), mode, map_name, opponent),
        ).lastrowid

    def end_match(self, match_id: int, ours: int, enemy: int, won: bool | None = None) -> None:
        if won is None:
            won = ours > enemy
        self._exec(
            "UPDATE matches SET ended=?, score_ours=?, score_enemy=?, won=? WHERE id=?",
            (time.time(), ours, enemy, int(won), match_id),
        )

    def set_plan(self, match_id: int, plan: str) -> None:
        self._exec("UPDATE matches SET plan=? WHERE id=?", (plan, match_id))

    def match(self, match_id: int) -> dict | None:
        row = self.conn.execute("SELECT * FROM matches WHERE id=?", (match_id,)).fetchone()
        return dict(row) if row else None

    def last_match_id(self) -> int | None:
        row = self.conn.execute("SELECT id FROM matches ORDER BY id DESC LIMIT 1").fetchone()
        return row["id"] if row else None

    # --- Logging -----------------------------------------------------------
    def add_round(self, match_id: int, round_no: int, won: bool, review: Any = None) -> int:
        return self._exec(
            "INSERT INTO rounds (match_id, round_no, won, ts, review) VALUES (?,?,?,?,?)",
            (match_id, round_no, int(won), time.time(), json.dumps(review, ensure_ascii=False) if review else None),
        ).lastrowid

    def set_round_review(self, round_id: int, review: Any) -> None:
        self._exec("UPDATE rounds SET review=? WHERE id=?", (json.dumps(review, ensure_ascii=False), round_id))

    def add_event(self, match_id: int | None, kind: str, data: dict) -> None:
        self._exec("INSERT INTO events (match_id, ts, kind, data) VALUES (?,?,?,?)",
                   (match_id, time.time(), kind, json.dumps(data, ensure_ascii=False)))

    def add_observation(self, match_id: int | None, round_no: int, text: str, tendency: str, feedback: list) -> None:
        self._exec(
            "INSERT INTO observations (match_id, ts, round_no, text, enemy_tendency, feedback) VALUES (?,?,?,?,?,?)",
            (match_id, time.time(), round_no, text, tendency, json.dumps(feedback, ensure_ascii=False)),
        )

    def add_comms(self, match_id: int | None, source: str, lang: str, text: str) -> None:
        self._exec("INSERT INTO comms (match_id, ts, source, lang, text) VALUES (?,?,?,?,?)",
                   (match_id, time.time(), source, lang, text))

    def add_review(self, match_id: int | None, kind: str, data: Any) -> None:
        self._exec("INSERT INTO reviews (match_id, ts, kind, data) VALUES (?,?,?,?)",
                   (match_id, time.time(), kind, json.dumps(data, ensure_ascii=False)))

    def add_note(self, match_id: int | None, author: str, text: str) -> None:
        self._exec("INSERT INTO notes (match_id, ts, author, text) VALUES (?,?,?,?)", (match_id, time.time(), author, text))

    # --- Abfragen ----------------------------------------------------------
    def _rows(self, sql: str, args: tuple = ()) -> list[dict]:
        return [dict(r) for r in self.conn.execute(sql, args).fetchall()]

    def events(self, match_id: int, since: float = 0) -> list[dict]:
        return self._rows("SELECT * FROM events WHERE match_id=? AND ts>=? ORDER BY ts", (match_id, since))

    def observations(self, match_id: int, round_no: int | None = None) -> list[dict]:
        if round_no is None:
            return self._rows("SELECT * FROM observations WHERE match_id=? ORDER BY ts", (match_id,))
        return self._rows("SELECT * FROM observations WHERE match_id=? AND round_no=? ORDER BY ts", (match_id, round_no))

    def comms(self, match_id: int, since: float = 0) -> list[dict]:
        return self._rows("SELECT * FROM comms WHERE match_id=? AND ts>=? ORDER BY ts", (match_id, since))

    def rounds(self, match_id: int) -> list[dict]:
        return self._rows("SELECT * FROM rounds WHERE match_id=? ORDER BY round_no", (match_id,))

    def notes(self, match_id: int) -> list[dict]:
        return self._rows("SELECT * FROM notes WHERE match_id=? ORDER BY ts", (match_id,))

    def matches_vs(self, opponent: str, limit: int = 10) -> list[dict]:
        return self._rows("SELECT * FROM matches WHERE lower(opponent)=lower(?) AND ended IS NOT NULL ORDER BY id DESC LIMIT ?",
                          (opponent, limit))

    def stats(self) -> dict:
        def agg(group: str) -> list[dict]:
            return self._rows(
                f"SELECT {group} AS key, COUNT(*) AS played, SUM(won) AS wins, "
                f"ROUND(AVG(score_ours),1) AS avg_ours, ROUND(AVG(score_enemy),1) AS avg_enemy "
                f"FROM matches WHERE ended IS NOT NULL GROUP BY {group} ORDER BY played DESC"
            )

        total = self._rows("SELECT COUNT(*) AS played, COALESCE(SUM(won),0) AS wins FROM matches WHERE ended IS NOT NULL")[0]
        return {"total": total, "by_mode": agg("mode"), "by_map": agg("map"), "by_opponent": agg("opponent")}

    # --- Profile (Lernen) --------------------------------------------------
    def get_profile(self, kind: str, name: str) -> dict:
        row = self.conn.execute("SELECT data FROM profiles WHERE kind=? AND lower(name)=lower(?)", (kind, name)).fetchone()
        return json.loads(row["data"]) if row else {}

    def set_profile(self, kind: str, name: str, data: dict) -> None:
        self._exec(
            "INSERT INTO profiles (kind, name, data, updated) VALUES (?,?,?,?) "
            "ON CONFLICT(kind, name) DO UPDATE SET data=excluded.data, updated=excluded.updated",
            (kind, name, json.dumps(data, ensure_ascii=False), time.time()),
        )

    def profiles(self, kind: str) -> dict[str, dict]:
        return {r["name"]: json.loads(r["data"]) for r in self._rows("SELECT name, data FROM profiles WHERE kind=?", (kind,))}

    # --- Langzeitgedächtnis ------------------------------------------------
    def add_memory(self, text: str, source: str = "chat") -> int:
        text = text.strip()
        dup = self.conn.execute("SELECT id FROM memories WHERE lower(text)=lower(?)", (text,)).fetchone()
        if dup:
            return dup["id"]
        return self._exec("INSERT INTO memories (ts, text, source) VALUES (?,?,?)", (time.time(), text, source)).lastrowid

    def memories(self, limit: int = 500) -> list[dict]:
        return self._rows("SELECT * FROM memories ORDER BY id DESC LIMIT ?", (limit,))

    def delete_memory(self, mem_id: int) -> None:
        self._exec("DELETE FROM memories WHERE id=?", (mem_id,))

    def relevant_memories(self, query: str, limit: int = 25) -> list[str]:
        """Neueste Erinnerungen + die, die Wörter der Anfrage enthalten."""
        mems = self.memories()
        words = {w for w in query.lower().split() if len(w) > 3}
        scored = sorted(mems, key=lambda m: (-sum(w in m["text"].lower() for w in words), -m["id"]))
        return [m["text"] for m in scored[:limit]]

    # --- Chatverläufe ------------------------------------------------------
    def new_session(self, title: str = "Neuer Chat") -> int:
        return self._exec("INSERT INTO chat_sessions (created, title) VALUES (?,?)", (time.time(), title)).lastrowid

    def sessions(self) -> list[dict]:
        return self._rows("SELECT * FROM chat_sessions ORDER BY id DESC")

    def rename_session(self, session_id: int, title: str) -> None:
        self._exec("UPDATE chat_sessions SET title=? WHERE id=?", (title, session_id))

    def delete_session(self, session_id: int) -> None:
        self._exec("DELETE FROM chat_messages WHERE session_id=?", (session_id,))
        self._exec("DELETE FROM chat_sessions WHERE id=?", (session_id,))

    def add_chat(self, session_id: int, role: str, text: str, lang: str = "") -> None:
        self._exec("INSERT INTO chat_messages (session_id, ts, role, text, lang) VALUES (?,?,?,?,?)",
                   (session_id, time.time(), role, text, lang))

    def chat(self, session_id: int, limit: int = 200) -> list[dict]:
        rows = self._rows("SELECT * FROM chat_messages WHERE session_id=? ORDER BY id DESC LIMIT ?", (session_id, limit))
        return rows[::-1]

    # --- Clips & Ratings ---------------------------------------------------
    def add_clip(self, match_id: int | None, round_no: int, note: str, image: str) -> int:
        return self._exec("INSERT INTO clips (match_id, ts, round_no, note, image) VALUES (?,?,?,?,?)",
                          (match_id, time.time(), round_no, note, image)).lastrowid

    def clips(self, match_id: int | None = None, limit: int = 100) -> list[dict]:
        if match_id is None:
            return self._rows("SELECT * FROM clips ORDER BY id DESC LIMIT ?", (limit,))
        return self._rows("SELECT * FROM clips WHERE match_id=? ORDER BY ts", (match_id,))

    def add_rating(self, match_id: int, player: str, rating: float, mvp: bool) -> None:
        self._exec("INSERT INTO ratings (match_id, ts, player, rating, mvp) VALUES (?,?,?,?,?)",
                   (match_id, time.time(), player, rating, int(mvp)))

    def player_ratings(self) -> list[dict]:
        return self._rows("SELECT player, COUNT(*) AS matches, ROUND(AVG(rating),1) AS avg_rating, SUM(mvp) AS mvps, "
                          "ROUND(AVG(CASE WHEN id IN (SELECT id FROM ratings r2 WHERE r2.player=ratings.player ORDER BY id DESC LIMIT 5) "
                          "THEN rating END),1) AS last5 FROM ratings GROUP BY player ORDER BY avg_rating DESC")
