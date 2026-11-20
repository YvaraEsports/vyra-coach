"""Minimap-Tracking: Spieler-Marker auf der Minimap erkennen (eigene Sicht oder
Caster-/Zuschauer-Ansicht mit allen 8 Spielern), Positionen und Bewegungen
verfolgen, Setups/Rotationen zusammenfassen und Heatmaps erzeugen.

Funktionsweise: Die Minimap wird per Bildbereich (hud.minimap) ausgeschnitten,
Marker werden über ihre Teamfarbe (HSV-Bereiche, einstellbar) gefunden.
Bewegungsrichtung ergibt sich aus der Verschiebung zwischen den Bildern.
Rein bildbasiert – kein Zugriff auf das Spiel.
"""
from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass

import numpy as np

from .capture import crop_rel

# OpenCV-HSV: H 0-179, S/V 0-255. Mehrere Bereiche pro Team möglich (Rot liegt an beiden Enden).
DEFAULT_COLORS = {
    "ours": [[[95, 120, 120], [125, 255, 255]]],                      # blau / cyan
    "enemy": [[[0, 130, 130], [8, 255, 255]], [[170, 130, 130], [179, 255, 255]]],  # rot / orange
}
GRID = 5  # Minimap in 5x5 Zonen A1..E5 für Text-Zusammenfassungen


def zone(x: float, y: float) -> str:
    col = "ABCDE"[min(GRID - 1, max(0, int(x * GRID)))]
    row = min(GRID, max(1, int(y * GRID) + 1))
    return f"{col}{row}"


@dataclass
class Marker:
    team: str
    x: float  # 0..1 innerhalb der Minimap
    y: float
    size: float


class MinimapTracker:
    def __init__(self, cfg) -> None:
        self.cfg = cfg
        self.history: deque[tuple[float, list[Marker]]] = deque(maxlen=3600)  # ~30 min bei 2 fps
        self.heat = {k: np.zeros((50, 50), np.float32) for k in ("ours", "enemy", "fights")}
        self.last_crop: np.ndarray | None = None

    @property
    def roi(self):
        return self.cfg.get_path("hud.minimap")

    def colors(self) -> dict:
        return self.cfg.get_path("minimap.team_colors") or DEFAULT_COLORS

    # ------------------------------------------------------------ detect
    def detect(self, crop: np.ndarray) -> list[Marker]:
        import cv2

        h, w = crop.shape[:2]
        hsv = cv2.cvtColor(crop, cv2.COLOR_BGR2HSV)
        min_area = float(self.cfg.get_path("minimap.min_area", 6)) * (w * h) / (200 * 200)
        max_area = float(self.cfg.get_path("minimap.max_area", 220)) * (w * h) / (200 * 200)
        markers: list[Marker] = []
        for team, ranges in self.colors().items():
            mask = np.zeros((h, w), np.uint8)
            for lo, hi in ranges:
                mask |= cv2.inRange(hsv, np.array(lo, np.uint8), np.array(hi, np.uint8))
            mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, np.ones((2, 2), np.uint8))
            n, _lab, stats, cents = cv2.connectedComponentsWithStats(mask)
            for i in range(1, n):
                area = stats[i, cv2.CC_STAT_AREA]
                if min_area <= area <= max_area:
                    cx, cy = cents[i]
                    markers.append(Marker(team, float(cx / w), float(cy / h), float(area)))
        # Max. 4 pro Team (größte Blobs)
        out: list[Marker] = []
        for team in self.colors():
            out += sorted((m for m in markers if m.team == team), key=lambda m: -m.size)[:4]
        return out

    def update(self, frame: np.ndarray, now: float | None = None) -> list[Marker] | None:
        crop = crop_rel(frame, self.roi)
        if crop is None:
            return None
        self.last_crop = crop
        markers = self.detect(crop)
        self.history.append((now or time.time(), markers))
        for m in markers:
            if m.team in self.heat:
                hx, hy = min(49, int(m.x * 50)), min(49, int(m.y * 50))
                self.heat[m.team][hy, hx] += 1
        # Kampfzonen: eigener und gegnerischer Marker dicht beieinander = Gefecht
        ours = [m for m in markers if m.team == "ours"]
        for e in (m for m in markers if m.team == "enemy"):
            for o in ours:
                if (e.x - o.x) ** 2 + (e.y - o.y) ** 2 < 0.07 ** 2:
                    cx, cy = (e.x + o.x) / 2, (e.y + o.y) / 2
                    self.heat["fights"][min(49, int(cy * 50)), min(49, int(cx * 50))] += 1
        return markers

    def reset(self) -> None:
        self.history.clear()
        for k in self.heat:
            self.heat[k][:] = 0

    # ----------------------------------------------------------- analyse
    @staticmethod
    def _centroid(ms: list[Marker]) -> tuple[float, float] | None:
        if not ms:
            return None
        return float(np.mean([m.x for m in ms])), float(np.mean([m.y for m in ms]))

    @staticmethod
    def _spread(ms: list[Marker]) -> float:
        if len(ms) < 2:
            return 0.0
        pts = np.array([[m.x, m.y] for m in ms])
        return float(np.mean(np.linalg.norm(pts - pts.mean(axis=0), axis=1)))

    def summary(self, seconds: float = 20, now: float | None = None) -> str:
        """Textzusammenfassung der letzten Sekunden für die KI."""
        now = now or time.time()
        win = [(t, ms) for t, ms in self.history if now - t <= seconds]
        if not win:
            return ""
        lines = [f"MINIMAP (last {int(seconds)}s, zones {GRID}x{GRID} A1=top-left):"]
        for team in self.colors():
            first = next((ms for _, ms in win if any(m.team == team for m in ms)), [])
            last = next((ms for _, ms in reversed(win) if any(m.team == team for m in ms)), [])
            f = [m for m in first if m.team == team]
            l_ = [m for m in last if m.team == team]
            if not l_:
                lines.append(f"- {team}: not visible")
                continue
            zones = ", ".join(sorted(zone(m.x, m.y) for m in l_))
            c0, c1 = self._centroid(f), self._centroid(l_)
            move = ""
            if c0 and c1:
                dx, dy = c1[0] - c0[0], c1[1] - c0[1]
                dist = (dx * dx + dy * dy) ** 0.5
                if dist > 0.08:
                    dirs = ("east" if dx > 0.04 else "west" if dx < -0.04 else "") + ("-south" if dy > 0.04 else "-north" if dy < -0.04 else "")
                    move = f", group moving {dirs.strip('-')} ({zone(*c0)} -> {zone(*c1)})"
                else:
                    move = ", holding position"
            spread = self._spread(l_)
            shape = "stacked together" if spread < 0.08 else "spread out" if spread > 0.22 else "medium spacing"
            lines.append(f"- {team}: {len(l_)} visible in {zones}{move}, {shape}")
        return "\n".join(lines)

    def round_summary(self, since: float) -> str:
        """Kompakte Positions-Zeitleiste einer Runde (alle ~5 s)."""
        rows, last_t = [], 0.0
        for t, ms in self.history:
            if t < since or t - last_t < 5:
                continue
            last_t = t
            parts = []
            for team in self.colors():
                zs = sorted(zone(m.x, m.y) for m in ms if m.team == team)
                parts.append(f"{team}:{'/'.join(zs) or '-'}")
            rows.append(f"+{int(t - since)}s " + " ".join(parts))
        return "MINIMAP TIMELINE:\n" + "\n".join(rows[-60:]) if rows else ""

    def heatmap(self, path) -> str | None:
        """Heatmap (eigenes Team blau, Gegner rot) über die letzte Minimap legen."""
        import cv2

        if self.last_crop is None or (self.heat["ours"].sum() + self.heat["enemy"].sum()) == 0:
            return None
        base = cv2.resize(self.last_crop, (500, 500))
        overlay = np.zeros_like(base)
        for team, channel in (("ours", 0), ("enemy", 2), ("fights", 1)):
            hm = self.heat[team]
            if hm.max() > 0:
                hm = cv2.GaussianBlur(cv2.resize(hm / hm.max(), (500, 500)), (0, 0), 12)
                overlay[:, :, channel] = np.clip(hm / max(hm.max(), 1e-6) * 255, 0, 255).astype(np.uint8)
        out = cv2.addWeighted(base, 0.55, overlay, 0.9, 0)
        cv2.imwrite(str(path), out)
        return str(path)
