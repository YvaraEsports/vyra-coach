"""Gaming-Modus: solange Call of Duty läuft, lässt Yvara AI der Grafikkarte Luft.

Grund: CoD, das große KI-Modell (gemma3:12b ≈ 9–10 GB), XTTS und Whisper teilen sich 16 GB Grafikspeicher.
Läuft der Speicher über, lagert Windows in den Arbeitsspeicher aus -> CoD fällt auf wenige FPS.

Im Gaming-Modus:
  * kleines, schnelles KI-Modell (Standard gemma3:4b, ≈ 3–4 GB) statt des großen – das große wird entladen
  * keine KI-Bildanalyse (nur die sparsame HUD-Erkennung auf der CPU, 1×/s)
  * Hintergrund-Lernen pausiert
  * Yvara AI läuft mit niedrigerer CPU-Priorität
Einstellung performance.gaming_mode: auto (Spiel erkennen) | on | off
"""
from __future__ import annotations

import asyncio
import logging
import re
import sys

log = logging.getLogger(__name__)

GAME_RE = re.compile(r"^(cod\d*|codhq|cod_\w+|blackops\w*|modernwarfare\w*|mw\d*|iw\d+\w*|sp\d+\w*)\.exe$", re.I)


def running_game(extra: list[str] | None = None) -> str | None:
    """Name des laufenden CoD-Prozesses (oder None)."""
    try:
        import psutil
    except Exception:
        return None
    extra_l = {e.lower() for e in (extra or [])}
    try:
        for p in psutil.process_iter(["name"]):
            n = (p.info.get("name") or "")
            if GAME_RE.match(n) or n.lower() in extra_l:
                return n
    except Exception:
        return None
    return None


class GamingMode:
    def __init__(self, cfg, coach) -> None:
        self.cfg = cfg
        self.coach = coach
        self.active = False
        self.game: str | None = None
        self._big: tuple[str, int] | None = None

    def c(self, key, default=None):
        return self.cfg.get_path(f"performance.{key}", default)

    @property
    def setting(self) -> str:
        return str(self.c("gaming_mode", "auto") or "auto").lower()

    def detect(self) -> str | None:
        if self.setting == "off":
            return None
        if self.setting == "on":
            return "manuell"
        return running_game(list(self.c("game_processes", []) or []))

    async def tick(self) -> None:
        game = await asyncio.to_thread(self.detect)
        if game and not self.active:
            await self.enter(game)
        elif not game and self.active:
            await self.leave()

    async def run(self) -> None:
        # kleines Modell vorab herunterladen, solange noch nicht gespielt wird (sonst Download mitten im Match)
        llm = self.coach.llm
        small = str(self.c("game_model", "gemma3:4b") or "")
        if small and hasattr(llm, "ensure_model") and self.setting != "off":
            try:
                await llm.ensure_model(small)
            except TypeError:
                pass
        while True:
            try:
                await self.tick()
            except Exception as e:
                log.debug("Gaming-Modus: %s", e)
            await asyncio.sleep(float(self.c("check_interval_s", 5)))

    async def enter(self, game: str) -> None:
        self.active, self.game = True, game
        self.coach.gaming = True
        llm = self.coach.llm
        small = str(self.c("game_model", "gemma3:4b") or "")
        if small and hasattr(llm, "model") and getattr(llm, "provider", "") == "ollama" and llm.model != small:
            self._big = (llm.model, getattr(llm, "num_ctx", 8192))
            old = llm.model
            llm.model, llm.num_ctx = small, int(self.c("game_num_ctx", 4096))
            await llm.unload(old)
            await llm.warmup()
        self._set_priority(low=True)
        log.info("🎮 Gaming-Modus AN (%s): KI %s, keine KI-Bildanalyse, Lernen pausiert – damit CoD flüssig läuft",
                 game, getattr(llm, "model", "?"))

    async def leave(self) -> None:
        self.active, self.game = False, None
        self.coach.gaming = False
        llm = self.coach.llm
        if self._big and hasattr(llm, "model"):
            small = llm.model
            llm.model, llm.num_ctx = self._big
            self._big = None
            await llm.unload(small)
            asyncio.ensure_future(llm.warmup())
        self._set_priority(low=False)
        log.info("Gaming-Modus AUS – volle KI wieder aktiv")

    @staticmethod
    def _set_priority(low: bool) -> None:
        try:
            import psutil

            p = psutil.Process()
            if sys.platform == "win32":
                p.nice(psutil.BELOW_NORMAL_PRIORITY_CLASS if low else psutil.NORMAL_PRIORITY_CLASS)
            else:
                p.nice(10 if low else 0)
        except Exception:
            pass

    def status(self) -> dict:
        return {"active": self.active, "game": self.game, "setting": self.setting,
                "model": getattr(self.coach.llm, "model", None)}
