"""Mehrere Bildquellen gleichzeitig: ein Feed pro Spieler.

Feed-Arten:
  monitor  – ein Bildschirm (ref = Nummer, z. B. 2)
  window   – ein Fenster per Titel (nur Windows), z. B. die Discord-Übertragung
  region   – Bildschirmausschnitt [links, oben, breite, höhe]
  twitch   – Twitch-Livestream eines Spielers (ref = Kanal oder URL); wird lokal
             mit yt-dlp + ffmpeg gelesen (ca. 1 Bild/Sekunde). Twitch hat einige
             Sekunden Verzögerung – ideal für Coaching und Feedback, nicht für
             Calls auf die Millisekunde.
  stream   – Bild direkt aus OBS im Heimnetz (ref = srt://… oder udp://…), z. B. vom
             Gaming-PC zum MacBook. Kaum Verzögerung (meist unter 1 Sekunde).
  device   – Capture-Karte / Kamera (ref = Gerätenummer, z. B. 0)

Yvara AI bekommt alle Feeds als Kachelbild (Übersicht) und entscheidet, wen sie sich
genauer anschaut ("Auto-Fokus").
"""
from __future__ import annotations

import logging
import subprocess
import threading
import time
from dataclasses import dataclass, field

import numpy as np

log = logging.getLogger(__name__)


@dataclass
class FeedSpec:
    player: str
    kind: str = "monitor"
    ref: object = 1

    @classmethod
    def from_dict(cls, d: dict) -> "FeedSpec":
        return cls(player=str(d.get("player") or d.get("name") or "?"), kind=d.get("kind", "monitor"), ref=d.get("ref", 1))

    def to_dict(self) -> dict:
        return {"player": self.player, "kind": self.kind, "ref": self.ref}


class ScreenFeed:
    def __init__(self, spec: FeedSpec) -> None:
        from ..config import Config
        from .capture import ScreenCapture

        cfg = Config({"capture": {"mode": "window" if spec.kind == "window" else "monitor",
                                  "monitor": int(spec.ref) if spec.kind == "monitor" else 1,
                                  "window_title": str(spec.ref) if spec.kind == "window" else "",
                                  "region": spec.ref if spec.kind == "region" else None}})
        self.cap = ScreenCapture(cfg)
        self.spec = spec

    def latest(self) -> np.ndarray | None:
        try:
            return self.cap.grab()
        except Exception as e:
            log.debug("Feed %s: %s", self.spec.player, e)
            return None

    def close(self) -> None:
        pass


class TwitchFeed:
    """Liest einen Twitch-Livestream im Hintergrund und hält das neueste Bild bereit."""

    def __init__(self, spec: FeedSpec, fps: float = 1.0, width: int = 960) -> None:
        self.spec = spec
        ref = str(spec.ref)
        self.url = ref if ref.startswith("http") else f"https://www.twitch.tv/{ref}"
        self.fps, self.width = fps, width
        self._frame: np.ndarray | None = None
        self._ts = 0.0
        self._stop = threading.Event()
        self._proc: subprocess.Popen | None = None
        self.error = ""
        self.in_flags: list[str] = []
        self.retry_s = 30.0
        threading.Thread(target=self._run, daemon=True).start()

    def _stream_url(self) -> str:
        import yt_dlp

        with yt_dlp.YoutubeDL({"quiet": True, "format": "best[height<=480]/worst"}) as ydl:
            info = ydl.extract_info(self.url, download=False)
        if not info.get("is_live", True):
            raise RuntimeError("Kanal ist gerade nicht live")
        return info["url"]

    def _run(self) -> None:
        import cv2

        while not self._stop.is_set():
            try:
                hls = self._stream_url()
                self.error = ""
                self._proc = subprocess.Popen(
                    ["ffmpeg", "-v", "quiet", *self.in_flags, "-i", hls, "-vf", f"fps={self.fps},scale={self.width}:-2",
                     "-f", "image2pipe", "-vcodec", "mjpeg", "-q:v", "5", "-"],
                    stdout=subprocess.PIPE)
                buf = b""
                while not self._stop.is_set():
                    chunk = self._proc.stdout.read(65536)
                    if not chunk:
                        break
                    buf += chunk
                    while True:  # JPEGs aus dem Datenstrom schneiden
                        start = buf.find(b"\xff\xd8")
                        end = buf.find(b"\xff\xd9", start + 2)
                        if start < 0 or end < 0:
                            break
                        img = cv2.imdecode(np.frombuffer(buf[start:end + 2], np.uint8), cv2.IMREAD_COLOR)
                        buf = buf[end + 2:]
                        if img is not None:
                            self._frame, self._ts = img, time.time()
            except Exception as e:
                self.error = str(e)
                log.info("Feed %s: %s – neuer Versuch in %.0f s", self.spec.player, e, self.retry_s)
            finally:
                if self._proc:
                    self._proc.kill()
            self._stop.wait(self.retry_s)

    def latest(self) -> np.ndarray | None:
        if self._frame is None or time.time() - self._ts > 30:
            return None
        return self._frame

    def close(self) -> None:
        self._stop.set()
        if self._proc:
            self._proc.kill()


LOW_DELAY_FLAGS = ["-fflags", "nobuffer", "-flags", "low_delay", "-probesize", "500000", "-analyzeduration", "0"]


def stream_url(ref: str) -> str:
    """Kurzformen für die OBS-Übertragung im Heimnetz: „9000“ oder „srt:9000“ -> SRT-Empfänger auf Port 9000."""
    ref = str(ref).strip()
    if ref.isdigit() or ref.startswith("srt:") and ref[4:].isdigit():
        port = ref.split(":")[-1]
        return f"srt://0.0.0.0:{port}?mode=listener&latency=60000"
    if ref.startswith("udp:") and ref[4:].isdigit():
        return f"udp://0.0.0.0:{ref[4:]}?fifo_size=1000000&overrun_nonfatal=1"
    if not ref.startswith(("srt://", "udp://", "rtmp://", "rtsp://", "tcp://")):
        raise ValueError("Stream-Adresse muss mit srt://, udp://, rtmp://, rtsp:// oder tcp:// beginnen")
    return ref


class StreamFeed(TwitchFeed):
    """OBS schickt das Spielbild direkt übers Heimnetz (SRT/UDP) – ohne Umweg über Twitch, kaum Verzögerung."""

    def __init__(self, spec: FeedSpec, fps: float = 2.0, width: int = 960) -> None:
        self.direct_url = stream_url(str(spec.ref))
        super().__init__(spec, fps=fps, width=width)
        self.url = self.direct_url
        self.in_flags = list(LOW_DELAY_FLAGS)
        self.retry_s = 3.0

    def _stream_url(self) -> str:
        return self.direct_url


class DeviceFeed:
    """Capture-Karte oder Kamera (OpenCV)."""

    def __init__(self, spec: FeedSpec) -> None:
        self.spec = spec
        self.error = ""
        self._frame: np.ndarray | None = None
        self._ts = 0.0
        self._stop = threading.Event()
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self) -> None:
        import cv2

        while not self._stop.is_set():
            cap = cv2.VideoCapture(int(self.spec.ref or 0))
            if not cap.isOpened():
                self.error = "Gerät nicht gefunden"
                self._stop.wait(5)
                continue
            cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
            self.error = ""
            while not self._stop.is_set():
                ok, img = cap.read()
                if not ok:
                    break
                self._frame, self._ts = img, time.time()
            cap.release()
            self._stop.wait(2)

    def latest(self) -> np.ndarray | None:
        if self._frame is None or time.time() - self._ts > 10:
            return None
        return self._frame

    def close(self) -> None:
        self._stop.set()


def make_feed(spec: FeedSpec):
    if spec.kind == "twitch":
        return TwitchFeed(spec)
    if spec.kind == "stream":
        return StreamFeed(spec)
    if spec.kind == "device":
        return DeviceFeed(spec)
    if spec.kind == "sync":
        from ..sync import SyncFeed

        return SyncFeed(spec)
    return ScreenFeed(spec)


def composite(frames: dict[str, np.ndarray | None], tile_w: int = 640) -> np.ndarray | None:
    """Kachelbild aller Feeds mit Spielernamen (2x2, 3x2 …)."""
    import cv2

    items = [(name, f) for name, f in frames.items()]
    if not items:
        return None
    cols = 2 if len(items) > 1 else 1
    rows = (len(items) + cols - 1) // cols
    tile_h = int(tile_w * 9 / 16)
    canvas = np.zeros((rows * tile_h, cols * tile_w, 3), np.uint8)
    for i, (name, f) in enumerate(items):
        r, c = divmod(i, cols)
        tile = np.zeros((tile_h, tile_w, 3), np.uint8)
        if f is not None:
            tile = cv2.resize(f, (tile_w, tile_h), interpolation=cv2.INTER_AREA)
        else:
            cv2.putText(tile, "kein Bild", (20, tile_h // 2), cv2.FONT_HERSHEY_SIMPLEX, 1, (120, 120, 120), 2)
        cv2.rectangle(tile, (0, 0), (len(name) * 16 + 20, 34), (0, 0, 0), -1)
        cv2.putText(tile, name, (8, 25), cv2.FONT_HERSHEY_SIMPLEX, 0.8, (255, 255, 255), 2)
        canvas[r * tile_h:(r + 1) * tile_h, c * tile_w:(c + 1) * tile_w] = tile
    return canvas


@dataclass
class FeedManager:
    specs: list[FeedSpec] = field(default_factory=list)

    def __post_init__(self) -> None:
        self._feeds: dict[str, object] = {}
        for s in self.specs:
            self._feeds[s.player] = make_feed(s)

    @classmethod
    def from_cfg(cls, cfg) -> "FeedManager":
        return cls([FeedSpec.from_dict(d) for d in cfg.get_path("feeds", []) or []])

    @property
    def active(self) -> bool:
        return bool(self._feeds)

    def players(self) -> list[str]:
        return list(self._feeds)

    def set(self, specs: list[FeedSpec]) -> None:
        for f in self._feeds.values():
            f.close()
        self.specs = specs
        self._feeds = {s.player: make_feed(s) for s in specs}

    def frames(self) -> dict[str, np.ndarray | None]:
        return {name: f.latest() for name, f in self._feeds.items()}

    def frame(self, player: str) -> np.ndarray | None:
        f = self._feeds.get(player) or next((v for k, v in self._feeds.items() if k.lower() == (player or "").lower()), None)
        return f.latest() if f else None

    def status(self) -> list[dict]:
        out = []
        for s in self.specs:
            f = self._feeds.get(s.player)
            out.append({**s.to_dict(), "ok": f is not None and f.latest() is not None, "error": getattr(f, "error", "")})
        return out
