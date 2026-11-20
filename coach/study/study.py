"""Lern-Modus: Yvara AI schaut Pro-Matches, Streams, VODs, Highlights oder Scrims
und schreibt daraus Lektionen in die Taktik-Bibliothek.

Quellen:
  screen  – alles, was auf dem Bildschirm läuft (Twitch im Browser, Discord-
            Übertragung, YouTube, CDL-Stream live). Ton (Caster/Comms) kommt
            über den Loopback-Eingang.
  video   – lokale Videodatei (z. B. eure aufgenommenen Scrims).
  url     – Link zu einem VOD / Video (YouTube, Twitch-VOD …), wird mit yt-dlp
            geladen. Nur Inhalte nutzen, die ihr zur Analyse verwenden dürft.
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import shutil
import subprocess
import time
from pathlib import Path

import numpy as np

from ..brain import prompts as P
from ..brain.llm import image_block
from ..config import DATA_DIR

log = logging.getLogger(__name__)
DL_DIR = DATA_DIR / "study_videos"


def _ffprobe_duration(path: Path) -> float:
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
                         capture_output=True, text=True, check=True).stdout.strip()
    return float(out or 0)


def _frame_b64(path: Path, t: float, width: int) -> str | None:
    res = subprocess.run(["ffmpeg", "-v", "quiet", "-ss", f"{t:.2f}", "-i", str(path), "-frames:v", "1",
                          "-vf", f"scale={width}:-2", "-f", "image2pipe", "-vcodec", "mjpeg", "-q:v", "4", "-"],
                         capture_output=True)
    return base64.standard_b64encode(res.stdout).decode("ascii") if res.stdout else None


def _audio_16k(path: Path) -> np.ndarray:
    raw = subprocess.run(["ffmpeg", "-v", "quiet", "-i", str(path), "-vn", "-f", "f32le", "-ac", "1", "-ar", "16000", "-"],
                         capture_output=True, check=True).stdout
    return np.frombuffer(raw, dtype=np.float32)


def _download(url: str) -> tuple[Path, str]:
    import yt_dlp

    DL_DIR.mkdir(parents=True, exist_ok=True)
    opts = {"format": "bv*[height<=720]+ba/b[height<=720]/b", "outtmpl": str(DL_DIR / "%(id)s.%(ext)s"),
            "merge_output_format": "mp4", "quiet": True, "noprogress": True, "noplaylist": True}
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)
        if info.get("is_live"):
            raise RuntimeError("Live-Streams bitte im Bildschirm-Modus anschauen (Stream öffnen, dann 'Bildschirm').")
        return Path(ydl.prepare_filename(info)).with_suffix(".mp4"), info.get("title") or url


def _live_stream(url: str) -> tuple[str, str] | None:
    """Direkte Stream-Adresse eines laufenden Livestreams (Twitch/YouTube) – None, wenn nicht live."""
    import yt_dlp

    with yt_dlp.YoutubeDL({"quiet": True, "format": "best[height<=720]/best", "noplaylist": True}) as ydl:
        info = ydl.extract_info(url, download=False)
    if not info.get("is_live"):
        return None
    return info.get("url"), info.get("title") or info.get("description") or url


def _grab_chunk(stream_url: str, seconds: float, out: Path) -> bool:
    res = subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", stream_url, "-t", f"{seconds:.0f}", "-c", "copy", str(out)],
                         capture_output=True, timeout=seconds + 60)
    return res.returncode == 0 and out.exists() and out.stat().st_size > 10000


class Studier:
    def __init__(self, cfg, llm, library, stt=None, capture=None, lang_fn=lambda: "de", coach=None) -> None:
        self.cfg = cfg
        self.llm = llm
        self.lib = library
        self.stt = stt
        self.capture = capture
        self.lang_fn = lang_fn
        self.coach = coach
        self.task: asyncio.Task | None = None
        self.source_id: int | None = None
        self.status: dict = {"running": False}
        self._stop = False
        self._screen_transcript: list[tuple[float, str]] = []
        self.own_team = False

    # ------------------------------------------------------------ settings
    @property
    def window_s(self) -> float:
        return float(self.cfg.get_path("study.window_s", 60))

    @property
    def frames(self) -> int:
        return int(self.cfg.get_path("study.frames_per_window", 5))

    @property
    def width(self) -> int:
        return int(self.cfg.get_path("study.frame_width", 1024))

    @property
    def active(self) -> bool:
        return bool(self.task and not self.task.done())

    # ------------------------------------------------------------- control
    def start(self, source: str, title: str = "", mode_hint: str = "", note: str = "", own_team: bool = False,
              max_minutes: float | None = None) -> int:
        if self.active:
            raise RuntimeError("Es läuft schon eine Lern-Session")
        self._stop = False
        self.own_team = own_team
        self.max_minutes = max_minutes
        if own_team and self.coach:
            note = (f"This is OUR team '{self.coach.team.name}' playing (league match / stream). Focus on what WE did wrong "
                    f"and right, and what to fix. Our players: {', '.join(p['ingame'] for p in self.coach.roster())}. " + note)
        if source == "screen":
            kind, ref = "screen", "screen"
        elif source.startswith(("http://", "https://")):
            kind, ref = "url", source
        else:
            kind, ref = "video", source
        self.source_id = self.lib.add_source(kind, ref, title or ref, note)
        self.status = {"running": True, "kind": kind, "title": title or ref, "progress": 0.0, "lessons": 0,
                       "source_id": self.source_id, "message": "startet …"}
        coro = self._run_screen(title, mode_hint, note) if kind == "screen" else self._run_video(kind, ref, title, mode_hint, note)
        if kind == "url" and ("twitch.tv/" in ref and "/videos/" not in ref or "/live" in ref):
            coro.close()
            coro = self._run_live(ref, title, mode_hint, note)
        self.task = asyncio.create_task(self._guard(coro))
        return self.source_id

    def stop(self) -> None:
        self._stop = True

    def feed_transcript(self, text: str) -> None:
        """Ton aus Loopback (Caster/Comms) während Bildschirm-Lernen."""
        if self.active and self.status.get("kind") == "screen":
            self._screen_transcript.append((time.time(), text))

    async def _guard(self, coro) -> None:
        try:
            await coro
            self.lib.update_source(self.source_id, status="done", progress=1.0, ended=time.time())
            self.status.update(running=False, progress=1.0, message="fertig")
        except Exception as e:
            log.exception("Lern-Session fehlgeschlagen")
            self.lib.update_source(self.source_id, status=f"error: {e}", ended=time.time())
            self.status.update(running=False, message=f"Fehler: {e}")

    # -------------------------------------------------------------- core
    def _minimap_b64(self, jpeg_b64: str | None) -> str | None:
        """Minimap (Caster-Ansicht: alle 8 Spieler) aus dem letzten Bild vergrößert ausschneiden."""
        roi = self.cfg.get_path("study.minimap_roi") or self.cfg.get_path("hud.minimap")
        if not roi or not jpeg_b64:
            return None
        import cv2

        from ..vision.capture import crop_rel, to_jpeg_b64

        img = cv2.imdecode(np.frombuffer(base64.b64decode(jpeg_b64), np.uint8), cv2.IMREAD_COLOR)
        crop = crop_rel(img, roi) if img is not None else None
        if crop is None or crop.size == 0:
            return None
        return to_jpeg_b64(cv2.resize(crop, (512, 512), interpolation=cv2.INTER_CUBIC), 512, 90)

    async def _learn_window(self, images: list[str], transcript: str, t_video: float, mode_hint: str, note: str) -> list[dict]:
        lang = self.lang_fn()
        content: list = [image_block(b) for b in images if b]
        mm = await asyncio.to_thread(self._minimap_b64, next((b for b in reversed(images) if b), None))
        if mm:
            content.append(image_block(mm))
            content.append({"type": "text", "text": "(Last image = enlarged minimap of the last frame.)"})
        content.append({"type": "text", "text":
                        f"SOURCE: {self.status.get('title')}\nMODE HINT: {mode_hint or 'unknown'}\nNOTE: {note or '-'}\n"
                        f"WINDOW START: {int(t_video)} s\nTRANSCRIPT:\n{transcript or '(no audio)'}"})
        system = self._sys(P.STUDY, lang=lang)
        res = await self.llm.ask(system, content, schema=P.STUDY_SCHEMA, deep=True, max_tokens=12000,
                                 effort=self.cfg.get_path("study.effort", "medium"))
        if not res or not res.get("is_gameplay", True):
            return []
        lessons = res.get("lessons", [])
        for les in lessons:
            les.setdefault("map", res.get("map_detected", ""))
            if not les.get("mode"):
                les["mode"] = mode_hint or res.get("mode_detected", "")
        if getattr(self, "own_team", False) and self.coach:
            # Eigene Fehler/Stärken gehören ins Team-Gedächtnis, nicht in die Pro-Bibliothek
            for les in lessons:
                if les.get("lesson") and float(les.get("confidence", 0)) >= 0.5:
                    self.coach.db.add_memory(f"[Stream-Analyse {self.status.get('title')}] {les['lesson']}", "study")
            self.status["lessons"] = self.status.get("lessons", 0) + len(lessons)
            return lessons
        n = self.lib.add_lessons(self.source_id, t_video, lessons)
        for co in res.get("callouts", []) or []:
            mp = co.get("map") or res.get("map_detected") or ""
            if mp and co.get("name"):
                self.lib.add_callout(mp, co["name"], co.get("zone", ""), note=co.get("note", ""), source="learned")
        self.status["lessons"] = self.status.get("lessons", 0) + n
        self.status["last_summary"] = res.get("summary", "")
        return lessons

    def _sys(self, template: str, **kw) -> str:
        if self.coach:
            return self.coach._sys(template, **kw)
        return template.format(name="Yvara AI", personality="", team="-", **kw)

    async def _run_video(self, kind: str, ref: str, title: str, mode_hint: str, note: str) -> None:
        if not shutil.which("ffmpeg"):
            raise RuntimeError("ffmpeg nicht gefunden (siehe README)")
        if kind == "url":
            self.status["message"] = "lädt Video …"
            path, vid_title = await asyncio.to_thread(_download, ref)
            if not title:
                self.status["title"] = vid_title
                self.lib.update_source(self.source_id, title=vid_title)
        else:
            path = Path(ref).expanduser()
            if not path.exists():
                raise FileNotFoundError(path)
        duration = await asyncio.to_thread(_ffprobe_duration, path)
        self.lib.update_source(self.source_id, duration=duration)
        self.status["duration"] = duration
        audio = np.zeros(0, np.float32)
        if self.stt:
            self.status["message"] = "lädt Ton …"
            audio = await asyncio.to_thread(_audio_16k, path)
        modes = set()
        starts = [i * self.window_s for i in range(int(duration // self.window_s) + (1 if duration % self.window_s else 0))]
        cap = getattr(self, "max_minutes", None)
        if cap and len(starts) * self.window_s > cap * 60:
            # Lange VODs: gleichmäßig verteilte Ausschnitte statt alles
            n = max(1, int(cap * 60 // self.window_s))
            starts = [starts[int(i * len(starts) / n)] for i in range(n)]
        for idx, t in enumerate(starts):
            await self._yield_to_live()
            if self._stop:
                break
            self.status["message"] = f"analysiert {int(t // 60)}:{int(t % 60):02d} / {int(duration // 60)}:{int(duration % 60):02d}"
            step = self.window_s / self.frames
            imgs = await asyncio.gather(*(asyncio.to_thread(_frame_b64, path, min(duration - 0.5, t + i * step), self.width)
                                          for i in range(self.frames)))
            text = await self._transcribe_window(audio, t)
            for les in await self._learn_window(list(imgs), text, t, mode_hint, note):
                modes.add((les.get("mode") or "").lower())
            seen = min(self.window_s, max(0.0, duration - t))
            self.lib.add_watched(self.source_id, seen)
            self.status["watched_s"] = self.status.get("watched_s", 0) + seen
            self.status["progress"] = (idx + 1) / len(starts)
            self.lib.update_source(self.source_id, progress=self.status["progress"])
        if kind == "url" and not self.cfg.get_path("autolearn.keep_videos", False):
            path.unlink(missing_ok=True)  # Speicher sparen
        if not self.own_team:
            await self.consolidate([m for m in modes if m])

    async def _transcribe_window(self, audio: np.ndarray, t: float) -> str:
        """Kommentar nur für diesen Abschnitt transkribieren – so startet das Lernen sofort."""
        if not self.stt or not len(audio):
            return ""
        part = audio[int(t * 16000): int((t + self.window_s) * 16000)]
        if len(part) < 16000:
            return ""
        try:
            segs = await asyncio.to_thread(self.stt.segments_sync, part)
        except Exception as e:
            log.debug("Transkription: %s", e)
            return ""
        return " ".join(x[2] for x in segs)

    async def _yield_to_live(self) -> None:
        """Beim Hintergrund-Lernen hat Live-Coaching Vorrang: während eines Matches pausieren."""
        coach = self.coach
        paused = False
        while not self._stop and coach is not None and getattr(coach, "tracker", None) is not None and not self.own_team:
            match = coach.tracker.state.active
            talking = time.time() - float(getattr(coach, "_last_talk", 0) or 0) < 40
            watching = bool(getattr(coach, "watch", {}).get("on"))
            gaming = bool(getattr(coach, "gaming", False))
            if not (match or talking or watching or gaming):
                break
            if not paused:
                paused = True
                self.status["message"] = ("pausiert – Match läuft" if match else
                                          "pausiert – Call of Duty läuft (Gaming-Modus)" if gaming else "pausiert – ich schaue gerade bei dir zu" if watching
                                           else "pausiert – ihr redet gerade mit mir") + \
                    ", Live-Coaching hat Vorrang"
            await asyncio.sleep(3)

    async def _run_live(self, url: str, title: str, mode_hint: str, note: str) -> None:
        """Livestream (z. B. CDL auf Twitch) mitschauen: immer ein Stück aufnehmen, auswerten, weiter."""
        if not shutil.which("ffmpeg"):
            raise RuntimeError("ffmpeg nicht gefunden (siehe README)")
        info = await asyncio.to_thread(_live_stream, url)
        if not info:
            raise RuntimeError("Stream ist gerade nicht live")
        stream_url, live_title = info
        if not title:
            self.status["title"] = f"🔴 LIVE: {live_title}"
            self.lib.update_source(self.source_id, title=self.status["title"])
        DL_DIR.mkdir(parents=True, exist_ok=True)
        chunk = DL_DIR / f"live_{self.source_id}.mp4"
        cap = float(getattr(self, "max_minutes", None) or self.cfg.get_path("autolearn.max_live_minutes", 90))
        start, fails, modes = time.time(), 0, set()
        while not self._stop and time.time() - start < cap * 60:
            await self._yield_to_live()
            self.status["message"] = f"schaut live seit {int((time.time() - start) // 60)} min"
            ok = await asyncio.to_thread(_grab_chunk, stream_url, self.window_s, chunk)
            if not ok:
                fails += 1
                if fails >= 3:
                    break  # Stream vorbei
                info = await asyncio.to_thread(_live_stream, url)
                if not info:
                    break
                stream_url = info[0]
                continue
            fails = 0
            dur = await asyncio.to_thread(_ffprobe_duration, chunk)
            step = max(1.0, dur / self.frames)
            imgs = await asyncio.gather(*(asyncio.to_thread(_frame_b64, chunk, min(dur - 0.5, i * step), self.width)
                                          for i in range(self.frames)))
            audio = await asyncio.to_thread(_audio_16k, chunk) if self.stt else np.zeros(0, np.float32)
            text = await self._transcribe_window(audio, 0)
            for les in await self._learn_window([i for i in imgs if i], text, time.time() - start, mode_hint, note):
                modes.add((les.get("mode") or "").lower())
            self.lib.add_watched(self.source_id, dur)
            self.status["watched_s"] = self.status.get("watched_s", 0) + dur
            self.lib.update_source(self.source_id, duration=time.time() - start)
        chunk.unlink(missing_ok=True)
        if not self.own_team:
            await self.consolidate([m for m in modes if m])

    async def _run_screen(self, title: str, mode_hint: str, note: str) -> None:
        if not self.capture:
            raise RuntimeError("Bildschirmaufnahme nicht verfügbar")
        from ..vision.capture import to_jpeg_b64

        modes = set()
        start = time.time()
        while not self._stop:
            w_start = time.time()
            imgs = []
            for _ in range(self.frames):
                frame = await asyncio.to_thread(self.capture.grab)
                imgs.append(await asyncio.to_thread(to_jpeg_b64, frame, self.width, 75))
                await asyncio.sleep(self.window_s / self.frames)
                if self._stop:
                    break
            text = " ".join(tx for ts, tx in self._screen_transcript if ts >= w_start)
            self._screen_transcript = [x for x in self._screen_transcript if x[0] >= w_start]
            self.status["message"] = f"schaut zu seit {int((time.time() - start) // 60)} min"
            seen = time.time() - w_start
            self.lib.add_watched(self.source_id, seen)
            self.lib.update_source(self.source_id, duration=time.time() - start)
            self.status["watched_s"] = self.status.get("watched_s", 0) + seen
            for les in await self._learn_window(imgs, text, w_start - start, mode_hint, note):
                modes.add((les.get("mode") or "").lower())
        if not self.own_team:
            await self.consolidate([m for m in modes if m])

    # --------------------------------------------------------- playbooks
    async def consolidate(self, modes: list[str]) -> None:
        """Lektionen pro Modus zu einem Playbook verdichten."""
        for mode in modes:
            lessons = self.lib.lessons(mode, limit=250)
            if not lessons:
                continue
            self.status["message"] = f"aktualisiert Playbook {mode} …"
            content = (f"MODE: {mode}\nEXISTING PLAYBOOK: {json.dumps(self.lib.playbook(mode), ensure_ascii=False) or '{}'}\n\n"
                       "LESSONS:\n" + "\n".join(f"- [{l['confidence']:.1f}] ({l['team']}, {l['map']}) {l['situation']} -> {l['lesson']}"
                                                for l in lessons))
            pb = await self.llm.ask(self._sys(P.CONSOLIDATE, lang=self.lang_fn()), content, deep=True,
                                    schema=P.PLAYBOOK_SCHEMA, max_tokens=16000)
            if pb:
                self.lib.set_playbook(mode, pb, len(lessons))
