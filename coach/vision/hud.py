"""HUD lesen (Score, Spielzeit, Objective-Timer, Killfeed) per OCR.

Liest nur Pixel vom Bildschirm – kein Speicherzugriff, keine Spielmanipulation.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

import numpy as np

from .capture import crop_rel

log = logging.getLogger(__name__)

_TIMER_RE = re.compile(r"(\d{1,2})\s*[:.]\s*(\d{2})")
_INT_RE = re.compile(r"\d{1,3}")
_OCR_FIX = str.maketrans({"O": "0", "o": "0", "D": "0", "I": "1", "l": "1", "|": "1", "S": "5", "B": "8", "Z": "2"})


_FIXABLE = set("OoDIl|SBZ0123456789:.")


def _fix_tokens(text: str) -> str:
    """OCR-Verwechsler (O->0, I->1 ...) nur in zahlenartigen Wörtern korrigieren."""
    out = []
    for tok in text.split():
        if set(tok) <= _FIXABLE and any(c.isdigit() for c in tok) or (len(tok) <= 3 and set(tok) <= _FIXABLE):
            tok = tok.translate(_OCR_FIX)
        out.append(tok)
    return " ".join(out)


def parse_int(text: str | None) -> int | None:
    if not text:
        return None
    m = _INT_RE.search(_fix_tokens(text))
    return int(m.group()) if m else None


def parse_timer(text: str | None) -> int | None:
    """'1:23' -> 83 Sekunden. Auch '0.45' oder '45' (nur Sekunden)."""
    if not text:
        return None
    fixed = _fix_tokens(text)
    m = _TIMER_RE.search(fixed)
    if m:
        mins, secs = int(m.group(1)), int(m.group(2))
        return mins * 60 + secs if secs < 60 else None
    n = parse_int(fixed)
    return n if n is not None and n < 100 else None


@dataclass
class HudReading:
    score_ours: int | None = None
    score_enemy: int | None = None
    game_timer: int | None = None
    objective_timer: int | None = None
    killfeed: list[str] = field(default_factory=list)


class HudReader:
    def __init__(self, cfg) -> None:
        self.cfg = cfg
        self._ocr = None

    def _engine(self):
        if self._ocr is None:
            from rapidocr import RapidOCR

            self._ocr = RapidOCR()
        return self._ocr

    def ocr_text(self, img: np.ndarray | None) -> str:
        if img is None or img.size == 0:
            return ""
        import cv2

        # Kleine HUD-Bereiche hochskalieren -> bessere Erkennung
        scale = 3 if img.shape[0] < 80 else 1
        if scale > 1:
            img = cv2.resize(img, None, fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC)
        result = self._engine()(img)
        txts = getattr(result, "txts", None) or ()
        return "  ".join(txts)

    def read(self, frame: np.ndarray) -> HudReading:
        hud = self.cfg.get("hud", {})
        txt = {k: self.ocr_text(crop_rel(frame, hud.get(k))) for k in ("score_ours", "score_enemy", "game_timer", "objective_timer")}
        kf_raw = self.ocr_text(crop_rel(frame, hud.get("killfeed"))) if hud.get("killfeed") else ""
        return HudReading(
            score_ours=parse_int(txt["score_ours"]),
            score_enemy=parse_int(txt["score_enemy"]),
            game_timer=parse_timer(txt["game_timer"]),
            objective_timer=parse_timer(txt["objective_timer"]),
            killfeed=[s for s in kf_raw.split("  ") if s.strip()] if kf_raw else [],
        )
