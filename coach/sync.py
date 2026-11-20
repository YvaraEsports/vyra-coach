"""Sync-Modus für zwei Geräte: der Gaming-PC schickt Bild und/oder Ton direkt an das Gerät, auf dem die KI läuft
(z. B. MacBook). Ohne OBS-Einstellungen, ohne Internet – nur im Heimnetz, mit Kopplungs-Code.

  PC (sendet):      Bildschirm (mss, JPEG) + PC-Sound (Discord/Spiel, Loopback) + optional Mikrofon
  Laptop (empfängt): stellt das Bild als Bildquelle bereit (Coach, Caster, Zuschauen) und gibt den Ton an die
                     Spracherkennung weiter – als wäre Yvara AI direkt auf dem PC.

Protokoll (TCP, Standard-Port 9100): erste Zeile JSON {"hello":"yvara-sync","code":"123456",…}, danach Pakete
[1 Byte Typ][4 Byte Länge][Daten]:  V = JPEG-Bild, A = PC-Sound, M = Mikrofon (16 kHz mono int16).
"""
from __future__ import annotations

import asyncio
import ipaddress
import json
import logging
import queue
import random
import socket
import struct
import threading
import time

import numpy as np

from .config import DATA_DIR

log = logging.getLogger(__name__)
PORT = 9100
SR = 16000
BLOCK = 480  # 30 ms, wie audio.input
CODE_FILE = DATA_DIR / "sync_code.txt"
MAX_PACKET = 4 * 1024 * 1024


def pair_code() -> str:
    """Kopplungs-Code dieses Empfängers (bleibt gleich, bis die Datei gelöscht wird)."""
    try:
        code = CODE_FILE.read_text(encoding="utf-8").strip()
        if code.isdigit() and len(code) == 6:
            return code
    except OSError:
        pass
    code = f"{random.SystemRandom().randint(0, 999999):06d}"
    try:
        CODE_FILE.parent.mkdir(parents=True, exist_ok=True)
        CODE_FILE.write_text(code, encoding="utf-8")
    except OSError:
        pass
    return code


def private_peer(addr: str) -> bool:
    try:
        ip = ipaddress.ip_address(str(addr).split("%")[0])
    except ValueError:
        return False
    return ip.is_private or ip.is_loopback


def pack(kind: bytes, payload: bytes) -> bytes:
    return kind + struct.pack(">I", len(payload)) + payload


# ------------------------------------------------------------------ Empfänger (Laptop)
class SyncReceiver:
    """Nimmt Bild und Ton vom Gaming-PC an. Wie ein Feed nutzbar (latest/close/error)."""

    def __init__(self, port: int = PORT, code: str | None = None) -> None:
        self.port = port
        self.code = code or pair_code()
        self._frame: np.ndarray | None = None
        self._ts = 0.0
        self.peer = ""
        self.error = ""
        self.connected = False
        self.stats = {"video": False, "audio": False, "mic": False}
        self.audio_q: dict[str, queue.Queue] = {"A": queue.Queue(maxsize=400), "M": queue.Queue(maxsize=400)}
        self._server: asyncio.base_events.Server | None = None

    async def start(self) -> None:
        self._server = await asyncio.start_server(self._handle, "0.0.0.0", self.port)
        log.info("🔗 Sync-Empfang bereit auf Port %s (Kopplungs-Code %s)", self.port, self.code)

    def close(self) -> None:
        if self._server:
            self._server.close()

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        peer = (writer.get_extra_info("peername") or ("?",))[0]
        if not private_peer(peer):
            log.warning("Sync: Verbindung von %s abgelehnt (nicht im Heimnetz)", peer)
            writer.close()
            return
        try:
            hello = json.loads((await asyncio.wait_for(reader.readline(), 5)).decode("utf-8", "ignore") or "{}")
        except Exception:
            writer.close()
            return
        if hello.get("hello") != "yvara-sync" or str(hello.get("code", "")) != self.code:
            log.warning("Sync: falscher Kopplungs-Code von %s", peer)
            writer.write(b'{"ok":false,"error":"code"}\n')
            await writer.drain()
            writer.close()
            return
        writer.write(b'{"ok":true}\n')
        await writer.drain()
        self.connected, self.peer, self.error = True, peer, ""
        self.stats = {"video": bool(hello.get("video")), "audio": bool(hello.get("audio")), "mic": bool(hello.get("mic"))}
        log.info("🔗 Sync verbunden mit %s (Bild %s, PC-Sound %s, Mikro %s)", peer,
                 *("an" if self.stats[k] else "aus" for k in ("video", "audio", "mic")))
        try:
            while True:
                head = await reader.readexactly(5)
                kind, n = head[:1], struct.unpack(">I", head[1:])[0]
                if n > MAX_PACKET:
                    raise ValueError("Paket zu groß")
                data = await reader.readexactly(n)
                if kind == b"V":
                    self._on_jpeg(data)
                elif kind in (b"A", b"M"):
                    self._on_audio(kind.decode(), data)
        except (asyncio.IncompleteReadError, ConnectionError):
            pass
        except Exception as e:
            self.error = str(e)
            log.warning("Sync-Verbindung: %s", e)
        finally:
            self.connected = False
            log.info("🔗 Sync getrennt (%s)", peer)
            writer.close()

    def _on_jpeg(self, data: bytes) -> None:
        import cv2

        img = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR)
        if img is not None:
            self._frame, self._ts = img, time.time()

    def _on_audio(self, kind: str, data: bytes) -> None:
        q = self.audio_q[kind]
        pcm = np.frombuffer(data, dtype="<i2").astype(np.float32) / 32768.0
        for i in range(0, len(pcm) - BLOCK + 1, BLOCK):
            try:
                q.put_nowait(pcm[i:i + BLOCK])
            except queue.Full:  # Spracherkennung kommt nicht hinterher -> ältestes verwerfen
                try:
                    q.get_nowait()
                except queue.Empty:
                    pass

    def read_block(self, kind: str = "A", timeout: float = 1.0) -> np.ndarray:
        """Für audio.input: nächster 30-ms-Block (Stille, wenn nichts kommt)."""
        try:
            return self.audio_q[kind].get(timeout=timeout)
        except queue.Empty:
            return np.zeros(BLOCK, np.float32)

    def latest(self) -> np.ndarray | None:
        if self._frame is None or time.time() - self._ts > 5:
            return None
        return self._frame

    def grab(self) -> np.ndarray | None:  # wie ScreenCapture
        return self.latest()

    def status(self) -> dict:
        return {"role": "receive", "port": self.port, "code": self.code, "connected": self.connected, "peer": self.peer,
                "picture": self.latest() is not None, **self.stats, "error": self.error}


RECEIVER: SyncReceiver | None = None  # vom App-Start gesetzt; Bildquellen „sync“ greifen darauf zu


class SyncFeed:
    """Bildquelle „sync“ für Caster und Feeds."""

    def __init__(self, spec=None) -> None:
        self.spec = spec
        self.error = "" if RECEIVER else "Sync-Empfang ist aus (Einstellungen → Sync)"

    def latest(self):
        return RECEIVER.latest() if RECEIVER else None

    def close(self) -> None:
        pass


# ------------------------------------------------------------------ Sender (Gaming-PC)
class SyncSender:
    """Schickt Bildschirm und Ton an den Laptop. Läuft in eigenen Threads, braucht keine KI."""

    def __init__(self, host: str, code: str, port: int = PORT, video: bool = True, audio: bool = True,
                 mic: bool = False, monitor: int = 1, fps: float = 8.0, width: int = 1280, quality: int = 70) -> None:
        self.host, self.code, self.port = host.strip(), str(code).strip(), int(port)
        self.video, self.audio, self.mic = video, audio, mic
        self.monitor, self.fps, self.width, self.quality = monitor, fps, width, quality
        self.connected = False
        self.error = ""
        self.sent = {"V": 0, "A": 0, "M": 0}
        self._stop = threading.Event()
        self._sock: socket.socket | None = None
        self._lock = threading.Lock()
        self._threads: list[threading.Thread] = []

    def start(self) -> None:
        self._stop.clear()
        t = threading.Thread(target=self._run, daemon=True)
        t.start()
        self._threads = [t]

    def stop(self) -> None:
        self._stop.set()
        self._close()

    def _close(self) -> None:
        s, self._sock = self._sock, None
        self.connected = False
        if s:
            try:
                s.close()
            except OSError:
                pass

    def _send(self, kind: bytes, payload: bytes) -> bool:
        s = self._sock
        if s is None:
            return False
        try:
            with self._lock:
                s.sendall(pack(kind, payload))
            self.sent[kind.decode()] += 1
            return True
        except OSError as e:
            self.error = f"Verbindung verloren: {e}"
            self._close()
            return False

    def _connect(self) -> bool:
        if not private_peer(self.host) and not self.host.endswith(".local"):
            self.error = "Nur Adressen im Heimnetz (z. B. 192.168.x.x)"
            return False
        try:
            s = socket.create_connection((self.host, self.port), timeout=5)
            s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            hello = {"hello": "yvara-sync", "code": self.code, "video": self.video, "audio": self.audio, "mic": self.mic}
            s.sendall((json.dumps(hello) + "\n").encode())
            answer = b""
            while not answer.endswith(b"\n") and len(answer) < 200:
                chunk = s.recv(200)
                if not chunk:
                    break
                answer += chunk
            if b'"ok":true' not in answer:
                self.error = "Falscher Kopplungs-Code" if b"code" in answer else "Laptop hat abgelehnt"
                s.close()
                return False
            s.settimeout(None)
            self._sock, self.connected, self.error = s, True, ""
            log.info("🔗 Sync: verbunden mit %s:%s", self.host, self.port)
            return True
        except OSError as e:
            self.error = f"Laptop nicht erreichbar ({e}). Läuft Yvara AI dort mit Sync-Empfang?"
            return False

    def _run(self) -> None:
        while not self._stop.is_set():
            if not self._connect():
                self._stop.wait(3)
                continue
            workers = []
            if self.video:
                workers.append(threading.Thread(target=self._video_loop, daemon=True))
            if self.audio:
                workers.append(threading.Thread(target=self._audio_loop, args=("loopback", b"A"), daemon=True))
            if self.mic:
                workers.append(threading.Thread(target=self._audio_loop, args=("mic", b"M"), daemon=True))
            for w in workers:
                w.start()
            while self.connected and not self._stop.is_set():
                self._stop.wait(0.5)
            self._stop.wait(1)

    def _video_loop(self) -> None:
        import cv2
        import mss

        with mss.mss() as sct:
            mons = sct.monitors
            mon = mons[self.monitor] if 0 < self.monitor < len(mons) else mons[1 if len(mons) > 1 else 0]
            gap = 1.0 / max(1.0, self.fps)
            while self.connected and not self._stop.is_set():
                t0 = time.time()
                try:
                    img = np.asarray(sct.grab(mon))[:, :, :3]
                    h, w = img.shape[:2]
                    if w > self.width:
                        img = cv2.resize(img, (self.width, int(h * self.width / w)), interpolation=cv2.INTER_AREA)
                    ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, int(self.quality)])
                    if ok and not self._send(b"V", buf.tobytes()):
                        return
                except Exception as e:
                    self.error = f"Bildschirm: {e}"
                    time.sleep(1)
                time.sleep(max(0.0, gap - (time.time() - t0)))

    def _audio_loop(self, kind: str, tag: bytes) -> None:
        from .audio.input import _pick_device

        try:
            dev = _pick_device(kind, None)
            rec_cm = dev.recorder(samplerate=SR, channels=1, blocksize=BLOCK)
            rec = rec_cm.__enter__()
        except Exception as e:
            self.error = f"{'PC-Sound' if kind == 'loopback' else 'Mikrofon'}: {e}"
            log.warning("Sync: %s", self.error)
            return
        try:
            while self.connected and not self._stop.is_set():
                data = rec.record(numframes=BLOCK * 4)  # 120 ms pro Paket
                mono = data[:, 0] if data.ndim == 2 else data
                pcm = (np.clip(mono, -1, 1) * 32767).astype("<i2").tobytes()
                if not self._send(tag, pcm):
                    return
        finally:
            try:
                rec_cm.__exit__(None, None, None)
            except Exception:
                pass

    def status(self) -> dict:
        return {"role": "send", "host": self.host, "port": self.port, "connected": self.connected,
                "video": self.video, "audio": self.audio, "mic": self.mic, "sent": dict(self.sent), "error": self.error}


def sender_from_cfg(cfg) -> SyncSender:
    g = lambda k, d=None: cfg.get_path(f"sync.{k}", d)  # noqa: E731
    return SyncSender(str(g("host", "") or ""), str(g("code", "") or ""), int(g("port", PORT) or PORT),
                      video=bool(g("video", True)), audio=bool(g("audio", True)), mic=bool(g("mic", False)),
                      monitor=int(g("monitor", 1) or 1), fps=float(g("fps", 8) or 8), width=int(g("width", 1280) or 1280))
