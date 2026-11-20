"""Bildschirmaufnahme: ganzer Monitor oder ein bestimmtes Fenster.

Tipp: mode "window" mit window_title "Discord" analysiert den Go-Live-Stream
eines Mates, den du in Discord geöffnet hast – so kann der Coach auch auf
einem separaten Coach-PC / Laptop laufen und einzelnen Spielern zuschauen.
macOS: Fenster-Modus gibt es nur unter Windows; nutze dort "monitor" oder
"region" (Bildausschnitt in Pixeln) und erlaube Bildschirmaufnahme in den
Systemeinstellungen > Datenschutz.
"""
from __future__ import annotations

import base64
import logging
import sys
import threading

import numpy as np

log = logging.getLogger(__name__)


class ScreenCapture:
    def __init__(self, cfg) -> None:
        import mss

        self.mode = cfg.get_path("capture.mode", "monitor")
        self.monitor = int(cfg.get_path("capture.monitor", 1))
        self.window_title = cfg.get_path("capture.window_title", "")
        self.region = cfg.get_path("capture.region")  # [left, top, width, height] in Pixeln
        self._mss = mss
        self._local = threading.local()  # mss-Instanz pro Thread (nicht thread-sicher)

    @property
    def _sct(self):
        if not hasattr(self._local, "sct"):
            self._local.sct = self._mss.mss()
        return self._local.sct

    def _region(self) -> dict:
        if self.region:
            l, t, w, h = (int(v) for v in self.region)
            return {"left": l, "top": t, "width": w, "height": h}
        if self.mode == "window" and sys.platform == "win32":
            try:
                import pygetwindow as gw

                wins = [w for w in gw.getWindowsWithTitle(self.window_title) if w.width > 100]
                if wins:
                    w = wins[0]
                    return {"left": w.left, "top": w.top, "width": w.width, "height": w.height}
                log.warning("Fenster '%s' nicht gefunden – nehme Monitor", self.window_title)
            except Exception as e:
                log.warning("Fenster-Capture fehlgeschlagen: %s", e)
        mons = self._sct.monitors
        return mons[self.monitor] if self.monitor < len(mons) else mons[1]

    def grab(self) -> np.ndarray:
        """Aktuelles Bild als BGR-Array."""
        shot = self._sct.grab(self._region())
        return np.asarray(shot)[:, :, :3].copy()


def crop_rel(img: np.ndarray, roi) -> np.ndarray | None:
    if not roi:
        return None
    h, w = img.shape[:2]
    x, y, rw, rh = roi
    x0, y0 = int(x * w), int(y * h)
    x1, y1 = int((x + rw) * w), int((y + rh) * h)
    if x1 <= x0 or y1 <= y0:
        return None
    return img[y0:y1, x0:x1]


def to_jpeg_b64(img: np.ndarray, width: int = 1280, quality: int = 80) -> str:
    import cv2

    h, w = img.shape[:2]
    if w > width:
        img = cv2.resize(img, (width, int(h * width / w)), interpolation=cv2.INTER_AREA)
    ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, quality])
    if not ok:
        raise RuntimeError("JPEG-Kodierung fehlgeschlagen")
    return base64.standard_b64encode(buf.tobytes()).decode("ascii")
