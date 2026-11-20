"""Audio-Eingang: Mikrofon und WASAPI-Loopback (hört die Discord-Comms über den PC mit).

Warum Loopback? Discord-Bots können wegen der Ende-zu-Ende-Verschlüsselung
(DAVE) aktuell nicht zuverlässig Stimmen im Voice-Channel empfangen. Dein
Discord-Client entschlüsselt die Comms aber ohnehin und spielt sie auf deinem
PC ab – genau das nimmt der Coach lokal ab.
"""
from __future__ import annotations

import asyncio
import logging
import sys
import threading
import time
import warnings
from dataclasses import dataclass

import numpy as np

from .segmenter import Segmenter

log = logging.getLogger(__name__)
# soundcard meldet bei kurzen Aussetzern „data discontinuity“ – harmlos, flutet aber die Konsole
warnings.filterwarnings("ignore", message=".*data discontinuity.*")
SR = 16000
BLOCK = 480  # 30 ms


@dataclass
class Utterance:
    audio: np.ndarray
    source: str  # "mic" | "loopback"
    ptt: bool    # während Push-to-Talk aufgenommen
    ts: float


def list_devices() -> str:
    import soundcard as sc

    lines = ["Mikrofone:"]
    lines += [f"  - {m.name}" for m in sc.all_microphones(include_loopback=False)]
    lines.append("Lautsprecher (für Loopback):")
    lines += [f"  - {s.name}" for s in sc.all_speakers()]
    return "\n".join(lines)


def _pick_device(kind: str, name: str | None):
    import sys

    import soundcard as sc

    if kind == "mic":
        if not name:
            return sc.default_microphone()
        return sc.get_microphone(name, include_loopback=False)
    # Loopback: Mikrofon-Objekt eines Lautsprechers.
    # macOS kann das nicht nativ -> virtuelles Gerät wie "BlackHole" als name angeben.
    if sys.platform == "darwin":
        if not name:
            raise RuntimeError("macOS: Für Loopback 'BlackHole' installieren und als audio.sources name eintragen")
        return sc.get_microphone(name, include_loopback=False)
    speaker_name = name or sc.default_speaker().name
    return sc.get_microphone(speaker_name, include_loopback=True)


def _norm_name(n: str) -> str:
    n = (n or "").replace("[Loopback]", "")
    try:  # manche PortAudio-Versionen liefern UTF-8 als Latin-1 („KopfhÃ¶rer“)
        n = n.encode("latin-1").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        pass
    return " ".join(n.lower().split())


def _name_match(want: str, have: str) -> bool:
    w, h = _norm_name(want), _norm_name(have)
    return bool(w) and bool(h) and (w in h or h in w)


def wasapi_device(pa, kind: str, name: str | None) -> dict | None:
    """WASAPI-Gerät für Mikrofon oder Loopback (Ton der Lautsprecher/Kopfhörer) finden."""
    import pyaudiowpatch as pyaudio

    api = pa.get_host_api_info_by_type(pyaudio.paWASAPI)
    devs = [pa.get_device_info_by_index(i) for i in range(pa.get_device_count())]
    devs = [d for d in devs if d.get("hostApi") == api["index"]]
    if kind == "mic":
        mics = [d for d in devs if d.get("maxInputChannels", 0) > 0 and not d.get("isLoopbackDevice")]
        if name:
            return next((d for d in mics if _name_match(name, d["name"])), None)
        idx = api.get("defaultInputDevice", -1)
        return next((d for d in mics if d["index"] == idx), mics[0] if mics else None)
    loops = [d for d in devs if d.get("isLoopbackDevice")]
    if name:
        return next((d for d in loops if _name_match(name, d["name"])), None)
    try:  # eingebaute Suche von PyAudioWPatch: Loopback des Standard-Ausgabegeräts
        d = pa.get_default_wasapi_loopback()
        if d:
            return d
    except Exception:
        pass
    idx = api.get("defaultOutputDevice", -1)
    out = next((d for d in devs if d["index"] == idx), None)
    if out:
        hit = next((d for d in loops if _name_match(out["name"], d["name"])), None)
        if hit:
            return hit
    return loops[0] if len(loops) == 1 else None


def to_mono_16k(raw: bytes, channels: int, rate: int) -> np.ndarray:
    """float32-Rohdaten (beliebige Kanäle/Abtastrate) -> Mono 16 kHz."""
    a = np.frombuffer(raw, dtype=np.float32)
    if channels > 1:
        a = a[: len(a) // channels * channels].reshape(-1, channels).mean(axis=1)
    if rate != SR and len(a):
        k = rate / SR
        if k >= 2:  # grob tiefpassen, damit beim Verkleinern nichts klirrt
            w = int(k)
            a = np.convolve(a, np.ones(w, dtype=np.float32) / w, mode="same")
        n = max(1, int(round(len(a) / k)))
        a = np.interp(np.linspace(0, len(a) - 1, n), np.arange(len(a)), a).astype(np.float32)
    return a


class AudioInput:
    """Startet je Quelle einen Thread und liefert Utterances in eine asyncio-Queue."""

    def __init__(self, cfg, loop: asyncio.AbstractEventLoop, queue: asyncio.Queue) -> None:
        self.cfg = cfg
        self.loop = loop
        self.queue = queue
        self.muted_until = 0.0  # Halbduplex: während der Coach spricht nicht zuhören (Loopback hört sie sonst selbst)
        self.speaking = False   # spricht Yvara AI gerade?
        self.on_barge_in = None  # Callback: jemand redet am Mikro, während Yvara AI spricht -> sie hört auf
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []
        self.ptt_key = cfg.get_path("audio.push_to_talk_key")
        self._ptt_state = False

    def mute_for(self, seconds: float) -> None:
        self.muted_until = max(self.muted_until, time.time() + seconds)

    def _ptt_pressed(self) -> bool:
        return bool(self.ptt_key) and self._ptt_state

    def _start_ptt(self) -> None:
        """Globaler Hotkey (Windows + macOS) via pynput. macOS: Bedienungshilfen-Recht nötig."""
        self._ptt_state = False
        if not self.ptt_key:
            return
        try:
            from pynput import keyboard
        except ImportError:
            log.warning("pynput fehlt – Push-to-Talk deaktiviert")
            return
        want = self.ptt_key.lower()

        def _name(key) -> str:
            return (getattr(key, "char", None) or getattr(key, "name", "") or "").lower()

        def on_press(key):
            if _name(key) == want:
                self._ptt_state = True

        def on_release(key):
            if _name(key) == want:
                self._ptt_state = False

        listener = keyboard.Listener(on_press=on_press, on_release=on_release)
        listener.daemon = True
        listener.start()

    def start(self) -> None:
        self._start_ptt()
        self._pa = None
        self._pa_lock = threading.Lock()
        if sys.platform == "win32":
            # Einmal im Haupt-Thread: PortAudio darf nicht parallel in mehreren Threads gestartet werden (sonst
            # stürzt Python ohne Meldung ab), und soundcard braucht COM in einem Thread, der dauerhaft lebt.
            try:  # zuerst soundcard (setzt COM für alle Threads), dann PortAudio
                __import__("soundcard")
            except Exception as e:
                log.info("soundcard nicht verfügbar: %s", e)
            try:
                import pyaudiowpatch as pyaudio

                self._pa = pyaudio.PyAudio()
            except Exception as e:
                log.info("WASAPI (PyAudioWPatch) nicht verfügbar: %s", e)
        from .. import sync

        rcv = sync.RECEIVER
        if rcv is not None:  # Sync: Ton vom Gaming-PC (PC-Sound = wie Loopback, PC-Mikro = wie Mikrofon)
            for kind, tag in (("loopback", "A"), ("mic", "M")):
                th = threading.Thread(target=self._listen, args=(kind, lambda t=tag: rcv.read_block(t)), daemon=True)
                th.start()
                self._threads.append(th)
        for src in self.cfg.get_path("audio.sources", []):
            th = threading.Thread(target=self._run, args=(src["type"], src.get("name")), daemon=True)
            th.start()
            self._threads.append(th)

    def stop(self) -> None:
        self._stop.set()

    def _run(self, kind: str, name: str | None) -> None:
        """Eine Audioquelle mithören. Fehler am Gerät beenden nie das Programm.
        Windows: zuerst WASAPI über PyAudioWPatch (kommt mit jedem Geräteformat klar), sonst soundcard."""
        try:
            if getattr(self, "_pa", None) is not None and self._run_wasapi(kind, name):
                return
            self._run_soundcard(kind, name)
        except BaseException as e:  # Audio darf Yvara AI nie beenden
            log.error("Audioquelle %s (%s) ausgefallen: %s – Yvara AI läuft ohne sie weiter.", kind, name or "Standard", e)

    def _retry(self, kind: str, dev_name: str, err: Exception, retries: int) -> bool:
        log.warning("Audioquelle %s (%s) unterbrochen: %s – verbinde neu (%d)", kind, dev_name, err, retries)
        if retries > 5:
            log.error("Audioquelle %s (%s) fällt immer wieder aus – Yvara AI hört hier nicht mehr zu.", kind, dev_name)
            return False
        time.sleep(min(2 * retries, 10))
        return not self._stop.is_set()

    def _run_wasapi(self, kind: str, name: str | None) -> bool:
        """True = Quelle lief (oder wurde bewusst beendet), False = hier nicht nutzbar -> soundcard versuchen."""
        import pyaudiowpatch as pyaudio

        pa = self._pa
        retries = 0
        while not self._stop.is_set():
            try:
                with self._pa_lock:
                    dev = wasapi_device(pa, kind, name)
                    if dev is None:
                        log.warning("WASAPI: %s-Gerät (%s) nicht gefunden – versuche es anders", kind, name or "Standard")
                        return False
                    rate = int(dev["defaultSampleRate"])
                    ch = max(1, int(dev["maxInputChannels"]))
                    n = int(round(BLOCK * rate / SR))
                    st = pa.open(format=pyaudio.paFloat32, channels=ch, rate=rate, input=True,
                                 input_device_index=dev["index"], frames_per_buffer=n)
            except Exception as e:
                log.warning("WASAPI: %s (%s) geht nicht auf: %s – versuche es anders", kind, name or "Standard", e)
                return False
            log.info("Höre auf %s: %s", kind, dev["name"])
            try:
                self._listen(kind, lambda: to_mono_16k(st.read(n, exception_on_overflow=False), ch, rate))
                return True
            except Exception as e:
                retries += 1
                if not self._retry(kind, dev["name"], e, retries):
                    return True
            finally:
                try:
                    with self._pa_lock:
                        st.close()
                except Exception:
                    pass
        return True

    def _run_soundcard(self, kind: str, name: str | None) -> None:
        retries = 0
        while not self._stop.is_set():
            try:
                dev = _pick_device(kind, name)
            except Exception as e:
                log.error("Audioquelle %s (%s) nicht gefunden: %s", kind, name or "Standard", e)
                return
            try:
                rec_cm = dev.recorder(samplerate=SR, channels=1, blocksize=BLOCK)
                rec = rec_cm.__enter__()
            except Exception as e:
                why = "Gerät nutzt ein Audioformat, das nicht unterstützt wird" if isinstance(e, AssertionError) else str(e)
                log.error("%s „%s“ lässt sich nicht öffnen (%s). Yvara AI läuft ohne diese Quelle weiter.",
                          "Mikrofon" if kind == "mic" else "Loopback", dev.name, why)
                return
            log.info("Höre auf %s: %s", kind, dev.name)
            try:
                self._listen(kind, lambda: rec.record(numframes=BLOCK))
                return
            except Exception as e:
                retries += 1
                if not self._retry(kind, dev.name, e, retries):
                    return
            finally:
                try:
                    rec_cm.__exit__(None, None, None)
                except Exception:
                    pass

    def _listen(self, kind: str, read) -> None:
        seg = Segmenter(SR, threshold=float(self.cfg.get_path("audio.vad_threshold", 0.012)),
                        hangover_s=float(self.cfg.get_path("audio.endpoint_s", 0.5)))
        mute_mic = bool(self.cfg.get_path("audio.mute_mic_while_speaking", False))
        barge = bool(self.cfg.get_path("audio.barge_in", True))
        barge_min = int(float(self.cfg.get_path("audio.barge_in_s", 0.35)) * SR)
        ptt_seen = False
        barged = False
        while not self._stop.is_set():
            data = read()
            frame = data[:, 0] if data.ndim == 2 else data
            # Loopback hört Yvaras eigene Stimme (über Discord) -> währenddessen stumm. Das Mikro bleibt an (Headset).
            if (kind == "loopback" or mute_mic) and time.time() < self.muted_until:
                seg.reset()
                continue
            if kind == "mic" and self.ptt_key:
                pressed = self._ptt_pressed()
                if not pressed and not seg.active:
                    ptt_seen = False
                    continue
                ptt_seen = ptt_seen or pressed
            segments = seg.feed(frame)
            if kind == "mic" and barge and self.speaking and seg.active and not barged \
                    and seg._speech_samples >= barge_min and self.on_barge_in:
                barged = True
                self.loop.call_soon_threadsafe(self.on_barge_in)
            if not seg.active:
                barged = False
            for audio in segments:
                utt = Utterance(audio=audio, source=kind, ptt=(kind == "mic" and ptt_seen), ts=time.time())
                self.loop.call_soon_threadsafe(self.queue.put_nowait, utt)
                ptt_seen = False
