"""Spielzustand aus HUD-Werten ableiten und Ereignisse erzeugen.

Erzeugte Events: round_end, match_point, match_end, score_swing,
hill_rotate, time_low. Werte werden erst übernommen, wenn sie zweimal in
Folge gleich gelesen wurden (OCR-Rauschen).
"""
from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass, field

from .hud import HudReading


@dataclass
class GameEvent:
    kind: str
    data: dict = field(default_factory=dict)
    ts: float = field(default_factory=time.time)


@dataclass
class GameState:
    mode: str = "hardpoint"
    map: str = ""
    opponent: str = ""
    score_ours: int = 0
    score_enemy: int = 0
    game_timer: int | None = None
    objective_timer: int | None = None
    round_no: int = 1
    active: bool = False
    started_at: float = 0.0

    def summary(self) -> str:
        parts = [f"Modus={self.mode}", f"Map={self.map or '?'}", f"Gegner={self.opponent or '?'}",
                 f"Score uns {self.score_ours} : {self.score_enemy} Gegner"]
        if self.round_no and self.mode != "hardpoint":
            parts.append(f"Runde {self.round_no}")
        if self.game_timer is not None:
            parts.append(f"Zeit {self.game_timer // 60}:{self.game_timer % 60:02d}")
        if self.objective_timer is not None:
            parts.append(f"Objective-Timer {self.objective_timer}s")
        return ", ".join(parts)


class _Stable:
    """Übernimmt einen Wert erst nach n identischen Lesungen."""

    def __init__(self, n: int = 2) -> None:
        self.n = n
        self.cand = None
        self.count = 0

    def feed(self, v):
        if v is None:
            return None
        if v == self.cand:
            self.count += 1
        else:
            self.cand, self.count = v, 1
        return v if self.count >= self.n else None


class StateTracker:
    def __init__(self, modes: dict) -> None:
        self.modes = modes
        self.state = GameState()
        self._s_ours = _Stable()
        self._s_enemy = _Stable()
        self._s_timer = _Stable()
        self._s_obj = _Stable()
        self._history: deque[tuple[float, int, int]] = deque(maxlen=600)
        self._fired: set[str] = set()
        self._last_swing = 0.0

    def mode_cfg(self) -> dict:
        return self.modes.get(self.state.mode, {})

    def start_match(self, mode: str, map_name: str = "", opponent: str = "") -> None:
        self.state = GameState(mode=mode, map=map_name, opponent=opponent, active=True, started_at=time.time())
        self._history.clear()
        self._fired.clear()

    def update(self, r: HudReading, now: float | None = None) -> list[GameEvent]:
        now = now or time.time()
        st = self.state
        ev: list[GameEvent] = []
        if not st.active:
            return ev
        mc = self.mode_cfg()
        target = int(mc.get("target") or 0) or 10**6  # 0 = unbekannt (z. B. neuer Modus)
        round_based = bool(mc.get("round_based", False))

        ours = self._s_ours.feed(r.score_ours)
        enemy = self._s_enemy.feed(r.score_enemy)
        timer = self._s_timer.feed(r.game_timer)
        obj = self._s_obj.feed(r.objective_timer)

        # Plausibilität: Scores fallen nie, springen nicht absurd
        max_jump = 1 if round_based else 40
        if ours is not None and st.score_ours <= ours <= st.score_ours + max_jump and ours <= target:
            if ours > st.score_ours and round_based:
                st.score_ours = ours
                ev.append(GameEvent("round_end", {"won": True, "round": st.round_no, "ours": ours, "enemy": st.score_enemy}))
                st.round_no += 1
                self._fired = {f for f in self._fired if not f.startswith("time_")}
            st.score_ours = ours
        if enemy is not None and st.score_enemy <= enemy <= st.score_enemy + max_jump and enemy <= target:
            if enemy > st.score_enemy and round_based:
                st.score_enemy = enemy
                ev.append(GameEvent("round_end", {"won": False, "round": st.round_no, "ours": st.score_ours, "enemy": enemy}))
                st.round_no += 1
                self._fired = {f for f in self._fired if not f.startswith("time_")}
            st.score_enemy = enemy

        self._history.append((now, st.score_ours, st.score_enemy))

        # Matchpoint / Matchende
        if round_based:
            for side, val in (("us", st.score_ours), ("them", st.score_enemy)):
                key = f"mp_{side}_{val}"
                if val == target - 1 and key not in self._fired:
                    self._fired.add(key)
                    ev.append(GameEvent("match_point", {"side": side}))
        if max(st.score_ours, st.score_enemy) >= target and "match_end" not in self._fired:
            self._fired.add("match_end")
            ev.append(GameEvent("match_end", {"won": st.score_ours > st.score_enemy, "ours": st.score_ours, "enemy": st.score_enemy}))

        # Score-Swing (Hardpoint/Respawn): Gegner holt in 30 s deutlich mehr
        if not round_based and now - self._last_swing > 45:
            window = [h for h in self._history if now - h[0] <= 30]
            if len(window) >= 2:
                d_ours = window[-1][1] - window[0][1]
                d_enemy = window[-1][2] - window[0][2]
                if d_enemy - d_ours >= int(mc.get("swing_points", 25)):
                    self._last_swing = now
                    ev.append(GameEvent("score_swing", {"points": d_enemy - d_ours}))

        # Objective-Timer (z. B. Hill-Rotation)
        if obj is not None:
            warn = int(mc.get("rotate_warn_s", 0) or 0)
            if st.objective_timer is not None and obj > st.objective_timer + 5:
                self._fired.discard("hill_rotate")  # neuer Hill
                ev.append(GameEvent("hill_new", {"seconds": obj}))
            if warn and obj <= warn and "hill_rotate" not in self._fired:
                self._fired.add("hill_rotate")
                ev.append(GameEvent("hill_rotate", {"seconds": obj}))
            st.objective_timer = obj

        # Spielzeit-Warnungen (rundenbasiert)
        if timer is not None:
            for thr in mc.get("time_warnings", []):
                key = f"time_{thr}"
                if timer <= thr and key not in self._fired and (st.game_timer is None or st.game_timer > thr):
                    self._fired.add(key)
                    ev.append(GameEvent("time_low", {"seconds": thr}))
            st.game_timer = timer
        return ev
