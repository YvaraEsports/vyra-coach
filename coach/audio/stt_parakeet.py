"""Spracherkennung mit NVIDIA Parakeet TDT 0.6B v3 (Deutsch + Englisch) über ONNX – läuft auf der CPU.

Vorteil gegenüber Whisper auf der Grafikkarte: kein Grafikspeicher (mehr Platz für KI + XTTS + CoD), sehr schnell.
Mit OpenVINO (Paket onnxruntime-openvino) wird automatisch der OpenVINO-Beschleuniger benutzt, sonst normale CPU.
Für lange Videos (Fortbildung, mit Zeitstempeln) wird bei Bedarf weiterhin Whisper geladen.
"""
from __future__ import annotations

import asyncio
import logging
import re
import threading

import numpy as np

log = logging.getLogger(__name__)

PARAKEET_MODEL = "nemo-parakeet-tdt-0.6b-v3"

_DE_WORDS = set("""ich du er sie wir ihr nicht und oder aber ist bin bist sind war hab habe hast hat haben ein eine einen
der die das den dem des mit auf für was wie wo warum wann ja nein noch schon mal mich dich mir dir uns euch kein keine
jetzt hier da dann auch nur sehr gut geil alter digga bitte danke heute morgen gerade spiel spielen rotiert rotieren""".split())
_EN_WORDS = set("""i you he she we they not and or but is am are was were have has had a an the with on for what how
where why when yes no still already me my your us our this that there then also only very good please thanks today
now just do does did can could would should will game play rotate""".split())


def guess_language(text: str, default: str = "de") -> str:
    """Parakeet sagt nicht, welche Sprache es war -> grob an typischen Wörtern erkennen (nur de/en)."""
    t = (text or "").lower()
    if re.search(r"[äöüß]", t):
        return "de"
    words = re.findall(r"[a-z']+", t)
    de = sum(w in _DE_WORDS for w in words)
    en = sum(w in _EN_WORDS for w in words)
    if de == en:
        return default
    return "de" if de > en else "en"


class ParakeetTranscriber:
    """Gleiche Schnittstelle wie stt.Transcriber (transcribe/transcribe_sync/segments_sync/extra_vocab)."""

    extra_vocab: str = ""

    def __init__(self, whisper_factory=None, default_lang: str = "de") -> None:
        import onnx_asr
        import onnxruntime as ort

        avail = ort.get_available_providers()
        providers = [p for p in ("OpenVINOExecutionProvider",) if p in avail] + ["CPUExecutionProvider"]
        self.device = "openvino" if providers[0] == "OpenVINOExecutionProvider" else "cpu"
        log.info("Lade Parakeet-Spracherkennung (%s) – beim ersten Mal wird das Modell (~700 MB) geladen",
                 "OpenVINO" if self.device == "openvino" else "CPU")
        try:
            self.model = onnx_asr.load_model(PARAKEET_MODEL, quantization="int8", providers=providers)
        except Exception as e:  # nicht jede Variante hat int8 -> volle Genauigkeit
            log.debug("Parakeet int8 nicht verfügbar (%s) – lade Standard", e)
            self.model = onnx_asr.load_model(PARAKEET_MODEL, providers=providers)
        self._lock = threading.Lock()
        self._whisper = None
        self._whisper_factory = whisper_factory
        self.default_lang = default_lang
        self.model.recognize(np.zeros(16000, dtype=np.float32), sample_rate=16000)  # Probelauf
        log.info("Spracherkennung: Parakeet v3 auf %s (kein Grafikspeicher nötig)", self.device.upper())

    def transcribe_sync(self, audio: np.ndarray, forced_language: str | None = None):
        from .stt import Transcript

        audio = np.asarray(audio, dtype=np.float32).reshape(-1)
        with self._lock:
            text = str(self.model.recognize(audio, sample_rate=16000) or "").strip()
        lang = forced_language if forced_language in ("de", "en") else guess_language(text, self.default_lang)
        return Transcript(text=text, language=lang, confidence=0.9, probs=None)

    async def transcribe(self, audio: np.ndarray, forced_language: str | None = None):
        return await asyncio.to_thread(self.transcribe_sync, audio, forced_language)

    def segments_sync(self, audio: np.ndarray) -> list[tuple[float, float, str]]:
        """Lange Videos mit Zeitstempeln: Whisper erst dann laden, wenn es wirklich gebraucht wird."""
        if self._whisper is None and self._whisper_factory is not None:
            self._whisper = self._whisper_factory()
        if self._whisper is not None:
            self._whisper.extra_vocab = self.extra_vocab
            return self._whisper.segments_sync(audio)
        # Notlösung ohne Whisper: in 20-s-Stücken transkribieren
        out, step = [], 20 * 16000
        for i in range(0, len(audio), step):
            part = np.asarray(audio[i:i + step], dtype=np.float32)
            if len(part) < 8000:
                continue
            with self._lock:
                txt = str(self.model.recognize(part, sample_rate=16000) or "").strip()
            if txt:
                out.append((i / 16000, (i + len(part)) / 16000, txt))
        return out
