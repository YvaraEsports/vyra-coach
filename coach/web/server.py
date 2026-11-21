"""Lokale Chat-Oberfläche (nur auf diesem PC erreichbar: http://127.0.0.1:8765)."""
from __future__ import annotations

import asyncio
import logging
import os
import re
import subprocess
import wave
from pathlib import Path

import numpy as np
from aiohttp import web

from ..audio.tts import CACHE
from ..config import save_user_value

log = logging.getLogger(__name__)
STATIC = Path(__file__).parent / "static"

SETTINGS = {
    "coach.name": str, "coach.personality": str, "language": str, "tts.engine": str, "tts.supertonic_voice": str, "audio.stt_engine": str,
    "tts.piper_de": str, "tts.piper_en": str, "tts.piper_speed": float, "web.research": bool,
    "callouts.enabled": bool, "capture.vision_interval_s": float, "audio.push_to_talk_key": str,
    "llm.provider": str, "llm.local_model": str, "llm.base_url": str, "llm.local_vision": bool,
    "coaching_mode": str, "tts.xtts_voice": str, "tts.xtts_voice_en": str, "tts.same_voice": bool, "coach.vibe": str, "coach.roast_level": int, "autolearn.game": str, "autolearn.autostart": bool,
    "performance.gaming_mode": str, "performance.game_model": str, "discord.follow_user": str,
}
PREVIEW = {"de": "Hey, ich bin Yvara AI. Rotiert früh zum nächsten Hill, ich halte euch den Rücken frei.",
           "en": "Hey, I'm Yvara AI. Rotate early to the next hill, I've got your back."}


def decode_audio(raw: bytes) -> np.ndarray:
    """Beliebiges Browser-Audio (webm/ogg/wav) -> 16 kHz mono float32 via ffmpeg."""
    if raw[:4] == b"RIFF" and raw[8:12] == b"WAVE":  # App-Call schickt fertiges 16-kHz-WAV: ohne ffmpeg lesen
        import io

        try:
            with wave.open(io.BytesIO(raw)) as wf:
                if wf.getframerate() == 16000 and wf.getnchannels() == 1 and wf.getsampwidth() == 2:
                    return np.frombuffer(wf.readframes(wf.getnframes()), dtype="<i2").astype(np.float32) / 32768.0
        except Exception:
            pass
    out = subprocess.run(["ffmpeg", "-v", "quiet", "-i", "pipe:0", "-f", "f32le", "-ac", "1", "-ar", "16000", "pipe:1"],
                         input=raw, capture_output=True, check=True).stdout
    return np.frombuffer(out, dtype=np.float32)


LAN_PATHS = ("/overlay", "/api/caster/feed", "/audio/", "/static/overlay.", "/static/logo")


def lan_request_ok(remote: str | None, method: str, path: str) -> bool:
    """Andere Geräte im Heimnetz (z. B. OBS auf dem Gaming-PC) dürfen NUR das Caster-Overlay ansehen."""
    import ipaddress

    if remote in ("127.0.0.1", "::1", None):
        return True
    try:
        ip = ipaddress.ip_address(str(remote).split("%")[0])
    except ValueError:
        return False
    if ip.is_loopback:
        return True
    return ip.is_private and method in ("GET", "HEAD") and path.startswith(LAN_PATHS)


def lan_ip() -> str:
    """Eigene Adresse im Heimnetz (es wird nichts gesendet)."""
    import socket

    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


@web.middleware
async def _lan_guard(request, handler):
    if not lan_request_ok(request.remote, request.method, request.path):
        raise web.HTTPForbidden(text="Nur das Caster-Overlay ist im Heimnetz freigegeben")
    return await handler(request)


@web.middleware
async def _no_cache(request, handler):
    """Nach Updates immer die neue Oberfläche zeigen – der Browser soll nichts Altes zwischenspeichern."""
    resp = await handler(request)
    if request.path == "/" or request.path.startswith("/static/"):
        resp.headers["Cache-Control"] = "no-store"
    return resp


def prepare_voice_sample(audio: np.ndarray, rate: int, max_s: float = 30.0) -> np.ndarray:
    """Stimmprobe säubern: Stille vorn/hinten weg, Lautstärke angleichen, höchstens 30 s."""
    audio = np.nan_to_num(audio.astype(np.float32))
    if not len(audio):
        return audio
    win = max(1, rate // 50)  # 20-ms-Fenster
    n = len(audio) // win
    if n:
        rms = np.sqrt(np.mean(audio[: n * win].reshape(n, win) ** 2, axis=1))
        thr = max(1e-4, float(np.percentile(rms, 95)) * 0.05)  # relativ zur eigenen Lautstärke
        voiced = np.where(rms > thr)[0]
        if len(voiced):
            a, b = max(0, voiced[0] - 10) * win, min(n, voiced[-1] + 10) * win
            audio = audio[a:b]
    peak = float(np.max(np.abs(audio))) if len(audio) else 0.0
    if peak > 1e-4:
        audio = audio / peak * 0.9
    return audio[: int(max_s * rate)]


def speech_seconds(audio: np.ndarray, rate: int) -> float:
    """Wie viele Sekunden tatsächlich gesprochen werden (absolute Schwelle, damit Stille/Rauschen nicht zählt)."""
    win = max(1, rate // 50)
    n = len(audio) // win
    if not n:
        return 0.0
    rms = np.sqrt(np.mean(audio[: n * win].reshape(n, win).astype(np.float32) ** 2, axis=1))
    return float(np.sum(rms > 0.02)) * win / rate


def _wav_silent(path: Path) -> bool:
    """Gespeicherte Stimmprobe ohne hörbare Sprache (z. B. stummes Mikrofon bei der Aufnahme)?"""
    try:
        with wave.open(str(path)) as wf:
            a = np.frombuffer(wf.readframes(wf.getnframes()), "<i2").astype(np.float32) / 32768
            return speech_seconds(a, wf.getframerate()) < 4
    except Exception:
        return False


class WebUI:
    def __init__(self, cfg, coach, tts) -> None:
        self.cfg = cfg
        self.coach = coach
        self.tts = tts
        self.app = web.Application(client_max_size=25 * 1024 * 1024, middlewares=[_lan_guard, _no_cache])
        r = self.app.router
        r.add_get("/", self.index)
        r.add_static("/static", STATIC)
        r.add_get("/audio/{name}", self.audio)
        r.add_get("/api/state", self.state)
        r.add_get("/overlay", self.overlay)
        r.add_get("/api/sync", self.sync_state)
        r.add_post("/api/sync", self.sync_set)
        r.add_get("/api/caster", self.caster_state)
        r.add_post("/api/caster", self.caster_set)
        r.add_get("/api/caster/feed", self.caster_feed)
        r.add_post("/api/caster/moment", self.caster_moment)
        r.add_post("/api/caster/learn", self.caster_learn)
        r.add_get("/api/caster/preview.jpg", self.caster_preview)
        r.add_post("/api/quit", self.quit)
        r.add_get("/api/logs", self.logs)
        r.add_get("/api/sessions", self.sessions)
        r.add_post("/api/sessions", self.new_session)
        r.add_get("/api/sessions/{sid}", self.session)
        r.add_delete("/api/sessions/{sid}", self.delete_session)
        r.add_post("/api/chat", self.chat)
        r.add_post("/api/voice", self.voice)
        r.add_post("/api/call", self.call)
        r.add_get("/api/memories", self.memories)
        r.add_post("/api/memories", self.add_memory)
        r.add_delete("/api/memories/{mid}", self.delete_memory)
        r.add_get("/api/teams", self.teams)
        r.add_post("/api/teams", self.save_team)
        r.add_post("/api/teams/activate", self.activate_team)
        r.add_post("/api/settings", self.save_settings)
        r.add_get("/api/update", self.update_check)
        r.add_post("/api/update", self.update_apply)
        r.add_post("/api/match/start", self.match_start)
        r.add_post("/api/match/end", self.match_end)
        r.add_post("/api/focus", self.focus)
        r.add_post("/api/mode", self.set_mode)
        r.add_get("/api/callouts", self.get_callouts)
        r.add_post("/api/callouts", self.add_callout)
        r.add_delete("/api/callouts/{cid}", self.delete_callout)
        r.add_post("/api/lesson", self.lesson)
        r.add_get("/api/feeds", self.get_feeds)
        r.add_post("/api/feeds", self.set_feeds)
        r.add_post("/api/plan", self.plan)
        r.add_get("/api/profile", self.profile)
        r.add_post("/api/training", self.training)
        r.add_post("/api/scout", self.scout)
        r.add_post("/api/clip", self.clip)
        r.add_post("/api/timeout", self.timeout)
        r.add_get("/api/clips", self.clips)
        r.add_post("/api/study", self.study_start)
        r.add_post("/api/study/stop", self.study_stop)
        r.add_post("/api/autolearn", self.autolearn_start)
        r.add_post("/api/autolearn/stop", self.autolearn_stop)
        r.add_post("/api/queue", self.enqueue)
        r.add_get("/api/watch", self.watch_state)
        r.add_post("/api/watch", self.watch_set)
        r.add_get("/api/watch/preview.jpg", self.watch_preview)
        r.add_get("/api/maps", self.maps)
        r.add_get("/api/heatmaps", self.heatmaps)
        r.add_get("/api/heatmap.png", self.heatmap_png)
        r.add_post("/api/heatmap/discord", self.heatmap_discord)
        r.add_get("/api/strats", self.strats)
        r.add_post("/api/strats", self.strat_save)
        r.add_get("/api/strats/bg.png", self.strat_bg)
        r.add_post("/api/strats/bg", self.strat_bg_upload)
        r.add_get("/api/strats/{sid}.png", self.strat_png)
        r.add_delete("/api/strats/{sid}", self.strat_delete)
        r.add_post("/api/strats/{sid}/discord", self.strat_discord)
        r.add_get("/api/voices", self.voices)
        r.add_post("/api/voices/preview", self.voice_preview)
        r.add_post("/api/voices/test", self.voice_test)
        r.add_post("/api/voices/custom", self.voice_upload)
        r.add_delete("/api/voices/custom/{name}", self.voice_delete)
        r.add_post("/api/voices/style", self.voice_style)
        r.add_get("/api/library", self.library)
        r.add_delete("/api/library/{lid}", self.delete_lesson)
        r.add_post("/api/library/consolidate", self.consolidate)

    async def start(self, port: int) -> None:
        runner = web.AppRunner(self.app)
        await runner.setup()
        lan = bool(self.cfg.get_path("web.lan_overlay", False))
        await web.TCPSite(runner, "0.0.0.0" if lan else "127.0.0.1", port).start()
        log.info("Chat-Oberfläche: http://127.0.0.1:%s", port)
        if lan:
            log.info("Caster-Overlay im Heimnetz: http://%s:%s/overlay (nur das Overlay ist von außen erreichbar)",
                     lan_ip(), port)

    # ----------------------------------------------------------- helpers
    async def _speak_file(self, text: str, lang: str) -> str | None:
        try:
            path = await self.tts.synth(_strip_md(text)[:1200], lang)
            return f"/audio/{path.name}"
        except Exception as e:
            log.warning("TTS für Chat fehlgeschlagen: %s", e)
            return None

    async def _chat_reply(self, sid: int, text: str, web_flag, speak: bool) -> dict:
        reply = await self.coach.chat(sid, text, web=web_flag)
        lang = self.coach.last_lang
        audio = await self._speak_file(reply, lang) if speak else None
        return {"reply": reply, "lang": lang, "audio": audio, "session_id": sid}

    # ------------------------------------------------------------ routes
    async def index(self, _req):
        from .. import __version__

        html = (STATIC / "index.html").read_text(encoding="utf-8")
        html = html.replace("/static/app.js", f"/static/app.js?v={__version__}").replace(
            "/static/style.css", f"/static/style.css?v={__version__}")
        return web.Response(text=html, content_type="text/html", headers={"Cache-Control": "no-store"})

    async def audio(self, req):
        name = req.match_info["name"]
        if not re.fullmatch(r"[0-9a-f]{16}\.(wav|mp3|aiff)", name):
            raise web.HTTPNotFound()
        return web.FileResponse(CACHE / name)

    async def _need_brain(self):
        """None wenn das KI-Modell bereit ist, sonst eine Fehlerantwort für die Oberfläche."""
        b = await self.coach.brain_status(force=True)
        if b["ok"]:
            return None
        return web.json_response({"ok": False, "error": b["message"]}, status=409)

    async def quit(self, req):
        """Vom App-Fenster beim Schließen: Yvara AI beenden (nur von diesem PC)."""
        if req.remote not in ("127.0.0.1", "::1"):
            return web.json_response({"ok": False}, status=403)
        log.info("App-Fenster geschlossen – Yvara AI wird beendet (KI-Modelle werden aus dem Grafikspeicher entladen)")

        async def shutdown() -> None:
            llm = getattr(self.coach, "llm", None)
            models = {getattr(llm, "model", None)}
            gm = getattr(self.coach, "gaming_mode", None)
            models |= {getattr(gm, "_big", None), self.cfg.get_path("performance.game_model")}
            if llm is not None and hasattr(llm, "unload"):
                for m in [m for m in models if m]:
                    try:
                        await asyncio.wait_for(llm.unload(m), 3)
                    except Exception:
                        pass
            bot = getattr(self.coach, "bot", None)
            if bot is not None:
                try:
                    await asyncio.wait_for(bot.close(), 3)
                except Exception:
                    pass
            os._exit(0)

        asyncio.get_running_loop().call_later(0.3, lambda: asyncio.ensure_future(shutdown()))
        asyncio.get_running_loop().call_later(8, os._exit, 0)  # spätestens nach 8 s ist alles zu
        return web.json_response({"ok": True})

    async def logs(self, req):
        """Konsole in der App: letzte Zeilen aus data/logs/vyra.log (nur von diesem PC)."""
        if req.remote not in ("127.0.0.1", "::1"):
            return web.json_response({"lines": []}, status=403)
        from ..config import DATA_DIR

        n = max(20, min(int(req.query.get("n", 300) or 300), 2000))
        path = DATA_DIR / "logs" / "vyra.log"
        try:
            with open(path, "rb") as f:
                f.seek(0, 2)
                size = f.tell()
                f.seek(max(0, size - 400_000))
                lines = [x for x in f.read().decode("utf-8", "replace").splitlines() if "aiohttp.access" not in x][-n:]
        except OSError:
            lines = ["(noch kein Log vorhanden)"]
        return web.json_response({"lines": lines})

    async def state(self, _req):
        c = self.coach
        st = c.tracker.state
        from .. import __version__

        return web.json_response({
            "version": __version__,
            "web_allowed": bool(self.cfg.get_path("privacy.allow_web_search", False)),
            "gaming": self.coach.gaming_mode.status() if getattr(self.coach, "gaming_mode", None) else None,
            "brain": await c.brain_status(),
            "discord_error": getattr(c, "discord_error", ""),
            "discord": self._discord_status(),
            "name": c.name,
            "team": c.team.info(),
            "teams": c.teams.list(),
            "focus": c.focus_player,
            "coaching_mode": c.mode,
            "lesson": ({"title": c.lesson.get("title"), "idx": c.lesson["idx"], "steps": [x["say"] for x in c.lesson["steps"]]}
                       if c.lesson else None),
            "llm": {"provider": self.cfg.get_path("llm.provider", "ollama"),
                    "model": getattr(c.llm, "model", "")},
            "match": {"active": st.active, "summary": st.summary(), "mode": st.mode, "map": st.map, "opponent": st.opponent,
                      "ours": st.score_ours, "enemy": st.score_enemy},
            "settings": {k: self.cfg.get_path(k) for k in SETTINGS},
            "feed": [{"ts": ts, "text": tx} for ts, tx in c.voice.spoken_log[-30:]],
            "stats": c.stats_text(),
        })

    # -------------------------------------------------------------- Sync
    async def sync_state(self, _req):
        from .. import sync

        g = lambda k, d=None: self.cfg.get_path(f"sync.{k}", d)  # noqa: E731
        rcv = sync.RECEIVER
        return web.json_response({
            "role": g("role", "off") or "off", "ip": lan_ip(), "port": int(g("port", sync.PORT) or sync.PORT),
            "code": rcv.code if rcv else sync.pair_code() if (g("role") == "receive") else "",
            "receiver": rcv.status() if rcv else None,
            "settings": {k: g(k, d) for k, d in (("host", ""), ("code", ""), ("video", True), ("audio", True),
                                                  ("mic", False), ("monitor", 1))}})

    async def sync_set(self, req):
        d = await req.json()
        keys = {"role": str, "host": str, "code": str, "video": bool, "audio": bool, "mic": bool, "monitor": int}
        for k, typ in keys.items():
            if k not in d:
                continue
            v = typ(d[k])
            if k == "role" and v not in ("off", "send", "receive"):
                continue
            if k == "code" and v and not re.fullmatch(r"\d{6}", v.strip()):
                return web.json_response({"ok": False, "error": "Der Kopplungs-Code hat 6 Ziffern"}, status=400)
            save_user_value(self.cfg, f"sync.{k}", v.strip() if isinstance(v, str) else v)
        if d.get("restart"):
            from .. import updater

            asyncio.get_running_loop().call_later(1.0, updater.restart_now)
        return web.json_response({"ok": True, "restart": bool(d.get("restart"))})

    # ------------------------------------------------------------ Caster
    CASTER_KEYS = {"lang": str, "style": str, "voice": str, "output": str, "source": str, "twitch_channel": str,
                   "interval_s": float, "max_words": int, "title": str, "team_a": str, "team_b": str, "score": str,
                   "fps": float, "image_width": int}

    async def overlay(self, _req):
        return web.FileResponse(STATIC / "overlay.html", headers={"Cache-Control": "no-store"})

    def _caster(self):
        c = getattr(self.coach, "caster", None)
        if c is None:
            raise web.HTTPServiceUnavailable(text="Caster nicht verfügbar")
        return c

    async def caster_state(self, _req):
        c = self._caster()
        port = int(self.cfg.get_path("web.port", 8765))
        lan = bool(self.cfg.get_path("web.lan_overlay", False))
        return web.json_response({**c.state(), "lan_overlay": lan,
                                  "overlay_url": f"http://{lan_ip() if lan else '127.0.0.1'}:{port}/overlay"})

    @staticmethod
    def _valid_source(src: str) -> bool:
        from ..vision.feeds import stream_url

        if src in ("screen", "sync") or re.fullmatch(r"(monitor|device):\d{1,2}", src) or re.fullmatch(r"twitch:\w{1,25}", src):
            return True
        if re.fullmatch(r"window:[^\n]{1,80}", src):
            return True
        if src.startswith("stream:"):
            try:
                stream_url(src[7:])
                return True
            except ValueError:
                return False
        return False

    async def caster_set(self, req):
        c = self._caster()
        d = await req.json()
        changed_conn = False
        for key, typ in self.CASTER_KEYS.items():
            if key not in d:
                continue
            val = d[key]
            if key == "lang" and val not in ("de", "en"):
                continue
            if key == "style" and val not in ("hype", "analyst", "funny"):
                continue
            if key == "output" and val not in ("local", "overlay"):
                continue
            if key == "source" and not self._valid_source(str(val)):
                return web.json_response({"ok": False, "error": "Unbekannte Bildquelle"}, status=400)
            try:
                val = None if val in ("", None) else typ(val)
            except (TypeError, ValueError):
                continue
            if isinstance(val, str):
                val = val.strip()[:120]
            save_user_value(self.cfg, f"caster.{key}", val)
            changed_conn |= key in ("source", "twitch_channel", "fps")
        if "killfeed_region" in d:
            r = d["killfeed_region"]
            ok = isinstance(r, list) and len(r) == 4 and all(isinstance(v, (int, float)) and 0 <= v <= 1 for v in r)
            save_user_value(self.cfg, "caster.killfeed_region", [round(float(v), 4) for v in r] if ok else None)
            c.killfeed.region = c.opt("killfeed_region", None)
        if "lan_overlay" in d:
            save_user_value(self.cfg, "web.lan_overlay", bool(d["lan_overlay"]))
        if "lang" in d or "voice" in d:
            c._shouts_ready.clear()
            if c.active:
                c._bg_task(c.prepare_shouts())
        if changed_conn:
            c.restart()
        if "active" in d:
            self.coach.set_mode("cast" if d["active"] else "talk", announce=False)
        return web.json_response({"ok": True, **c.state()})

    async def caster_feed(self, req):
        c = self._caster()
        try:
            after = int(req.query.get("after", 0))
        except ValueError:
            after = 0
        return web.json_response({"active": c.active, "last_id": c._line_id,
                                  "lines": [x for x in c.lines if x["id"] > after][-10:]})

    async def caster_moment(self, req):
        c = self._caster()
        d = await req.json()
        kind = str(d.get("kind") or "big_play")
        if not c.active:
            return web.json_response({"ok": False, "error": "Caster-Modus ist aus"}, status=409)
        return web.json_response({"ok": True, "shout": c.moment(kind)})

    async def caster_learn(self, req):
        c = self._caster()
        if c.learn_state.get("running"):
            return web.json_response({"ok": False, "error": "Lernt gerade schon"}, status=409)
        if (req.content_type or "").startswith("multipart/"):
            from ..caster import CAST_DIR

            reader = await req.multipart()
            path, url, text = None, "", ""
            async for part in reader:
                if part.name == "file" and part.filename:
                    name = re.sub(r"[^\w.\-]", "_", Path(part.filename).name)[-80:] or "cast.mp4"
                    (CAST_DIR / "learn").mkdir(parents=True, exist_ok=True)
                    path = CAST_DIR / "learn" / name
                    with open(path, "wb") as f:
                        while chunk := await part.read_chunk():
                            f.write(chunk)
                elif part.name == "url":
                    url = (await part.text()).strip()
                elif part.name == "text":
                    text = await part.text()
        else:
            d = await req.json()
            path, url, text = None, str(d.get("url") or "").strip(), str(d.get("text") or "")
        if text.strip():
            res = await c.learn_text(text, "Text")
            return web.json_response(res)
        if url and not re.match(r"https://(www\.)?(youtube\.com|youtu\.be|twitch\.tv)/", url):
            return web.json_response({"ok": False, "error": "Nur YouTube- oder Twitch-Links"}, status=400)
        if not (path or url):
            return web.json_response({"ok": False, "error": "Datei, Link oder Text fehlt"}, status=400)
        c._bg_task(c.learn_media(path=path, url=url))
        return web.json_response({"ok": True, "started": True})

    async def caster_preview(self, _req):
        c = self._caster()
        frame = await asyncio.to_thread(c.frame) if c._feed is None else c.frame()
        if frame is None:
            raise web.HTTPNotFound(text="Kein Bild")
        import cv2

        h, w = frame.shape[:2]
        small = cv2.resize(frame, (640, max(1, int(h * 640 / w))), interpolation=cv2.INTER_AREA) if w > 640 else frame.copy()
        r = c.opt("killfeed_region", None)
        if r:
            sh, sw = small.shape[:2]
            x, y, rw, rh = (float(v) for v in r)
            cv2.rectangle(small, (int(x * sw), int(y * sh)), (int((x + rw) * sw), int((y + rh) * sh)), (255, 92, 138), 2)
        ok, buf = cv2.imencode(".jpg", small, [cv2.IMWRITE_JPEG_QUALITY, 70])
        return web.Response(body=buf.tobytes(), content_type="image/jpeg", headers={"Cache-Control": "no-store"})

    def _discord_status(self) -> dict:
        bot = getattr(self.coach, "bot", None)
        if bot is None:
            return {"enabled": False, "ready": False, "voice": ""}
        try:
            vc = next((v for v in bot.voice_clients if v.is_connected()), None)
            return {"enabled": True, "ready": bool(bot.is_ready()), "voice": vc.channel.name if vc else ""}
        except Exception:
            return {"enabled": True, "ready": False, "voice": ""}

    async def sessions(self, _req):
        return web.json_response(self.coach.db.sessions())

    async def new_session(self, _req):
        return web.json_response({"id": self.coach.db.new_session()})

    async def session(self, req):
        return web.json_response(self.coach.db.chat(int(req.match_info["sid"])))

    async def delete_session(self, req):
        self.coach.db.delete_session(int(req.match_info["sid"]))
        return web.json_response({"ok": True})

    async def chat(self, req):
        data = await req.json()
        text = (data.get("text") or "").strip()
        if not text:
            raise web.HTTPBadRequest(text="leer")
        sid = int(data.get("session_id") or self.coach.db.new_session())
        return web.json_response(await self._chat_reply(sid, text, data.get("web"), bool(data.get("speak"))))

    async def voice(self, req):
        if not self.coach.stt:
            return web.json_response({"error": "Spracherkennung nicht geladen (faster-whisper installiert?)"}, status=503)
        form = await req.post()
        raw = form["audio"].file.read()
        sid = int(form.get("session_id") or self.coach.db.new_session())
        audio = await asyncio.to_thread(decode_audio, raw)
        forced = self.cfg.get("language") if self.cfg.get("language") in ("de", "en") else None
        tr = await self.coach.stt.transcribe(audio, forced)
        if not tr.text:
            return web.json_response({"error": "Nichts verstanden"}, status=422)
        res = await self._chat_reply(sid, tr.text, form.get("web") == "true", True)
        res["text"] = tr.text
        return web.json_response(res)

    async def call(self, req):
        """Sprach-Call in der App (ohne Discord): ein gesprochener Satz rein, Yvaras gesprochene Antwort zurück."""
        if not self.coach.stt:
            return web.json_response({"error": "Spracherkennung nicht geladen"}, status=503)
        form = await req.post()
        raw = form["audio"].file.read()
        audio = await asyncio.to_thread(decode_audio, raw)
        forced = self.cfg.get("language") if self.cfg.get("language") in ("de", "en") else None
        tr = await self.coach.stt.transcribe(audio, forced)
        text = (tr.text or "").strip()
        if len(text) < 2:
            return web.json_response({"text": "", "replies": []})
        from ..i18n import norm_lang

        lang = norm_lang(tr.language, self.coach.lang)
        log.info("[app-call/%s] %s", lang, text)
        if self.cfg.get_path("audio.log_comms", True):
            try:
                self.coach.db.add_comms(self.coach.match_id, "app-call", lang, text)
            except Exception:
                pass
        said = await self.coach.call_turn(text, lang)
        replies = []
        for sent, lg in said:
            replies.append({"text": sent, "lang": lg, "audio": await self._speak_file(sent, lg)})
        return web.json_response({"text": text, "lang": lang, "replies": replies})

    async def memories(self, _req):
        return web.json_response(self.coach.db.memories())

    async def add_memory(self, req):
        data = await req.json()
        return web.json_response({"id": self.coach.db.add_memory(data["text"], "manual")})

    async def delete_memory(self, req):
        self.coach.db.delete_memory(int(req.match_info["mid"]))
        return web.json_response({"ok": True})

    async def teams(self, _req):
        return web.json_response({"active": self.coach.team.slug, "teams": self.coach.teams.list()})

    async def save_team(self, req):
        data = await req.json()
        name = (data.get("name") or "").strip()
        if not name:
            return web.json_response({"ok": False, "error": "Teamname fehlt"}, status=400)
        teams = self.coach.teams
        if data.get("slug"):  # bestehendes Team bearbeiten (auch umbenennen)
            team = teams.get(data["slug"])
            if team is None:
                return web.json_response({"ok": False, "error": "Team nicht gefunden"}, status=404)
            other = teams.get(name)
            if other is not None and other is not team:
                return web.json_response({"ok": False, "error": f"Es gibt schon ein Team „{name}“"}, status=409)
        else:
            team = teams.get(name)
            if team is not None and data.get("create"):
                return web.json_response({"ok": False, "error": f"Es gibt schon ein Team „{name}“"}, status=409)
            team = team or teams.create(name)
        if "players" in data:
            team.meta["players"] = [p for p in data["players"] if p.get("ingame")]
        if "notes" in data:
            team.meta["notes"] = data["notes"]
        team.meta["name"] = name
        team.save()
        return web.json_response({"ok": True, **team.info()})

    async def activate_team(self, req):
        data = await req.json()
        if self.coach.teams.get(data.get("slug")) is None:
            return web.json_response({"ok": False, "error": "Team nicht gefunden"}, status=404)
        ok = self.coach.set_team(data["slug"])
        if not ok:
            return web.json_response({"ok": False, "error": "Während eines Matches kein Teamwechsel"}, status=409)
        return web.json_response({"ok": True, "team": self.coach.team.info()})

    async def save_settings(self, req):
        data = await req.json()
        from ..safety import allowed

        if data.get("llm.provider") == "claude" and not allowed(self.cfg, "cloud_ai"):
            data.pop("llm.provider")  # Privatsphäre: Cloud-KI nur mit privacy.allow_cloud_ai: true
        if data.get("tts.engine") == "edge" and not allowed(self.cfg, "cloud_voice"):
            data.pop("tts.engine")
        if data.get("web.research") and not allowed(self.cfg, "web_search"):
            data["web.research"] = False
        for key, typ in SETTINGS.items():
            if key in data:
                val = data[key]
                val = None if val in ("", None) else typ(val)
                save_user_value(self.cfg, key, val)
        if any(k.startswith("llm.") for k in data):
            self.swap_llm()
            self.coach._brain_cache = None
        gm = getattr(self.coach, "gaming_mode", None)
        if gm is not None and (any(k.startswith(("llm.", "performance.")) for k in data)):
            if gm.active:  # neues Modell/Einstellung -> Gaming-Modus frisch anwenden
                gm.active, gm._big = False, None
            asyncio.ensure_future(gm.tick())
        if data.get("coaching_mode") in self.coach.MODES:
            self.coach.set_mode(data["coaching_mode"], announce=False)
        if any(k.startswith("tts.") for k in data) and hasattr(self.tts, "reset_xtts"):
            self.tts.reset_xtts()
        return web.json_response({"ok": True})

    def swap_llm(self) -> None:
        """KI-Modell ohne Neustart wechseln (Claude <-> Ollama/LM Studio)."""
        from ..brain.llm import make_llm

        llm = make_llm(self.cfg)
        if hasattr(llm, "ensure_model"):
            asyncio.ensure_future(llm.ensure_model())  # neues Modell ggf. gleich herunterladen
        for obj in (self.coach, getattr(self.coach, "studier", None), getattr(self.coach, "autolearn", None)):
            if obj is not None and hasattr(obj, "llm"):
                obj.llm = llm

    async def update_check(self, _req):
        from .. import updater

        try:
            info = await asyncio.to_thread(updater.check, self.cfg)
        except Exception as e:
            info = {"available": False, "error": True, "message": f"Prüfung fehlgeschlagen: {e}"}
        info["installed"] = updater.installed()
        return web.json_response(info)

    async def update_apply(self, _req):
        from .. import updater

        if self.coach.tracker.state.active:
            return web.json_response({"error": "Bitte erst das Match beenden."}, status=409)
        try:
            res = await asyncio.to_thread(updater.update, self.cfg)
        except Exception as e:
            return web.json_response({"error": str(e)}, status=500)
        if res.get("updated"):
            asyncio.get_running_loop().call_later(1.5, updater.restart_now)
        return web.json_response(res)

    async def match_start(self, req):
        d = await req.json()
        await self.coach.start_match(d.get("mode", "hardpoint"), d.get("map", ""), d.get("opponent", ""))
        return web.json_response({"ok": True})

    async def match_end(self, req):
        d = await req.json()
        ours = d.get("ours")
        enemy = d.get("enemy")
        await self.coach.end_match(int(ours) if ours not in (None, "") else None, int(enemy) if enemy not in (None, "") else None)
        return web.json_response({"ok": True})

    async def focus(self, req):
        d = await req.json()
        self.coach.set_focus(d.get("player"))
        return web.json_response({"focus": self.coach.focus_player})

    async def set_mode(self, req):
        d = await req.json()
        if d.get("mode") not in self.coach.MODES:
            raise web.HTTPBadRequest(text="mode: talk | live | observe | teach | cast")
        self.coach.set_mode(d["mode"], announce=bool(d.get("announce", True)))
        return web.json_response({"mode": self.coach.mode})

    async def get_callouts(self, req):
        c = self.coach
        mp = req.query.get("map") or c.tracker.state.map or ""
        maps = set(c.library.callout_maps() if c.library else [])
        maps |= {str(v.get("name", k)) for k, v in (c.kb.maps or {}).items() if k != "example_map"}
        maps = sorted(maps)
        return web.json_response({"map": mp, "maps": maps, "callouts": c.callouts(mp) if mp else []})

    async def add_callout(self, req):
        d = await req.json()
        aliases = d.get("aliases") or []
        if isinstance(aliases, str):
            aliases = [a.strip() for a in aliases.split(",") if a.strip()]
        ok = self.coach.add_callout(d.get("map", ""), d.get("name", ""), d.get("zone", ""), aliases, d.get("note", ""))
        if not ok:
            raise web.HTTPBadRequest(text="map und name nötig")
        return web.json_response({"ok": True})

    async def delete_callout(self, req):
        if self.coach.library:
            self.coach.library.delete_callout(int(req.match_info["cid"]))
            self.coach._co_cache = None
        return web.json_response({"ok": True})

    async def lesson(self, req):
        d = await req.json()
        action = d.get("action", "start")
        c = self.coach
        if action == "next":
            c.lesson_next()
        elif action == "repeat":
            c.lesson_repeat()
        elif action == "stop":
            c.lesson = None
        else:
            topic = (d.get("topic") or "").strip()
            if not topic:
                raise web.HTTPBadRequest(text="topic fehlt")
            res = await c.start_lesson(topic)
            if not res:
                return web.json_response({"ok": False}, status=502)
            return web.json_response({"ok": True, "lesson": res})
        return web.json_response({"ok": True})

    async def get_feeds(self, _req):
        f = self.coach.feeds
        return web.json_response({"feeds": f.status() if f else [], "auto_focus": self.cfg.get_path("feeds_auto_focus", True)})

    async def set_feeds(self, req):
        from ..vision.feeds import FeedManager, FeedSpec

        d = await req.json()
        specs = [FeedSpec.from_dict(x) for x in d.get("feeds", []) if x.get("player") and x.get("ref") not in (None, "")]
        for sp in specs:  # Zahlen/Listen aus dem Formular umwandeln
            if sp.kind == "monitor":
                sp.ref = int(sp.ref)
            elif sp.kind == "region" and isinstance(sp.ref, str):
                sp.ref = [int(v) for v in sp.ref.replace(",", " ").split()]
        save_user_value(self.cfg, "feeds", [s.to_dict() for s in specs])
        save_user_value(self.cfg, "feeds_auto_focus", bool(d.get("auto_focus", True)))
        if self.coach.feeds is None:
            self.coach.feeds = FeedManager()
        await asyncio.to_thread(self.coach.feeds.set, specs)
        return web.json_response({"feeds": self.coach.feeds.status()})

    async def plan(self, req):
        d = await req.json()
        plan = await self.coach.make_plan(d.get("mode", "hardpoint"), d.get("map", ""), d.get("opponent", ""))
        return web.json_response({"plan": plan})

    async def profile(self, req):
        return web.json_response({"text": self.coach.profile_text(req.query.get("kind", "player"), req.query.get("name", ""))})

    async def training(self, _req):
        return web.json_response({"plan": await self.coach.training_plan()})

    async def scout(self, req):
        d = await req.json()
        return web.json_response({"report": await self.coach.scout(d["opponent"])})

    async def clip(self, req):
        d = await req.json()
        return web.json_response({"id": self.coach.save_clip(d.get("note", ""))})

    async def timeout(self, _req):
        self.coach.timeout_reset()
        return web.json_response({"ok": True})

    async def clips(self, _req):
        return web.json_response(self.coach.db.clips(limit=50))

    # ------------------------------------------------------------ lernen
    async def study_start(self, req):
        d = await req.json()
        if (err := await self._need_brain()) is not None:
            return err
        st = self.coach.studier
        if not st:
            return web.json_response({"error": "Lern-Modus nicht verfügbar"}, status=503)
        try:
            sid = st.start(d.get("source", "screen").strip(), d.get("title", ""), d.get("mode", ""), d.get("note", ""),
                           own_team=bool(d.get("own_team")))
        except RuntimeError as e:
            return web.json_response({"error": str(e)}, status=409)
        return web.json_response({"source_id": sid})

    async def study_stop(self, _req):
        if self.coach.studier:
            self.coach.studier.stop()
        return web.json_response({"ok": True})

    async def library(self, req):
        lib = self.coach.library
        if not lib:
            return web.json_response({"lessons": [], "playbooks": {}, "sources": [], "status": {}})
        return web.json_response({
            "status": self.coach.studier.status if self.coach.studier else {},
            "count": lib.count(),
            "lessons": lib.lessons(req.query.get("mode") or None, limit=100),
            "playbooks": lib.playbooks(),
            "sources": lib.sources(20),
            "autolearn": self.coach.autolearn.status if self.coach.autolearn else {},
            "queue": lib.queue(30),
            "ideas": lib.ideas(self.coach.team.name, 20),
            "totals": lib.totals(),
            "history": lib.history(40),
        })

    # ------------------------------------------------------------ Zuschauen
    async def watch_state(self, _req):
        import sys

        c = self.coach
        windows: list[str] = []
        if sys.platform == "win32":
            try:
                import pygetwindow as gw

                skip = ("vyra", "program manager", "settings", "einstellungen", "nvidia", "task")
                windows = sorted({t for t in gw.getAllTitles() if t.strip() and not any(x in t.lower() for x in skip)})[:40]
            except Exception as e:
                log.debug("Fensterliste: %s", e)
        monitors = 1
        try:
            monitors = max(1, len(c.capture._sct.monitors) - 1) if c.capture else 0
        except Exception:
            pass
        return web.json_response({**{k: v for k, v in c.watch.items()}, "available": c.capture is not None,
                                  "windows": windows, "monitors": monitors,
                                  "log": [{"ts": ts, "text": o} for ts, o in c.watch_log[-8:]]})

    async def watch_set(self, req):
        d = await req.json()
        if "comment" in d:
            self.coach.watch["comment"] = bool(d["comment"])
        st = self.coach.set_watch(bool(d.get("on")), d.get("source") or None, announce=False)
        if d.get("on") and self.coach.capture is None:
            return web.json_response({"error": "Bildschirmaufnahme ist aus (Yvara AI mit Bildanalyse starten)"}, status=400)
        return web.json_response({"ok": True, **st})

    async def watch_preview(self, _req):
        c = self.coach
        frame = c.last_frame
        if frame is None and c.capture is not None:
            try:
                frame = await asyncio.to_thread(c.capture.grab)
            except Exception:
                frame = None
        if frame is None:
            raise web.HTTPNotFound(text="Kein Bild")
        import cv2

        h, w = frame.shape[:2]
        small = cv2.resize(frame, (640, max(1, int(h * 640 / w))), interpolation=cv2.INTER_AREA) if w > 640 else frame
        ok, buf = cv2.imencode(".jpg", small, [cv2.IMWRITE_JPEG_QUALITY, 70])
        return web.Response(body=buf.tobytes(), content_type="image/jpeg", headers={"Cache-Control": "no-store"})

    # --------------------------------------------------- Heatmaps & Strat-Board
    def _strat_dir(self) -> Path:
        d = self.coach.team.dir / "strats"
        d.mkdir(parents=True, exist_ok=True)
        return d

    async def maps(self, _req):
        names = {str(v.get("name") or k) for k, v in (self.coach.kb.maps or {}).items() if k != "example_map"}
        if self.coach.library:
            names |= set(self.coach.library.callout_maps()) if hasattr(self.coach.library, "callout_maps") else set()
        names |= {m["map"] for m in self.coach.mapheat.maps()}
        if self.coach.tracker.state.map:
            names.add(self.coach.tracker.state.map)
        return web.json_response(sorted(n for n in names if n))

    async def heatmaps(self, _req):
        return web.json_response(self.coach.mapheat.maps())

    def _live_heat(self, map_name: str) -> dict | None:
        """Laufendes Match zählt schon mit, auch bevor es gespeichert ist."""
        c = self.coach
        saved = c.mapheat.load(map_name) or {}
        if c.tracker.state.active and (c.tracker.state.map or "").lower() == map_name.lower():
            merged = {k: saved.get(k, 0) + c.minimap.heat[k] for k in c.mapheat.LAYERS}
            if c.minimap.last_crop is not None:
                merged["bg"] = c.minimap.last_crop
            elif "bg" in saved:
                merged["bg"] = saved["bg"]
            return merged
        return saved or None

    async def heatmap_png(self, req):
        map_name = req.query.get("map", "")
        data = self._live_heat(map_name)
        png = await asyncio.to_thread(self.coach.mapheat.render, map_name, req.query.get("layer", "all"), data) if data else None
        if not png:
            raise web.HTTPNotFound(text="Noch keine Heatmap für diese Map")
        return web.Response(body=png, content_type="image/png", headers={"Cache-Control": "no-store"})

    async def heatmap_discord(self, req):
        d = await req.json()
        map_name, layer = d.get("map", ""), d.get("layer", "all")
        data = self._live_heat(map_name)
        png = await asyncio.to_thread(self.coach.mapheat.render, map_name, layer, data) if data else None
        if not png:
            return web.json_response({"error": "Noch keine Heatmap für diese Map"}, status=404)
        out = self.coach.team.reports / f"heatmap_{re.sub(r'[^a-z0-9]+', '_', map_name.lower())}.png"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(png)
        await self.coach.notify(f"🔥 Heatmap **{map_name}** – blau = wir, rot = Gegner, gelb = Kampfzonen", out)
        return web.json_response({"ok": True})

    async def strats(self, _req):
        import json as _json

        out = []
        for f in sorted(self._strat_dir().glob("*.json"), key=lambda p: -p.stat().st_mtime):
            try:
                d = _json.loads(f.read_text(encoding="utf-8"))
                d.pop("shapes", None) if _req.query.get("full") != "1" else None
                out.append(d)
            except Exception:
                continue
        return web.json_response(out)

    async def strat_save(self, req):
        import base64
        import json as _json
        import time as _time

        d = await req.json()
        sid = re.sub(r"[^a-z0-9]", "", str(d.get("id") or "")) or f"{int(_time.time() * 1000):x}"
        png = str(d.pop("png", "") or "")
        if png.startswith("data:image/png;base64,"):
            (self._strat_dir() / f"{sid}.png").write_bytes(base64.b64decode(png.split(",", 1)[1]))
        rec = {"id": sid, "name": str(d.get("name") or "Strat")[:80], "map": str(d.get("map") or "")[:60],
               "mode": str(d.get("mode") or "")[:20], "notes": str(d.get("notes") or "")[:2000],
               "shapes": d.get("shapes") or [], "ts": _time.time()}
        (self._strat_dir() / f"{sid}.json").write_text(_json.dumps(rec, ensure_ascii=False), encoding="utf-8")
        return web.json_response({"ok": True, "id": sid})

    def _strat_file(self, req, ext: str) -> Path:
        sid = re.sub(r"[^a-z0-9]", "", req.match_info["sid"])
        return self._strat_dir() / f"{sid}.{ext}"

    async def strat_png(self, req):
        f = self._strat_file(req, "png")
        if not f.exists():
            raise web.HTTPNotFound()
        return web.FileResponse(f, headers={"Cache-Control": "no-store"})

    async def strat_delete(self, req):
        for ext in ("json", "png"):
            self._strat_file(req, ext).unlink(missing_ok=True)
        return web.json_response({"ok": True})

    async def strat_discord(self, req):
        import json as _json

        f, png = self._strat_file(req, "json"), self._strat_file(req, "png")
        if not f.exists():
            return web.json_response({"error": "Strat nicht gefunden"}, status=404)
        d = _json.loads(f.read_text(encoding="utf-8"))
        text = f"🗺️ **{d['name']}**" + (f" – {d['map']}" if d.get("map") else "") + (f" ({d['mode']})" if d.get("mode") else "")
        if d.get("notes"):
            text += f"\n{d['notes']}"
        await self.coach.notify(text, png if png.exists() else None)
        return web.json_response({"ok": True})

    def _bg_file(self, map_name: str) -> Path:
        d = self._strat_dir() / "bg"
        d.mkdir(parents=True, exist_ok=True)
        return d / f"{re.sub(r'[^a-z0-9_-]+', '_', (map_name or 'x').lower())}.png"

    async def strat_bg(self, req):
        map_name = req.query.get("map", "")
        f = self._bg_file(map_name)
        if f.exists():
            return web.FileResponse(f, headers={"Cache-Control": "no-store"})
        data = self._live_heat(map_name) or {}
        bg = data.get("bg")
        if bg is None or not getattr(bg, "size", 0):
            raise web.HTTPNotFound(text="Kein Kartenbild – Bild hochladen oder Match mit Minimap spielen")
        import cv2

        ok, buf = cv2.imencode(".png", cv2.resize(bg, (600, 600), interpolation=cv2.INTER_CUBIC))
        return web.Response(body=buf.tobytes(), content_type="image/png", headers={"Cache-Control": "no-store"})

    async def strat_bg_upload(self, req):
        form = await req.post()
        raw = form["image"].file.read()
        out = self._bg_file(str(form.get("map") or ""))

        def convert() -> None:
            import cv2

            img = cv2.imdecode(np.frombuffer(raw, np.uint8), cv2.IMREAD_COLOR)
            if img is None:
                raise ValueError("kein Bild")
            h, w = img.shape[:2]
            k = 600 / max(h, w)
            img = cv2.resize(img, (max(1, int(w * k)), max(1, int(h * k))), interpolation=cv2.INTER_AREA)
            canvas = np.full((600, 600, 3), 28, np.uint8)
            y, x = (600 - img.shape[0]) // 2, (600 - img.shape[1]) // 2
            canvas[y:y + img.shape[0], x:x + img.shape[1]] = img
            cv2.imwrite(str(out), canvas)

        try:
            await asyncio.to_thread(convert)
        except Exception as e:
            return web.json_response({"error": f"Bild konnte nicht gelesen werden: {e}"}, status=400)
        return web.json_response({"ok": True})

    # ------------------------------------------------------------- Stimmen
    async def voices(self, _req):
        from ..audio import tts as T

        loaded = self.tts.xtts_speakers()
        speakers = [{"id": n, "label": d} for n, d in T.XTTS_SPEAKERS if not loaded or n in loaded]  # nur Standard + E-Girl
        return web.json_response({
            "engine": self.tts.engine,
            "xtts_installed": T.xtts_installed(), "xtts_ready": T.xtts_ready(), "xtts_status": T.xtts_status(),
            "xtts_error": self.tts._xtts_failed, "xtts_speakers": speakers, "last_used": self.tts.last_used,
            "custom": [{"id": f"custom:{n}", "label": f"Eigene: {n}", "silent": _wav_silent(T.CUSTOM_DIR / f"{n}.wav"),
                        "style": T.voice_style(n)}
                       for n in T.custom_voices()],
            "piper": [{"id": n, "label": d} for n, d in T.PIPER_PRESETS], "slots": T.MAX_VOICE_SLOTS,
        })

    async def voice_preview(self, req):
        d = await req.json()
        lang = d.get("lang") if d.get("lang") in ("de", "en") else "de"
        opts = {k: d.get(k) for k in ("engine", "xtts_voice", "xtts_voice_en", "piper_de", "piper_en", "piper_speed", "same_voice", "supertonic_voice")}
        text = (d.get("text") or PREVIEW[lang])[:300]
        try:
            info = await self.tts.synth_info(text, lang, opts)
        except Exception as e:
            return web.json_response({"error": str(e)}, status=500)
        return web.json_response({"audio": f"/audio/{info['path'].name}", "engine": info["engine"], "voice": info["voice"],
                                  "wanted": info["wanted"], "engine_error": info["errors"].get(info["wanted"])})

    async def voice_test(self, _req):
        """„Stimme testen“: zeigt, welche Engine und Stimme Yvara AI im Spiel WIRKLICH benutzt – und warum nicht."""
        from ..audio import tts as T

        self.tts.reset_xtts()  # nach dem Einrichten ohne Neustart erneut versuchen
        st = await asyncio.to_thread(T.xtts_status)
        res = {"engine_setting": self.tts.engine, "xtts": st, "langs": {}}
        for lang in ("de", "en"):
            want = self.tts.xtts_voice(lang) if self.tts.engine == "xtts" else ""
            row = {"wanted_voice": want}
            if want.startswith("custom:"):
                row["voice_file"] = (T.CUSTOM_DIR / f"{T.slug_voice(want[7:])}.wav").exists()
            try:
                info = await self.tts.synth_info(PREVIEW[lang], lang)
                row.update(engine=info["engine"], voice=info["voice"], errors=info["errors"],
                           audio=f"/audio/{info['path'].name}")
            except Exception as e:
                row["errors"] = {"alle": str(e)}
            res["langs"][lang] = row
        log.info("Stimmtest: %s", {k: (v.get("engine"), v.get("voice"), v.get("errors")) for k, v in res["langs"].items()})
        return web.json_response(res)

    async def voice_upload(self, req):
        from ..audio import tts as T

        form = await req.post()
        if str(form.get("consent")) != "true":
            return web.json_response({"error": "Bitte bestätigen, dass es deine Stimme ist oder die Person einverstanden ist."},
                                     status=400)
        name = T.slug_voice(str(form.get("name") or "meine_stimme"))
        if name not in T.custom_voices() and len(T.custom_voices()) >= T.MAX_VOICE_SLOTS:
            return web.json_response({"error": f"Alle {T.MAX_VOICE_SLOTS} Stimm-Plätze sind belegt – lösche erst eine Stimme "
                                               "(✕) oder nimm denselben Namen, um sie zu ersetzen."}, status=400)
        parts = [f.file.read() for f in form.getall("audio", []) if hasattr(f, "file")]
        raw = b"".join(parts)
        T.CUSTOM_DIR.mkdir(parents=True, exist_ok=True)
        out = T.CUSTOM_DIR / f"{name}.wav"

        def convert() -> float:
            # Rohaufnahme erst als Datei ablegen (Browser-WebM über eine Pipe ist unzuverlässig), dann dekodieren
            import tempfile

            # Mehrere Dateien möglich (z. B. mehrere kurze WhatsApp-Sprachnachrichten .opus/.ogg/.mp3/.m4a)
            pieces = []
            for part in parts:
                with tempfile.NamedTemporaryFile(suffix=".bin", delete=False) as tf:
                    tf.write(part)
                    src = tf.name
                try:
                    pcm = subprocess.run(["ffmpeg", "-v", "error", "-i", src, "-vn", "-f", "f32le", "-ac", "1", "-ar", "24000",
                                          "pipe:1"], capture_output=True, check=True).stdout
                finally:
                    Path(src).unlink(missing_ok=True)
                piece = prepare_voice_sample(np.frombuffer(pcm, dtype=np.float32), 24000)
                if len(piece):
                    pieces += [piece, np.zeros(int(0.3 * 24000), np.float32)]
            audio = np.concatenate(pieces)[: 30 * 24000] if pieces else np.zeros(0, np.float32)
            if speech_seconds(audio, 24000) < 4:
                raise ValueError("In der Aufnahme ist kaum Sprache zu hören (stumm oder falsches Mikrofon?)")
            with wave.open(str(out), "wb") as wf:
                wf.setnchannels(1)
                wf.setsampwidth(2)
                wf.setframerate(24000)
                wf.writeframes((audio * 32767).astype("<i2").tobytes())
            return len(audio) / 24000

        try:
            secs = await asyncio.to_thread(convert)
        except Exception as e:
            out.unlink(missing_ok=True)
            log.warning("Stimmaufnahme nicht lesbar (%d Bytes): %s", len(raw), e)
            return web.json_response({"error": f"Aufnahme konnte nicht gelesen werden: {e}"}, status=400)
        log.info("Eigene Stimme „%s“ gespeichert: %.1f s (Upload %d KB)", name, secs, len(raw) // 1024)
        if secs < 6:
            out.unlink(missing_ok=True)
            return web.json_response({"error": f"Zu kurz ({secs:.0f} s Sprache erkannt) – bitte mindestens 10 Sekunden: "
                                               "länger aufnehmen oder mehrere Sprachnachrichten zusammen auswählen."}, status=400)
        vid = f"custom:{name}"
        use = str(form.get("use") or "both")
        if use in ("both", "de"):
            save_user_value(self.cfg, "tts.xtts_voice", vid)
        if use == "both":
            save_user_value(self.cfg, "tts.xtts_voice_en", "")
        if use == "en":
            save_user_value(self.cfg, "tts.xtts_voice_en", vid)
        if use in ("both", "de", "en"):
            save_user_value(self.cfg, "tts.engine", "xtts")
        self.tts.forget_voice(f"custom:{name}")
        self.tts.reset_xtts()
        st = T.xtts_status()
        return web.json_response({"ok": True, "id": f"custom:{name}", "seconds": round(secs, 1), "xtts_ready": st["ready"],
                                  "xtts_problem": st.get("problem")})

    async def voice_style(self, req):
        from ..audio import tts as T

        d = await req.json()
        name = T.slug_voice(str(d.get("name") or ""))
        if name not in T.custom_voices():
            return web.json_response({"error": "Stimme nicht gefunden"}, status=404)
        T.set_voice_style(name, str(d.get("style") or ""))
        return web.json_response({"ok": True, "style": T.voice_style(name)})

    async def voice_delete(self, req):
        from ..audio import tts as T

        name = T.slug_voice(req.match_info["name"])
        (T.CUSTOM_DIR / f"{name}.wav").unlink(missing_ok=True)
        self.tts.forget_voice(f"custom:{name}")
        return web.json_response({"ok": True})

    async def autolearn_start(self, req):
        d = await req.json()
        if (err := await self._need_brain()) is not None:
            return err
        self.coach.autolearn.start(d.get("topics", ""), d.get("links") or [])
        return web.json_response({"ok": True})

    async def autolearn_stop(self, _req):
        self.coach.autolearn.stop()
        return web.json_response({"ok": True})

    async def enqueue(self, req):
        d = await req.json()
        return web.json_response({"added": self.coach.autolearn.add_links(d.get("links") or [])})

    async def delete_lesson(self, req):
        self.coach.library.delete_lesson(int(req.match_info["lid"]))
        return web.json_response({"ok": True})

    async def consolidate(self, req):
        d = await req.json()
        await self.coach.studier.consolidate([d.get("mode", "hardpoint")])
        return web.json_response({"playbook": self.coach.library.playbook(d.get("mode", "hardpoint"))})


def _strip_md(text: str) -> str:
    text = re.sub(r"```.*?```", " ", text, flags=re.S)
    text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)
    return re.sub(r"[#*_`>|-]{1,}", " ", text)
