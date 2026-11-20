"""Fortbildungsmodus: Yvara AI lernt selbstständig, bis du "Stopp" sagst.

Ablauf (Endlosschleife):
  1. Nächstes Video aus der Warteschlange (deine geschickten Links haben Vorrang).
  2. Leer? -> Yvara AI überlegt selbst, was ihr noch fehlt, sucht auf YouTube und in
     Twitch-VODs (z. B. Call of Duty League) und füllt die Warteschlange.
  3. Video anschauen & lernen (Lektionen -> Bibliothek -> Playbooks).
  4. Alle paar Videos: eigene Taktiken für euer Team entwickeln (nicht kopieren,
     sondern passend zu euren Spielern weiterdenken).
"""
from __future__ import annotations

import asyncio
import json
import logging
import time

from ..brain import prompts as P

log = logging.getLogger(__name__)


def _search_videos(query: str, n: int) -> list[dict]:
    import yt_dlp

    with yt_dlp.YoutubeDL({"quiet": True, "extract_flat": True, "skip_download": True}) as ydl:
        info = ydl.extract_info(f"ytsearch{n}:{query}", download=False)
    out = []
    for e in info.get("entries") or []:
        url = e.get("url") or (f"https://www.youtube.com/watch?v={e['id']}" if e.get("id") else None)
        if url:
            out.append({"url": url, "title": e.get("title") or url, "duration": e.get("duration")})
    return out


def _twitch_vods(channel: str, n: int) -> list[dict]:
    import yt_dlp

    url = f"https://www.twitch.tv/{channel}/videos?filter=archives&sort=time"
    with yt_dlp.YoutubeDL({"quiet": True, "extract_flat": True, "skip_download": True, "playlistend": n}) as ydl:
        info = ydl.extract_info(url, download=False)
    return [{"url": e.get("url"), "title": e.get("title") or e.get("url"), "duration": e.get("duration")}
            for e in (info.get("entries") or [])[:n] if e.get("url")]


OTHER_TITLES = ["black ops", "bo7", "bo6", "bo5", "bo4", "bo3", "bo2", "bo1", "cold war", "vanguard", "mw3", "mwiii",
                "mw2", "mwii", "mw2019", "mw 2019", "warzone", "ww2", "wwii", "infinite warfare", "advanced warfare",
                "ghosts", "cod4", "mw 3", "mw 2", "modern warfare 3", "modern warfare 2", "modern warfare iii",
                "modern warfare ii", "world at war", "verdansk", "rebirth island"]


def title_ok(title: str, exclude: list[str]) -> bool:
    """Nur das aktuelle Spiel lernen: Titel mit anderen CoD-Teilen aussortieren."""
    import re

    t = " " + re.sub(r"[^a-z0-9]+", " ", (title or "").lower()) + " "
    return not any(f" {re.sub(r'[^a-z0-9]+', ' ', x.lower()).strip()} " in t for x in exclude if x.strip())


def _twitch_live(channel: str) -> dict | None:
    import yt_dlp

    try:
        with yt_dlp.YoutubeDL({"quiet": True, "skip_download": True, "noplaylist": True}) as ydl:
            info = ydl.extract_info(f"https://www.twitch.tv/{channel}", download=False)
    except Exception:
        return None  # offline -> yt-dlp meldet einen Fehler
    if not info.get("is_live"):
        return None
    return {"url": f"https://www.twitch.tv/{channel}", "title": f"🔴 {channel}: {info.get('description') or info.get('title') or 'live'}"}


class AutoLearner:
    def __init__(self, cfg, llm, library, studier, coach) -> None:
        self.cfg = cfg
        self.llm = llm
        self.lib = library
        self.studier = studier
        self.coach = coach
        self.task: asyncio.Task | None = None
        self._stop = False
        self.topics = ""
        self.status: dict = {"running": False}

    def c(self, key: str, default):
        return self.cfg.get_path(f"autolearn.{key}", default)

    @property
    def game(self) -> str:
        return str(self.c("game", "Modern Warfare 4"))

    @property
    def game_tags(self) -> list[str]:
        tags = [str(x) for x in (self.c("game_tags", ["MW4", "Modern Warfare 4"]) or [])]
        return tags if self.game.lower() in (t.lower() for t in tags) else [self.game]  # Spiel in der App geändert

    @property
    def modes(self) -> list[str]:
        return [str(x) for x in (self.c("modes", ["hardpoint", "snd", "hijack"]) or [])]

    @property
    def exclude(self) -> list[str]:
        own = {t.lower() for t in self.game_tags + [self.game]}
        return [x for x in (self.c("exclude_titles", None) or OTHER_TITLES) if x.lower() not in own]

    def ok(self, title: str) -> bool:
        return title_ok(title, self.exclude)

    def purge_queue(self) -> int:
        """Alte Einträge aus anderen CoD-Teilen aus der Warteschlange werfen."""
        n = 0
        for q in self.lib.queue(500):
            if q["status"] == "queued" and q.get("origin") != "manual" and not self.ok(q.get("title") or ""):
                self.lib.set_queue_status(q["id"], "skipped")
                n += 1
        return n

    @property
    def active(self) -> bool:
        return bool(self.task and not self.task.done())

    def add_links(self, links: list[str]) -> int:
        return sum(self.lib.enqueue(u.strip(), origin="manual") for u in links if u.strip().startswith("http"))

    def start(self, topics: str = "", links: list[str] | None = None) -> None:
        self.add_links(links or [])
        if self.active:
            self.topics = topics or self.topics
            return
        self.topics = topics
        self._stop = False
        skipped = self.purge_queue()
        if skipped:
            log.info("Fortbildung: %d Videos aus anderen CoD-Teilen übersprungen", skipped)
        self.status = {"running": True, "started": time.time(), "videos": 0, "ideas": 0, "message": "startet …"}
        self.task = asyncio.create_task(self._loop())

    def stop(self) -> None:
        self._stop = True
        self.studier.stop()

    # ------------------------------------------------------------- loop
    async def _loop(self) -> None:
        max_hours = float(self.c("max_hours", 0) or 0)
        max_videos = int(self.c("max_videos", 0) or 0)
        reflect_every = int(self.c("reflect_every", 3))
        empty_rounds = 0
        try:
            while not self._stop:
                if max_hours and time.time() - self.status["started"] > max_hours * 3600:
                    self.status["message"] = "Zeitlimit erreicht"
                    break
                if max_videos and self.status["videos"] >= max_videos:
                    self.status["message"] = "Video-Limit erreicht"
                    break
                live = await self._check_live()
                if live:
                    await self._study(live)
                    continue
                item = self.lib.next_queued()
                if item and item.get("origin") != "manual" and not self.ok(item.get("title") or ""):
                    self.lib.set_queue_status(item["id"], "skipped")
                    continue
                if not item:
                    self.status["message"] = "sucht neue Videos …"
                    added = await self.discover()
                    if not added:
                        empty_rounds += 1
                        if empty_rounds >= 3:
                            self.status["message"] = "nichts Neues gefunden – pausiere 10 min"
                            await self._sleep(600)
                            empty_rounds = 0
                        continue
                    empty_rounds = 0
                    continue
                await self._study(item)
                if self.status["videos"] and self.status["videos"] % reflect_every == 0 and not self._stop:
                    await self.reflect()
            if self.status["videos"] and not self._stop:
                await self.reflect()
        except Exception as e:
            log.exception("Fortbildungsmodus abgebrochen")
            self.status["message"] = f"Fehler: {e}"
        finally:
            self.status["running"] = False
            if self._stop:
                self.status["message"] = "gestoppt"

    async def _check_live(self) -> dict | None:
        """Läuft gerade ein Stream (z. B. CDL)? Dann zuerst live mitschauen."""
        if not self.c("watch_live", True) or time.time() - getattr(self, "_live_checked", 0) < 300:
            return None
        self._live_checked = time.time()
        for ch in self.c("twitch_channels", []) or []:
            info = await asyncio.to_thread(_twitch_live, ch)
            if info and self.ok(info["title"]):
                key = info["url"] + f"#live{int(time.time() // 3600)}"
                if self.lib.enqueue(key, info["title"], origin=f"live: {ch}"):
                    row = self.lib.conn.execute("SELECT * FROM queue WHERE url=?", (key,)).fetchone()
                    if row:
                        return {**dict(row), "url": info["url"]}
        return None

    async def _sleep(self, seconds: float) -> None:
        end = time.time() + seconds
        while time.time() < end and not self._stop:
            await asyncio.sleep(1)

    async def _study(self, item: dict) -> None:
        self.lib.set_queue_status(item["id"], "running")
        self.status["message"] = f"schaut: {item['title'] or item['url']}"
        self.status["current"] = item["title"] or item["url"]
        try:
            while self.studier.active:  # z. B. manuelle Session läuft noch
                await asyncio.sleep(2)
            live = str(item.get("origin") or "").startswith("live")
            self.studier.start(item["url"], item["title"] or "", note="autolearn",
                               max_minutes=float(self.c("max_live_minutes" if live else "max_minutes_per_video",
                                                        90 if live else 25)))
            await self.studier.task
            ok = self.studier.status.get("message") == "fertig"
            self.lib.set_queue_status(item["id"], "done" if ok else "error")
            self.status["videos"] += 1
        except Exception as e:
            log.warning("Video übersprungen: %s", e)
            self.lib.set_queue_status(item["id"], "error")

    # -------------------------------------------------------- discover
    async def discover(self) -> int:
        known = self.lib.known_urls()
        studied = [s["title"] for s in self.lib.sources(40)]
        pbs = {m: pb.get("summary", "") for m, pb in self.lib.playbooks().items()}
        content = (f"GAME: ONLY {self.game} (search tags: {', '.join(self.game_tags)}). Never other Call of Duty titles.\n"
                   f"MODES TO LEARN: {', '.join(self.modes)}\n"
                   f"FOCUS TOPICS FROM TEAM: {self.topics or '-'}\nLESSONS PER MODE: {json.dumps(self.lib.mode_counts())}\n"
                   f"PLAYBOOKS: {json.dumps(pbs, ensure_ascii=False)}\nRECENTLY STUDIED: {json.dumps(studied, ensure_ascii=False)}\n"
                   f"Give {int(self.c('queries_per_round', 4))} queries.")
        res = await self.llm.ask(self.coach._sys(P.DISCOVER), content, schema=P.DISCOVER_SCHEMA)
        queries = (res or {}).get("queries") or [f"{self.game_tags[0] if self.game_tags else self.game} {m} CDL"
                                                  for m in self.modes]
        tag = self.game_tags[0] if self.game_tags else self.game
        queries = [q if any(t.lower() in q.lower() for t in self.game_tags + [self.game]) else f"{q} {tag}" for q in queries]
        min_s, max_s = 60 * float(self.c("min_video_minutes", 3)), 60 * float(self.c("max_video_minutes", 240))
        added = 0
        for q in queries:
            try:
                vids = await asyncio.to_thread(_search_videos, q, int(self.c("results_per_query", 4)))
            except Exception as e:
                log.warning("Suche '%s' fehlgeschlagen: %s", q, e)
                continue
            for v in vids:
                d = v.get("duration") or 0
                if v["url"] in known or (d and not (min_s <= d <= max_s)) or not self.ok(v["title"]):
                    continue
                if self.lib.enqueue(v["url"], v["title"], origin=f"search: {q}", duration=d):
                    known.add(v["url"])
                    added += 1
        for ch in self.c("twitch_channels", []) or []:
            try:
                for v in await asyncio.to_thread(_twitch_vods, ch, 2):
                    if v["url"] not in known and self.ok(v["title"]) and self.lib.enqueue(v["url"], v["title"], origin=f"twitch: {ch}", duration=v.get("duration")):
                        added += 1
            except Exception as e:
                log.warning("Twitch-VODs %s: %s", ch, e)
        self.status["last_queries"] = queries
        return added

    # ---------------------------------------------------------- reflect
    async def reflect(self) -> dict | None:
        """Eigene Taktiken für das aktive Team entwickeln."""
        coach = self.coach
        self.status["message"] = "entwickelt eigene Taktiken …"
        content = "\n\n".join([
            f"WRITE IN: {coach.lang}",
            f"OUR TEAM: {coach.team.name}\nROSTER: {json.dumps(coach.roster(), ensure_ascii=False)}",
            coach._profiles_context(),
            coach.stats_text(),
            "PLAYBOOKS: " + json.dumps(self.lib.playbooks(), ensure_ascii=False)[:30000],
            "EXISTING OWN IDEAS (do not repeat): " + json.dumps([i["name"] for i in self.lib.ideas(coach.team.name)], ensure_ascii=False),
            f"FOCUS TOPICS: {self.topics or '-'}",
        ])
        res = await self.llm.ask(coach._sys(P.INNOVATE), content, deep=True, schema=P.INNOVATE_SCHEMA)
        if not res:
            return None
        for idea in res.get("ideas", []):
            self.lib.add_idea(coach.team.name, idea)
            coach.db.add_memory(f"Eigene Taktik-Idee '{idea.get('name')}' ({idea.get('mode')}, {idea.get('map')}): {idea.get('concept')}", "autolearn")
        coach.team.reports.mkdir(parents=True, exist_ok=True)
        path = coach.team.reports / f"eigene_taktiken_{time.strftime('%Y-%m-%d_%H%M')}.md"
        path.write_text(res.get("report_markdown", ""), encoding="utf-8")
        self.status["ideas"] = self.status.get("ideas", 0) + len(res.get("ideas", []))
        await coach.notify(f"🧠 Neue eigene Taktiken für {coach.team.name}:\n" + res.get("report_markdown", "")[:1600], path)
        return res
