"""Einfache Sprach-Segmentierung (VAD) auf Lautstärke-Basis mit adaptivem Rauschpegel.

Rein numpy, damit es überall läuft. Whisper filtert anschließend mit seinem
eigenen VAD noch Nicht-Sprache heraus.
"""
from __future__ import annotations

import numpy as np


class Segmenter:
    def __init__(
        self,
        sample_rate: int = 16000,
        threshold: float = 0.012,
        hangover_s: float = 0.7,
        min_speech_s: float = 0.35,
        max_segment_s: float = 15.0,
        preroll_s: float = 0.3,
    ) -> None:
        self.sr = sample_rate
        self.base_threshold = threshold
        self.noise = threshold / 2
        self.hangover = int(hangover_s * sample_rate)
        self.min_speech = int(min_speech_s * sample_rate)
        self.max_segment = int(max_segment_s * sample_rate)
        self.preroll = int(preroll_s * sample_rate)
        self._pre: list[np.ndarray] = []
        self._buf: list[np.ndarray] = []
        self._speech_samples = 0
        self._silence_run = 0
        self.active = False

    def _threshold(self) -> float:
        return max(self.base_threshold, self.noise * 2.5)

    def feed(self, frame: np.ndarray) -> list[np.ndarray]:
        """Ein Frame (mono float32) einspeisen, fertige Segmente zurückgeben."""
        out: list[np.ndarray] = []
        frame = np.asarray(frame, dtype=np.float32).reshape(-1)
        rms = float(np.sqrt(np.mean(frame**2))) if frame.size else 0.0
        is_speech = rms >= self._threshold()

        if not is_speech:
            # Rauschpegel langsam nachführen (nur in Stille)
            self.noise = 0.95 * self.noise + 0.05 * rms

        if not self.active:
            self._pre.append(frame)
            while sum(len(f) for f in self._pre) > self.preroll and len(self._pre) > 1:
                self._pre.pop(0)
            if is_speech:
                self.active = True
                self._buf = list(self._pre)
                self._pre = []
                self._speech_samples = len(frame)
                self._silence_run = 0
            return out

        self._buf.append(frame)
        if is_speech:
            self._speech_samples += len(frame)
            self._silence_run = 0
        else:
            self._silence_run += len(frame)

        total = sum(len(f) for f in self._buf)
        if self._silence_run >= self.hangover or total >= self.max_segment:
            if self._speech_samples >= self.min_speech:
                out.append(np.concatenate(self._buf))
            self.reset()
        return out

    def reset(self) -> None:
        self.active = False
        self._buf = []
        self._pre = []
        self._speech_samples = 0
        self._silence_run = 0
