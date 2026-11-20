"""Sprach-Warteschlange mit Prioritäten + Ausgabe in Discord oder lokal.

Prioritäten: 3 = dringender Live-Call (unterbricht), 2 = Antwort auf Frage,
1 = Info, 0 = Smalltalk. Veraltete Live-Calls werden verworfen.
"""
from __future__ import annotations

import asyncio
import heapq
import itertools
import logging
import re
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

log = logging.getLogger(__name__)


_EMOJI = re.compile("[\U0001F000-\U0001FAFF\u2600-\u27BF\u2B00-\u2BFF\uFE0F\u200D]")
_STAGE = re.compile(r"[(*\[]\s*(haha+|hehe+|lol|lacht|lachend|grinst|zwinker\w*|laughs?|grins?|smiles?|winks?)[^)*\]]*[)*\]]",
                    re.I)


def clean_for_speech(text: str) -> str:
    """Emojis, „(haha)“, *grinst*, Markdown und Links vor dem Vorlesen entfernen."""
    t = re.sub(r"<think>.*?</think>", "", text or "", flags=re.S)
    t = _STAGE.sub("", t)
    t = re.sub(r"(?i)(^|[\s,.!?])(ha(ha)+|he(he)+|hihi+|lol|xd)(?=$|[\s,.!?])", r"\1", t)
    t = _EMOJI.sub("", t)
    t = re.sub(r"https?://\S+", "", t)
    t = re.sub(r"[*_#`>|~]+", "", t)
    return re.sub(r"\s+", " ", t).strip(" -–,;:")


def audio_seconds(path: Path) -> float | None:
    try:
        import wave

        with wave.open(str(path), "rb") as w:
            return w.getnframes() / float(w.getframerate() or 1)
    except Exception:
        return None


def describe_audio(path: Path, vc=None) -> str:
    """Kurzinfo für das Log: Länge/Größe der Datei + Discord-Verbindung (Verschlüsselung, Stummschaltung)."""
    p = Path(path)
    size = p.stat().st_size if p.exists() else 0
    sec = audio_seconds(p)
    parts = [f"{sec:.1f}s" if sec is not None else p.suffix, f"{size // 1024} KB"]
    if size < 2000:
        parts.append("⚠ Datei fast leer")
    if vc is not None:
        st = getattr(vc, "_connection", None)
        ver = getattr(st, "dave_protocol_version", None)
        if ver is not None:
            parts.append(f"E2EE {'an' if ver else 'aus'}" + ("" if not ver or getattr(st, "can_encrypt", True) else " (noch nicht bereit)"))
            if ver and not getattr(st, "can_encrypt", True):
                parts.append(dave_status(vc))
        me = getattr(getattr(vc, "guild", None), "me", None)
        vs = getattr(me, "voice", None)
        if vs is not None and (vs.mute or vs.suppress):
            parts.append("⚠ Yvara AI ist auf dem Server stummgeschaltet")
    return ", ".join(parts)


@dataclass(order=True)
class SpeechItem:
    sort_key: tuple = field(init=False, repr=False)
    priority: int
    text: str = field(compare=False)
    lang: str = field(compare=False, default="de")
    created: float = field(compare=False, default_factory=time.time)
    max_age: float = field(compare=False, default=8.0)
    seq: int = field(compare=False, default=0)
    opts: dict | None = field(compare=False, default=None)  # z. B. Caster-Stimme/Emotion

    def __post_init__(self) -> None:
        self.sort_key = (-self.priority, self.seq)

    def stale(self, now: float | None = None) -> bool:
        return ((now or time.time()) - self.created) > self.max_age


class SpeechQueue:
    def __init__(self) -> None:
        self._heap: list[SpeechItem] = []
        self._seq = itertools.count()
        self._event = asyncio.Event()

    def put(self, item: SpeechItem) -> None:
        item.seq = next(self._seq)
        item.__post_init__()
        heapq.heappush(self._heap, item)
        self._event.set()

    def pop_fresh(self) -> SpeechItem | None:
        now = time.time()
        while self._heap:
            item = heapq.heappop(self._heap)
            if not item.stale(now):
                return item
            log.info("Verworfen (zu alt): %s", item.text)
        self._event.clear()
        return None

    def clear_below(self, priority: int) -> int:
        """Alles mit niedrigerer Priorität verwerfen (z. B. Rest einer Antwort nach Unterbrechung)."""
        keep = [i for i in self._heap if i.priority >= priority]
        dropped = len(self._heap) - len(keep)
        self._heap = keep
        heapq.heapify(self._heap)
        if not self._heap:
            self._event.clear()
        return dropped

    def peek_priority(self) -> int:
        return self._heap[0].priority if self._heap else -1

    async def wait(self) -> None:
        await self._event.wait()

    def __len__(self) -> int:
        return len(self._heap)


class Player:
    """Basis: spielt eine Datei ab; stop() bricht ab."""

    async def play(self, path: Path) -> None:  # pragma: no cover - interface
        raise NotImplementedError

    def stop(self) -> None:
        pass

    @property
    def ready(self) -> bool:
        return True


def dave_status(vc) -> str:
    """Zustand der Discord-Ende-zu-Ende-Verschlüsselung (DAVE) als Text fürs Log."""
    st = getattr(vc, "_connection", None)
    ses = getattr(st, "dave_session", None)
    ver = getattr(st, "dave_protocol_version", 0)
    if not ver:
        return "E2EE aus"
    if ses is None:
        return f"E2EE v{ver}, keine Sitzung"
    try:
        users = len(ses.get_user_ids() or [])
    except Exception:
        users = "?"
    return (f"E2EE v{ver}, Status {getattr(ses, 'status', '?')}, bereit={getattr(ses, 'ready', '?')}, "
            f"Epoche {getattr(ses, 'epoch', '?')}, Mitglieder {users}, offene Übergänge {getattr(st, 'dave_pending_transitions', {})}")


def dave_ready(vc) -> bool:
    st = getattr(vc, "_connection", None)
    if st is None or not getattr(st, "dave_protocol_version", 0):
        return True
    return bool(getattr(st, "can_encrypt", False))


class DiscordPlayer(Player):
    def __init__(self) -> None:
        self.vc = None  # discord.VoiceClient, wird vom Bot gesetzt
        self.rejoin = None  # async Callback vom Bot: Voice neu verbinden
        self.wait_s = 10.0
        self._rejoined = False

    async def _ensure_encryption(self) -> bool:
        """Discord verschlüsselt Voice Ende-zu-Ende. Solange die Verschlüsselung nicht steht, verwirft Discord
        unser Audio stillschweigend – also warten und notfalls einmal neu verbinden."""
        for attempt in range(2):
            t0 = time.time()
            while time.time() - t0 < self.wait_s:
                if dave_ready(self.vc):
                    return True
                await asyncio.sleep(0.2)
            log.warning("Discord-Verschlüsselung nicht bereit (%s)", dave_status(self.vc))
            if attempt == 0 and self.rejoin and not self._rejoined:
                self._rejoined = True
                log.info("Verbinde Voice neu, damit die Verschlüsselung klappt …")
                try:
                    await self.rejoin()
                except Exception as e:
                    log.error("Neu verbinden fehlgeschlagen: %s", e)
                    return False
            else:
                break
        log.error("Discord-Verschlüsselung (E2EE) kommt nicht zustande – Audio würde nicht ankommen. "
                  "Bitte 'pip install -U \"discord.py[voice]\" davey' (passiert beim nächsten Start automatisch) "
                  "und data/logs/vyra.log schicken.")
        return False

    @property
    def ready(self) -> bool:
        return self.vc is not None and self.vc.is_connected()

    async def play(self, path: Path) -> None:
        import discord

        if not await self._ensure_encryption():
            return
        done = asyncio.Event()
        loop = asyncio.get_running_loop()
        src = discord.FFmpegPCMAudio(str(path))
        log.info("Spreche in Discord: %s", describe_audio(path, self.vc))

        def after(err) -> None:
            if err:
                log.error("Discord-Wiedergabe fehlgeschlagen: %s", err)
            loop.call_soon_threadsafe(done.set)

        self.vc.play(src, after=after)
        await done.wait()

    def stop(self) -> None:
        if self.vc and self.vc.is_playing():
            self.vc.stop()


class LocalPlayer(Player):
    """Spielt über die Standard-Lautsprecher (ffmpeg dekodiert)."""

    def __init__(self) -> None:
        self._stop = False

    async def play(self, path: Path) -> None:
        self._stop = False
        await asyncio.to_thread(self._play_sync, path)

    def _play_sync(self, path: Path) -> None:
        import numpy as np
        import soundcard as sc

        raw = subprocess.run(
            ["ffmpeg", "-v", "quiet", "-i", str(path), "-f", "f32le", "-ac", "1", "-ar", "24000", "-"],
            capture_output=True,
            check=True,
        ).stdout
        audio = np.frombuffer(raw, dtype=np.float32)
        spk = sc.default_speaker()
        with spk.player(samplerate=24000, channels=1) as p:
            for i in range(0, len(audio), 2400):
                if self._stop:
                    break
                p.play(audio[i : i + 2400])

    def stop(self) -> None:
        self._stop = True


class Voice:
    """Verbindet Queue, TTS und Player. Unterbricht für dringende Calls."""

    def __init__(
        self,
        tts,
        player: Player,
        on_speaking: Callable[[bool], None] | None = None,
    ) -> None:
        self.tts = tts
        self.player = player
        self.queue = SpeechQueue()
        self.on_speaking = on_speaking or (lambda _s: None)
        self.current: SpeechItem | None = None
        self.enabled = True
        self.spoken_log: list[tuple[float, str]] = []
        self.interrupted_at = 0.0
        self._pending: dict = {}

    def _prefetch(self, item: SpeechItem) -> None:
        """Stimme schon erzeugen, während noch der vorige Satz läuft -> keine Pause zwischen den Sätzen."""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        key = self._key(item)
        if key not in self._pending:
            self._pending[key] = loop.create_task(self._tts(item))

    @staticmethod
    def _key(item: SpeechItem):
        return (item.text, item.lang, repr(sorted((item.opts or {}).items())))

    def _tts(self, item: SpeechItem):
        return self.tts.synth(item.text, item.lang, item.opts) if item.opts else self.tts.synth(item.text, item.lang)

    async def _synth(self, item: SpeechItem):
        task = self._pending.pop(self._key(item), None)
        if task is not None:
            return await task
        return await self._tts(item)

    def interrupt(self) -> None:
        """Barge-in: jemand redet dazwischen -> sofort still sein und zuhören (dringende Calls laufen weiter)."""
        if self.current and self.current.priority >= 3:
            return
        dropped = self.queue.clear_below(3)
        if self.current or dropped:
            self.interrupted_at = time.time()
            log.info("Unterbrochen – ich höre zu (%d Sätze verworfen)", dropped)
            self.player.stop()

    def say(self, text: str, lang: str = "de", priority: int = 1, max_age: float = 8.0, opts: dict | None = None) -> None:
        text = clean_for_speech(text)
        if not text or not self.enabled and priority < 2:
            return
        item = SpeechItem(priority=priority, text=text, lang=lang, max_age=max_age, opts=opts)
        self._prefetch(item)
        # Dringender Call unterbricht weniger wichtige Ausgabe
        if self.current and priority >= 3 and self.current.priority < 3:
            self.player.stop()
        self.queue.put(item)

    async def run(self) -> None:
        while True:
            await self.queue.wait()
            item = self.queue.pop_fresh()
            if item is None:
                continue
            if not self.player.ready:
                log.info("[Coach, nicht im Voice-Channel] %s", item.text)
                continue
            try:
                t0 = time.time()
                path = await self._synth(item)
                if time.time() - t0 > 0.3:
                    log.info("Stimme erzeugt in %.1fs", time.time() - t0)
                # Nur dringende Live-Calls verfallen nach dem Erzeugen (Stimme laden kann beim 1. Mal dauern)
                if item.priority >= 3 and item.stale():
                    log.info("Verworfen (zu alt): %s", item.text)
                    continue
                self.current = item
                self.on_speaking(True)
                log.info("[Coach] %s", item.text)
                self.spoken_log.append((time.time(), item.text))
                await self.player.play(path)
            except Exception as e:
                log.error("Sprachausgabe fehlgeschlagen: %s", e)
            finally:
                self.current = None
                self.on_speaking(False)
