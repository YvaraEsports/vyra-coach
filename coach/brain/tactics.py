"""Live-Taktik: Timer-Calls, Gegner-Tendenzen und Heatmaps pro Map.

Alles rein aus dem, was Yvara AI hört (Comms) und sieht (Minimap/HUD) – kein Zugriff auf das Spiel.
Daten pro Team unter data/teams/<team>/ (bleiben bei Updates erhalten).
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from collections import Counter
from pathlib import Path
from typing import Callable

import numpy as np

log = logging.getLogger(__name__)


def _norm(text: str) -> str:
    return " ".join(re.sub(r"[^\w\s]", " ", text.lower()).split())


# ------------------------------------------------------------------ Erkennung aus Sprache
PLANT_RE = re.compile(r"\b(geplant(et|ed)|geplanted|planted|plant (ist )?(drin|down|rein)|"
                      r"bomb\w* (ist )?(down|liegt|gelegt|drin|geplant\w*)|bomb is (down|planted)|they planted)\b")
DEFUSE_RE = re.compile(r"\b(defused|entschärft|entschaerft|defuse (ist )?(drin|durch)|bombe ist weg)\b")
ENEMY_UAV_RE = re.compile(r"\b((gegner|die|enemy|gegnerisch\w*) (haben |hat |has |have )?(ne |eine |an |a )?(uav|drohne|spy ?plane)|"
                          r"(uav|drohne) (vom gegner|von denen|oben bei denen)|enemy uav)\b")
TIMER_RE = re.compile(r"\b(timer|countdown|stoppuhr|uhr)\b.*?\b(\d{1,3})\s*(s|sek\w*|sec\w*|min\w*)?\b")
TIMER_RE2 = re.compile(r"\b(\d{1,3})\s*(s|sek\w*|sec\w*|min\w*)\s*(timer|countdown)\b")
HILL_RE = re.compile(r"\b(hill|hardpoint|punkt)( ist)? (neu|rotiert|gewechselt|rotated|moved|timer|uhr|sync\w*)\b|"
                     r"\bneuer (hill|punkt)\b|\bnew hill\b|\bhill timer\b")
STOP_TIMER_RE = re.compile(r"\b(timer|timers|uhr|countdown)\w* (aus|stop\w*|weg|off|beenden)\b|\bstop (the )?timers?\b")


NUM_WORDS = {"zehn": 10, "ten": 10, "fünfzehn": 15, "fuenfzehn": 15, "fifteen": 15, "zwanzig": 20, "twenty": 20,
             "dreißig": 30, "dreissig": 30, "thirty": 30, "vierzig": 40, "forty": 40, "fünfundvierzig": 45,
             "fuenfundvierzig": 45, "sechzig": 60, "sixty": 60, "neunzig": 90, "ninety": 90, "eine": 1, "einer": 1,
             "ein": 1, "one": 1, "a": 1, "zwei": 2, "two": 2, "drei": 3, "three": 3, "fünf": 5, "fuenf": 5, "five": 5}


def detect_timer_cmd(text: str) -> tuple[str, float] | None:
    """Sprachbefehl an Yvara AI: „Timer 30 Sekunden“, „Hill ist neu“, „Timer aus“, „Bombe liegt“."""
    n = " ".join(str(NUM_WORDS.get(w, w)) for w in _norm(text).split())
    if STOP_TIMER_RE.search(n):
        return ("stop", 0)
    if HILL_RE.search(n):
        return ("hill", 0)
    if PLANT_RE.search(n):
        return ("bomb", 0)
    m = TIMER_RE.search(n) or TIMER_RE2.search(n)
    if m:
        num_unit = (m.group(2), m.group(3)) if m.re is TIMER_RE else (m.group(1), m.group(2))
        secs = float(num_unit[0]) * (60 if (num_unit[1] or "").startswith("min") else 1)
        if 3 <= secs <= 1800:
            return ("timer", secs)
    return None


# ------------------------------------------------------------------ Timer
class TimerCalls:
    """Countdown-Calls: Bomben-Timer (SnD), Hill-Rotation (Hardpoint), Gegner-UAV, eigener Timer."""

    def __init__(self, cfg, call: Callable[[str, int], None], lang_fn: Callable[[], str]) -> None:
        self.cfg = cfg
        self.call = call          # call(text, priority)
        self.lang = lang_fn
        self.tasks: dict[str, asyncio.Task] = {}

    def _v(self, key: str, default: float) -> float:
        try:
            return float(self.cfg.get_path(f"timers.{key}", default))
        except (TypeError, ValueError):
            return default

    def _say(self, de: str, en: str, priority: int = 3) -> None:
        self.call(de if self.lang() == "de" else en, priority)

    def _start(self, name: str, coro) -> None:
        self.cancel(name)
        try:
            self.tasks[name] = asyncio.get_running_loop().create_task(coro)
        except RuntimeError:  # kein Event-Loop (z. B. in Tests ohne asyncio)
            coro.close()

    def cancel(self, name: str) -> bool:
        t = self.tasks.pop(name, None)
        if t and not t.done():
            t.cancel()
            return True
        return False

    def stop_all(self) -> None:
        for name in list(self.tasks):
            self.cancel(name)

    @property
    def active(self) -> list[str]:
        return [n for n, t in self.tasks.items() if not t.done()]

    async def _countdown(self, total: float, marks: list[tuple[float, str, str]], start: float | None = None) -> None:
        """marks: (Sekunden übrig, Text de, Text en), absteigend."""
        start = start or time.monotonic()
        for left, de, en in sorted(marks, key=lambda m: -m[0]):
            wait = start + total - left - time.monotonic()
            if wait < -1.5:
                continue  # Marke schon vorbei
            if wait > 0:
                await asyncio.sleep(wait)
            self._say(de, en)

    # --------------------------------------------------------------- Bombe
    def bomb_planted(self) -> None:
        total = self._v("bomb_s", 45)
        defuse = self._v("defuse_s", 7.5)
        last = defuse + 3
        marks = [(total, "Bombe liegt. Retake zusammen.", "Bomb's down. Retake together."),
                 (30, "Bombe 30.", "Bomb 30."), (15, "Bombe 15.", "Bomb 15."),
                 (last, "Letzte Chance, jetzt defusen oder raus!", "Last chance, defuse now or save!")]
        log.info("Timer: Bombe (%ds)", total)
        self._start("bomb", self._countdown(total, [m for i, m in enumerate(marks) if i == 0 or m[0] < total]))

    def bomb_defused(self) -> None:
        if self.cancel("bomb"):
            log.info("Timer: Bombe gestoppt")

    # ---------------------------------------------------------------- Hill
    def hill_sync(self, seconds_left: float | None = None) -> None:
        """Hill-Uhr starten/synchronisieren. Läuft im Kreis bis Matchende oder „Timer aus“."""
        hill = self._v("hill_s", 60)
        pre = self._v("pre_rotate_s", 20)
        first = seconds_left if seconds_left is not None else hill
        log.info("Timer: Hill-Rotation alle %ds (nächste in %ds)", hill, first)

        async def loop() -> None:
            left = first
            while True:
                await self._countdown(left, [(pre, "Zwei Mann vorrotieren zum nächsten Hill.",
                                              "Send two early to the next hill."),
                                             (5, "Hill rotiert, alle rüber!", "Hill's rotating, everyone go!")])
                await asyncio.sleep(max(0.0, 5))
                left = hill

        self._start("hill", loop())

    # ------------------------------------------------------------ Gegner-UAV
    def enemy_uav(self) -> None:
        dur = self._v("enemy_uav_s", 30)
        self._start("uav", self._countdown(dur, [(0, "Gegner-UAV müsste weg sein.", "Enemy UAV should be down.")]))

    # ---------------------------------------------------------- eigener Timer
    def custom(self, seconds: float) -> None:
        marks = [(0, "Timer ist um!", "Time's up!")]
        if seconds > 15:
            marks.append((10, "Noch zehn Sekunden.", "Ten seconds."))
        if seconds > 60:
            marks.append((30, "Noch dreißig Sekunden.", "Thirty seconds."))
        self._say(f"Timer läuft, {int(seconds)} Sekunden." if seconds < 120 else f"Timer läuft, {seconds / 60:g} Minuten.",
                  f"Timer started, {int(seconds)} seconds." if seconds < 120 else f"Timer started, {seconds / 60:g} minutes.", 2)
        self._start("custom", self._countdown(seconds, marks))

    # --------------------------------------------------- aus Comms (ohne „Coach“)
    def on_comms(self, text: str, mode: str) -> None:
        n = _norm(text)
        if mode == "snd" and PLANT_RE.search(n) and "bomb" not in self.active:
            self.bomb_planted()
        elif DEFUSE_RE.search(n):
            self.bomb_defused()
        if ENEMY_UAV_RE.search(n):
            self.enemy_uav()


# ------------------------------------------------------------------ Gegner-Tendenzen
class EnemyTendencies:
    """Merkt sich, wo die Gegner auftauchen (aus Calls und Minimap) und sagt wiederkehrende Muster an.
    Pro Gegner und Map gespeichert, damit Yvara AI beim nächsten Match gegen sie schon Bescheid weiß."""

    def __init__(self, path_fn: Callable[[], Path]) -> None:
        self.path_fn = path_fn
        self.reset()

    def reset(self, opponent: str = "", map_name: str = "", mode: str = "") -> None:
        self.opponent, self.map, self.mode = opponent, map_name, mode
        self.sightings: list[tuple[float, str, int, str]] = []  # (ts, ort, runde, phase)
        self.round_no, self.round_start = 1, time.time()
        self._called: dict[str, float] = {}
        self._early_seen: set[str] = set()

    def new_round(self, round_no: int) -> None:
        self.round_no, self.round_start = round_no, time.time()
        self._early_seen = set()

    def _phase(self, now: float) -> str:
        dt = now - self.round_start
        return "early" if dt < 25 else "mid" if dt < 60 else "late"

    def add(self, where: str, now: float | None = None) -> None:
        now = now or time.time()
        self.sightings.append((now, where, self.round_no, self._phase(now)))
        self.sightings = self.sightings[-2000:]

    def add_minimap(self, zones: list[str], now: float | None = None) -> None:
        """Frühe Gegner-Positionen pro Runde (einmal pro Zone und Runde)."""
        now = now or time.time()
        if self._phase(now) != "early":
            return
        for z in zones:
            key = f"Zone {z}"
            if key not in self._early_seen:
                self._early_seen.add(key)
                self.add(key, now)

    def check(self, round_based: bool, now: float | None = None) -> tuple[str, str] | None:
        """Gibt (Ort, Art) zurück, wenn ein Muster neu ansagenswert ist."""
        now = now or time.time()
        if round_based:
            early = Counter()
            for where, rounds in self._by_place(lambda s: s[3] == "early").items():
                early[where] = len(rounds)
            recent_rounds = max(1, self.round_no - 1)
            for where, n in early.most_common(3):
                if n >= 3 and n >= 0.6 * min(recent_rounds, 6) and now - self._called.get(where, 0) > 240:
                    self._called[where] = now
                    return where, "early"
            return None
        window = Counter(w for ts, w, _r, _p in self.sightings if now - ts < 240 and not w.startswith("Zone "))
        for where, n in window.most_common(2):
            if n >= 4 and now - self._called.get(where, 0) > 180:
                self._called[where] = now
                return where, "often"
        return None

    def _by_place(self, pred) -> dict[str, set[int]]:
        out: dict[str, set[int]] = {}
        for s in self.sightings:
            if pred(s):
                out.setdefault(s[1], set()).add(s[2])
        return out

    def top(self, n: int = 5) -> list[tuple[str, int]]:
        return Counter(w for _t, w, _r, _p in self.sightings).most_common(n)

    def summary(self) -> str:
        top = self.top(6)
        if not top:
            return ""
        early = sorted(((w, len(r)) for w, r in self._by_place(lambda s: s[3] == "early").items()), key=lambda x: -x[1])[:3]
        txt = "ENEMY TENDENCIES this match: " + ", ".join(f"{w} ({c}x)" for w, c in top)
        if early:
            txt += " | early-round: " + ", ".join(f"{w} ({c} rounds)" for w, c in early)
        hist = self.history()
        if hist:
            txt += " | previous matches vs them here: " + ", ".join(f"{w} ({c}x)" for w, c in hist[:5])
        return txt

    # ---------------------------------------------------------- Speicherung
    def _key(self) -> str:
        return f"{(self.opponent or '?').lower()}|{(self.map or '?').lower()}|{self.mode}"

    def _load(self) -> dict:
        p = self.path_fn()
        try:
            return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
        except Exception:
            return {}

    def history(self) -> list[tuple[str, int]]:
        d = self._load().get(self._key(), {})
        return sorted(d.get("places", {}).items(), key=lambda x: -x[1])

    def save(self) -> None:
        if not self.sightings or not self.opponent:
            return
        data = self._load()
        entry = data.setdefault(self._key(), {"places": {}, "matches": 0})
        for w, c in Counter(w for _t, w, _r, _p in self.sightings).items():
            entry["places"][w] = entry["places"].get(w, 0) + c
        entry["matches"] += 1
        entry["ts"] = time.time()
        p = self.path_fn()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")


def tendency_call(where: str, kind: str, lang: str) -> str:
    if kind == "early":
        return (f"Muster: Die sind am Rundenanfang fast immer {where}. Plant das ein."
                if lang == "de" else f"Pattern: they're almost always {where} early in the round. Play around it.")
    return (f"Muster: Die kommen immer wieder über {where}. Crossfire drauf."
            if lang == "de" else f"Pattern: they keep coming through {where}. Set a crossfire.")


# ------------------------------------------------------------------ Heatmaps pro Map
def _slug(name: str) -> str:
    return re.sub(r"[^a-z0-9_-]+", "_", (name or "unbekannt").lower()).strip("_") or "unbekannt"


class MapHeat:
    """Heatmaps über alle Matches auf einer Map: eigene Positionen, Gegner, Kampfzonen."""

    LAYERS = ("ours", "enemy", "fights")

    def __init__(self, dir_fn: Callable[[], Path]) -> None:
        self.dir_fn = dir_fn

    def _file(self, map_name: str) -> Path:
        return self.dir_fn() / f"{_slug(map_name)}.npz"

    def maps(self) -> list[dict]:
        d = self.dir_fn()
        out = []
        for f in sorted(d.glob("*.npz")) if d.exists() else []:
            try:
                z = np.load(f, allow_pickle=False)
                out.append({"map": str(z["name"]) if "name" in z else f.stem, "slug": f.stem,
                            "matches": int(z["matches"]) if "matches" in z else 0})
            except Exception:
                continue
        return out

    def load(self, map_name: str) -> dict | None:
        f = self.dir_fn() / f"{_slug(map_name)}.npz"
        if not f.exists():
            return None
        z = np.load(f, allow_pickle=False)
        return {k: z[k] for k in z.files}

    def add_match(self, map_name: str, heat: dict, background: np.ndarray | None) -> Path | None:
        if not any(float(np.sum(heat.get(k, 0))) > 0 for k in self.LAYERS):
            return None
        d = self.dir_fn()
        d.mkdir(parents=True, exist_ok=True)
        old = self.load(map_name) or {}
        data = {k: (old.get(k, np.zeros((50, 50), np.float32)) + heat.get(k, np.zeros((50, 50), np.float32))).astype(np.float32)
                for k in self.LAYERS}
        data["matches"] = np.array(int(old.get("matches", 0)) + 1)
        data["name"] = np.array(map_name or "unbekannt")
        if background is not None:
            data["bg"] = background
        elif "bg" in old:
            data["bg"] = old["bg"]
        np.savez_compressed(self._file(map_name), **data)
        return self._file(map_name)

    def render(self, map_name: str, layer: str = "all", data: dict | None = None) -> bytes | None:
        """PNG: blau = ihr, rot = Gegner, gelb = Kampfzonen."""
        import cv2

        data = data if data is not None else self.load(map_name)
        if not data:
            return None
        size = 500
        bg = data.get("bg")
        base = cv2.resize(bg, (size, size)) if bg is not None and getattr(bg, "size", 0) else \
            np.full((size, size, 3), 28, np.uint8)
        if bg is None or not getattr(bg, "size", 0):
            for i in range(1, 5):  # Raster A–E / 1–5
                cv2.line(base, (i * 100, 0), (i * 100, size), (60, 60, 60), 1)
                cv2.line(base, (0, i * 100), (size, i * 100), (60, 60, 60), 1)
        overlay = np.zeros_like(base)
        colors = {"ours": (255, 120, 0), "enemy": (60, 60, 255), "fights": (0, 230, 255)}  # BGR
        for k in self.LAYERS:
            if layer not in ("all", k):
                continue
            hm = data.get(k)
            if hm is None or float(hm.max()) <= 0:
                continue
            hm = cv2.GaussianBlur(cv2.resize(hm.astype(np.float32) / float(hm.max()), (size, size)), (0, 0), 12)
            hm = hm / max(float(hm.max()), 1e-6)
            for c in range(3):
                overlay[:, :, c] = np.maximum(overlay[:, :, c], (hm * colors[k][c]).astype(np.uint8))
        out = cv2.addWeighted(base, 0.6, overlay, 0.9, 0)
        ok, buf = cv2.imencode(".png", out)
        return buf.tobytes() if ok else None


# ------------------------------------------------------------------ Strats als Text für die KI
def _zone(p) -> str:
    try:
        x, y = float(p[0]), float(p[1])
    except (TypeError, ValueError, IndexError):
        return "?"
    return "ABCDE"[min(4, max(0, int(x * 5)))] + str(min(5, max(1, int(y * 5) + 1)))


def describe_strat(s: dict, zone_name: Callable[[str], str] | None = None) -> str:
    """Strat-Board-Zeichnung in Worte fassen: wer steht wo, welche Laufwege, wo Gegner erwartet werden."""
    zn = zone_name or (lambda z: "")

    def where(p) -> str:
        z = _zone(p)
        n = zn(z)
        return f"{n} ({z})" if n else z

    bits = []
    for sh in s.get("shapes") or []:
        t = sh.get("t")
        if t == "player":
            bits.append(f"{sh.get('s') or 'player'} at {where(sh.get('a'))}")
        elif t == "arrow":
            bits.append(f"route {where(sh.get('a'))} -> {where(sh.get('b'))}")
        elif t == "x":
            bits.append(f"enemy expected at {where(sh.get('a'))}")
        elif t == "circle":
            bits.append(f"area/hold {where(sh.get('a'))}")
        elif t == "text" and sh.get("s"):
            bits.append(f"note '{sh['s']}' at {where(sh.get('a'))}")
        elif t == "pen" and sh.get("pts"):
            bits.append(f"path {where(sh['pts'][0])} -> {where(sh['pts'][-1])}")
    head = f"- {s.get('name', 'Strat')} [{s.get('mode') or 'any mode'}, {s.get('map') or 'any map'}]"
    if s.get("notes"):
        head += f": {s['notes']}"
    return head + ("\n  drawing: " + "; ".join(bits[:25]) if bits else "")


def strats_context(folder: Path, map_name: str = "", mode: str = "", limit: int = 6,
                   zone_name: Callable[[str], str] | None = None) -> str:
    if not folder.exists():
        return ""
    items = []
    for f in folder.glob("*.json"):
        try:
            items.append(json.loads(f.read_text(encoding="utf-8")))
        except Exception:
            continue
    if not items:
        return ""

    def score(s: dict) -> tuple:
        return ((map_name and (s.get("map") or "").lower() == map_name.lower()) * 2
                + (mode and (s.get("mode") or "").lower() == mode.lower()) * 1, s.get("ts", 0))

    items.sort(key=score, reverse=True)
    return "OUR STRATS (from the strat board, minimap zones A1=top-left .. E5=bottom-right):\n" + \
        "\n".join(describe_strat(s, zone_name) for s in items[:limit])
