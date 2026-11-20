"""Spracherkennung lokal mit faster-whisper. Erkennt Deutsch und Englisch automatisch."""
from __future__ import annotations

import asyncio
import logging
import sys
from dataclasses import dataclass

import numpy as np

log = logging.getLogger(__name__)

# Begriffe, die Whisper bei CoD-Comms helfen (Prompt-Bias)
COD_VOCAB = (
    "Hardpoint, Search and Destroy, SnD, Control, hill, P1, P2, rotate, rotation, spawn, "
    "trade, plant, defuse, bomb, A site, B site, flank, one shot, lit, cracked, UAV, "
    "Scorestreak, Streak, push, hold, Coach"
)


@dataclass
class Transcript:
    text: str
    language: str
    confidence: float
    probs: dict | None = None


_torch_preloaded = False


def preload_torch() -> None:
    """Windows: PyTorch (für XTTS) VOR der Spracherkennung laden.

    PyTorch bringt eigene cuDNN-DLLs mit. Lädt faster-whisper vorher die cuDNN-DLLs aus dem Paket
    nvidia-cudnn-cu12 (andere Version), findet PyTorch danach nicht mehr alles:
    „[WinError 127] … cudnn_cnn64_9.dll“ – und eigene Stimmen fallen auf Piper zurück.
    Lädt PyTorch zuerst, nutzen beide dieselben (passenden) DLLs."""
    global _torch_preloaded
    if _torch_preloaded or sys.platform != "win32":
        return
    _torch_preloaded = True
    import importlib
    import importlib.util

    if importlib.util.find_spec("torch") is None:
        return
    try:
        importlib.import_module("torch")

        log.debug("PyTorch vorgeladen (cuDNN aus torch\\lib)")
    except Exception as e:
        log.warning("PyTorch lässt sich nicht laden (%s) – eigene Stimmen (XTTS) gehen dann nicht. "
                    "Tipp: stimme_einrichten.bat noch einmal ausführen.", e)


def _add_nvidia_dll_dirs() -> None:
    """DLLs aus den pip-Paketen nvidia-cublas-cu12 / nvidia-cudnn-cu12 auffindbar machen (Windows)."""
    if sys.platform != "win32":
        return
    preload_torch()  # zuerst – sonst passen die cuDNN-Versionen nicht zusammen (WinError 127)
    import os
    import site

    roots = list(site.getsitepackages()) + [site.getusersitepackages()]
    for root in roots:
        for sub in ("nvidia/cublas/bin", "nvidia/cudnn/bin", "nvidia/cuda_nvrtc/bin"):
            d = os.path.join(root, sub)
            if os.path.isdir(d):
                try:
                    os.add_dll_directory(d)
                except OSError:
                    pass
                if d not in os.environ.get("PATH", ""):
                    os.environ["PATH"] = d + os.pathsep + os.environ.get("PATH", "")


def _has_nvidia_gpu() -> bool:
    try:
        import ctranslate2

        return ctranslate2.get_cuda_device_count() > 0
    except Exception:
        return False


CUDA_PACKAGES = ["nvidia-cublas-cu12", "nvidia-cudnn-cu12==9.*"]


def install_cuda_libs() -> bool:
    """Einmalig die CUDA-Bibliotheken für die Grafikkarte nachinstallieren (ca. 1 GB)."""
    import subprocess

    log.info("NVIDIA-Grafikkarte gefunden – lade die GPU-Bibliotheken für schnelle Spracherkennung "
             "(einmalig, ca. 1 GB, läuft im Hintergrund) …")
    res = subprocess.run([sys.executable, "-m", "pip", "install", "--disable-pip-version-check", "--progress-bar", "off",
                          *CUDA_PACKAGES], capture_output=True, text=True)
    if res.returncode != 0:
        out = ((res.stdout or "") + "\n" + (res.stderr or "")).strip()
        log.warning("GPU-Bibliotheken konnten nicht installiert werden (Code %s): %s", res.returncode, out[-600:])
        log.warning("Tipp: Yvara AI schließen und 'gpu_einrichten.bat' im Yvara-AI-Ordner doppelklicken.")
        return False
    _add_nvidia_dll_dirs()
    return True


def _cuda_libs_ok() -> bool:
    """Windows: faster-whisper braucht cuBLAS 12 + cuDNN 9 als DLLs, sonst stürzt es beim ersten Satz ab."""
    if sys.platform != "win32":
        return True
    import ctypes

    for dll in ("cublas64_12.dll", "cudnn_ops64_9.dll"):
        try:
            ctypes.WinDLL(dll)
        except OSError:
            log.info("%s nicht gefunden – Spracherkennung läuft auf der CPU", dll)
            return False
    return True


class Transcriber:
    def __init__(self, model_size: str = "small", device: str = "auto", auto_gpu: bool = True,
                 gpu_model: str | None = "large-v3-turbo") -> None:
        _add_nvidia_dll_dirs()
        from faster_whisper import WhisperModel

        self.model_size = model_size
        self.gpu_model = gpu_model or model_size  # auf der Grafikkarte: großes, schnelles Modell (genauer)
        self._lock = __import__("threading").Lock()
        want_gpu_later = False

        if device == "auto":
            try:
                import ctranslate2

                gpu = ctranslate2.get_cuda_device_count() > 0
                device = "cuda" if gpu and _cuda_libs_ok() else "cpu"
                want_gpu_later = gpu and device == "cpu" and auto_gpu and sys.platform == "win32"
            except Exception:
                device = "cpu"
        compute = "int8_float16" if device == "cuda" else "int8"  # GPU: wenig VRAM (neben Ollama + CoD)
        if device == "cuda":
            model_size = self.gpu_model
        log.info("Lade Whisper '%s' auf %s (%s)%s", model_size, device, compute,
                 " – beim ersten Mal wird das Modell heruntergeladen" if device == "cuda" else "")
        try:
            self.model = WhisperModel(model_size, device=device, compute_type=compute,
                                      cpu_threads=8 if device == "cpu" else 0)
            if device == "cuda":  # Probelauf: fehlende CUDA-Bibliotheken fallen sonst erst beim ersten Satz auf
                list(self.model.transcribe(np.zeros(16000, dtype=np.float32), beam_size=1)[0])
        except Exception as e:
            if device == "cpu":
                raise
            log.warning("Grafikkarte nicht nutzbar für Spracherkennung (%s) – nutze CPU", e)
            self.model = WhisperModel(model_size, device="cpu", compute_type="int8")
        self.device = "cuda" if device == "cuda" and getattr(self.model, "device", "cuda") == "cuda" else device
        log.info("Spracherkennung: %s", "GRAFIKKARTE aktiv (%s)" % self.gpu_model if self.device == "cuda" else
                 "CPU (%s)%s" % (model_size, " – GPU-Bibliotheken werden im Hintergrund geladen" if want_gpu_later
                                 else ""))
        if want_gpu_later:
            __import__("threading").Thread(target=self._upgrade_to_gpu, daemon=True).start()

    def _upgrade_to_gpu(self) -> None:
        """CPU läuft schon – im Hintergrund GPU-Bibliotheken holen und dann auf die Grafikkarte wechseln."""
        try:
            if not install_cuda_libs() or not _cuda_libs_ok():
                return
            from faster_whisper import WhisperModel

            log.info("Lade Whisper '%s' für die Grafikkarte …", self.gpu_model)
            gpu = WhisperModel(self.gpu_model, device="cuda", compute_type="int8_float16")
            list(gpu.transcribe(np.zeros(16000, dtype=np.float32), beam_size=1)[0])
            with self._lock:
                self.model, self.device = gpu, "cuda"
            log.info("Spracherkennung läuft jetzt auf der Grafikkarte – deutlich schneller.")
        except Exception as e:
            log.warning("Wechsel auf die Grafikkarte nicht möglich (%s) – bleibe auf der CPU", e)

    extra_vocab: str = ""  # Map-Callouts (z. B. "Top Bridge"), damit Whisper sie richtig schreibt

    def vocab(self) -> str:
        return COD_VOCAB + (", " + self.extra_vocab if self.extra_vocab else "")

    def _transcribe(self, audio: np.ndarray, language: str | None) -> Transcript:
        with self._lock:
            model = self.model
        segments, info = model.transcribe(
            audio,
            language=language,
            beam_size=1,
            vad_filter=True,
            initial_prompt=self.vocab(),
            condition_on_previous_text=False,
        )
        text = " ".join(s.text.strip() for s in segments).strip()
        probs = dict(getattr(info, "all_language_probs", None) or [])
        return Transcript(text=text, language=info.language, confidence=float(info.language_probability), probs=probs)

    def transcribe_sync(self, audio: np.ndarray, forced_language: str | None = None) -> Transcript:
        tr = self._transcribe(audio, forced_language)
        # Nur Deutsch/Englisch zulassen; sonst mit wahrscheinlicherer Sprache neu
        if forced_language is None and tr.language not in ("de", "en"):
            # Kurze Sätze landen manchmal bei Niederländisch o. Ä. -> wahrscheinlichere von Deutsch/Englisch nehmen
            p = tr.probs or {}
            tr = self._transcribe(audio, "en" if p.get("en", 0) > p.get("de", 0) else "de")
        return tr

    async def transcribe(self, audio: np.ndarray, forced_language: str | None = None) -> Transcript:
        return await asyncio.to_thread(self.transcribe_sync, audio, forced_language)

    def segments_sync(self, audio: np.ndarray) -> list[tuple[float, float, str]]:
        """Lange Aufnahmen (VODs, Caster-Kommentar) mit Zeitstempeln transkribieren."""
        segments, _info = self.model.transcribe(audio, beam_size=1, vad_filter=True, initial_prompt=self.vocab())
        return [(s.start, s.end, s.text.strip()) for s in segments]


def make_transcriber(cfg):
    """audio.stt_engine: parakeet (Standard, CPU/OpenVINO, kein Grafikspeicher) oder whisper (Grafikkarte)."""
    def whisper():
        return Transcriber(cfg.get_path("audio.stt_model", "small"), cfg.get_path("audio.stt_device", "auto"),
                           gpu_model=cfg.get_path("audio.stt_model_gpu", "large-v3-turbo"))

    engine = str(cfg.get_path("audio.stt_engine", "parakeet") or "parakeet").lower()
    if engine == "parakeet":
        try:
            from .stt_parakeet import ParakeetTranscriber

            return ParakeetTranscriber(whisper_factory=whisper, default_lang=cfg.get_path("default_language", "de"))
        except Exception as e:
            log.warning("Parakeet nicht verfügbar (%s) – nutze Whisper", e)
    return whisper()
