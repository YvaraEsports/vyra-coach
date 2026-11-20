"""Sprachausgabe: Text -> Audiodatei.

Engines:
  xtts   – lokal auf der Grafikkarte, EINE Stimme für Deutsch und Englisch, eigene Stimme klonbar
  piper  – lokal & offline, neuronale Stimmen, sehr schnell (Standard und Ersatz)
  edge   – online, sehr natürliche Microsoft-Stimmen
  system – Windows SAPI / macOS "say" (offline, simpel)
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import re
import subprocess
import sys
import threading
import wave
from pathlib import Path

from ..config import DATA_DIR

log = logging.getLogger(__name__)
CACHE = DATA_DIR / "tts_cache"

# Englische Begriffe, die in deutschen Sätzen englisch ausgesprochen werden sollen
EN_TERMS = [
    "search and destroy", "hardpoint", "control", "overload", "hijack", "respawn", "spawn", "spawns", "off spawn",
    "rotate", "rotation", "rotations", "push", "pushen", "pusht", "hold", "holden", "trade", "traden",
    "trades", "flank", "flanken", "one shot", "lit", "cracked", "low", "down", "plant", "planten", "defuse", "defusen",
    "bomb", "site", "a site", "b site", "hill", "streak", "streaks", "scorestreak", "uav", "counter uav", "gunfight",
    "rush", "nice", "top", "mid", "bottom", "bridge", "window", "stairs", "garage", "balcony", "heaven", "long",
    "short", "back", "headglitch", "head glitch", "power position", "setup", "setups", "rotate early", "time",
    "timeout", "callout", "callouts", "comms", "review", "playbook", "team", "teamfight", "fight", "map", "maps",
    "match", "round", "game", "gameplan", "coach", "slayer", "objective", "obj", "anchor", "flex", "smg", "ar",
    "sniper", "nade", "nades", "grenade", "flash", "stun", "smoke", "snake", "snaken", "peek", "peeken", "jump shot",
    "drop shot", "slide", "reload", "scrim", "scrims", "cdl", "ranked", "lobby", "pros", "skill", "aim",
]
_SUFFIX_DE = ("en", "t", "st")  # „pushen“, „pusht“ -> Stamm englisch, Endung bleibt


def _english_phonemes(term: str) -> str:
    """Englische Aussprache (IPA) eines Begriffs über die espeak-Engine, die Piper mitbringt."""
    import unicodedata

    from piper import voice as pv
    from piper.phonemize_espeak import EspeakPhonemizer

    with pv._ESPEAK_PHONEMIZER_LOCK:
        if pv._ESPEAK_PHONEMIZER is None:
            pv._ESPEAK_PHONEMIZER = EspeakPhonemizer(pv.ESPEAK_DATA_DIR) if hasattr(pv, "ESPEAK_DATA_DIR") \
                else EspeakPhonemizer()
        sents = pv._ESPEAK_PHONEMIZER.phonemize("en-us", term)
    ph = "".join(p for s in sents for p in s)
    return unicodedata.normalize("NFD", ph).strip(" .,!?;:")


_PH_CACHE: dict[str, str] = {}


def mark_english(text: str, extra: list[str] | None = None, phonemes=None) -> str:
    """Englische Begriffe in einem deutschen Satz als [[Lautschrift]] markieren -> Piper spricht sie englisch."""
    import re

    phonemes = phonemes or _english_phonemes
    terms = sorted({t.lower() for t in EN_TERMS + list(extra or []) if t.strip()}, key=len, reverse=True)
    if not terms:
        return text
    pat = re.compile(r"(?<![\w\[])(" + "|".join(re.escape(t) for t in terms) + r")(?![\w\]])([,.!?;:]?)", re.I)

    def rep(m) -> str:
        word = m.group(1)
        key = word.lower()
        stem = key
        for suf in _SUFFIX_DE:  # deutsche Endung an englischem Wort: Stamm englisch aussprechen
            base = key[: -len(suf)]
            if key.endswith(suf) and (base in _STEMS or base + "e" in _STEMS):
                stem = base if base in _STEMS else base + "e"
                break
        try:
            if stem not in _PH_CACHE:
                _PH_CACHE[stem] = phonemes(stem)
            ph = _PH_CACHE[stem]
        except Exception:
            return word
        if not ph:
            return word
        tail = word[len(stem):] if stem != key else ""
        punct = m.group(2) or ""
        if tail:
            return f"[[ {ph} ]]" + tail + punct
        return f"[[ {ph}{punct} ]]"  # Satzzeichen mit in den Block, sonst verschluckt Piper Pause/Komma

    return pat.sub(rep, text)


_STEMS = {"push", "hold", "trade", "flank", "plant", "defuse", "peek", "snake"}
VOICES_DIR = DATA_DIR / "voices"
CUSTOM_DIR = VOICES_DIR / "custom"      # eigene Stimmen (Referenzaufnahmen), bleiben bei Updates erhalten
XTTS_HOME = VOICES_DIR / "xtts"         # XTTS-Modell, bleibt bei Updates erhalten
XTTS_MODEL = "tts_models/multilingual/multi-dataset/xtts_v2"
XTTS_RATE = 24000

# Eingebaute XTTS-Sprecher (sprechen alle Deutsch UND Englisch mit derselben Stimme)
# Bewusst nur zwei eingebaute Stimmen (dazu kommen eure eigenen): Standard männlich + E-Girl.
XTTS_SPEAKERS = [
    ("Damien Black", "Yvara AI – männlich (Standard)"),
    ("Ana Florence", "Yvara Girl – weiblich, erwachsen (um die 30), flirty"),
]
# Supertonic: schnelle Stimmen ohne Grafikkarte (Deutsch + Englisch), kein Klonen eigener Stimmen
SUPERTONIC_VOICES = {f"M{i}": f"Supertonic männlich {i}" for i in range(1, 6)}
SUPERTONIC_VOICES.update({f"F{i}": f"Supertonic weiblich {i}" for i in range(1, 6)})
# Sprechstil der eingebauten Stimmen (wie .style.txt bei eigenen Stimmen)
BUILTIN_STYLES = {
    "Ana Florence": ("confident grown-up woman around 30 with a gamer-girl vibe: flirty and teasing just for fun (always PG-13, never explicit), "
                   "smooth, mature and self-assured, a teasing 'okay babe' only now and then, still gives clear calls"),
}
PIPER_PRESETS = [
    ("de_DE-thorsten-high", "Deutsch, männlich (Standard)"), ("en_US-ryan-high", "Englisch, männlich (Standard)"),
    ("de_DE-kerstin-low", "Deutsch, weiblich"), ("en_US-amy-medium", "Englisch, weiblich"),
]


def slug_voice(name: str) -> str:
    s = re.sub(r"[^a-z0-9_-]+", "_", name.strip().lower()).strip("_")
    return s[:40] or "stimme"


def custom_voices() -> list[str]:
    return sorted(p.stem for p in CUSTOM_DIR.glob("*.wav")) if CUSTOM_DIR.exists() else []


MAX_VOICE_SLOTS = 10


def voice_style(name: str) -> str:
    """Sprechstil einer eigenen Stimme (z. B. „sagt ab und zu Bro, locker“) – liegt neben der Stimmdatei."""
    try:
        return (CUSTOM_DIR / f"{slug_voice(name)}.style.txt").read_text(encoding="utf-8").strip()[:600]
    except OSError:
        return ""


def set_voice_style(name: str, style: str) -> None:
    CUSTOM_DIR.mkdir(parents=True, exist_ok=True)
    path = CUSTOM_DIR / f"{slug_voice(name)}.style.txt"
    style = (style or "").strip()[:600]
    if style:
        path.write_text(style, encoding="utf-8")
    else:
        path.unlink(missing_ok=True)


def xtts_installed() -> bool:
    try:
        import importlib.util

        return importlib.util.find_spec("TTS") is not None and importlib.util.find_spec("torch") is not None
    except Exception:
        return False


XTTS_HF_REPO = "models--tts-hub--XTTS-v2"  # coqui-tts ab 0.27 lädt XTTS in den Hugging-Face-Cache


def _hf_hub_dirs() -> list[Path]:
    dirs = [Path(os.environ[k]) for k in ("HF_HUB_CACHE", "HUGGINGFACE_HUB_CACHE") if os.environ.get(k)]
    if os.environ.get("HF_HOME"):
        dirs.append(Path(os.environ["HF_HOME"]) / "hub")
    dirs.append(Path.home() / ".cache" / "huggingface" / "hub")
    return dirs


def _model_complete(d: Path) -> bool:
    return all((d / f).exists() for f in ("model.pth", "config.json", "vocab.json"))


def xtts_model_dir() -> Path:
    """Modellordner finden: data/voices/xtts, alte Coqui-Ordner oder der Hugging-Face-Cache
    (dorthin lädt stimme_einrichten.bat mit coqui-tts 0.28 das Modell)."""
    name = XTTS_MODEL.replace("/", "--")
    candidates = [XTTS_HOME / "tts" / name]
    for env in ("LOCALAPPDATA", "XDG_DATA_HOME"):
        if os.environ.get(env):
            candidates.append(Path(os.environ[env]) / "tts" / name)
    candidates.append(Path.home() / ".local" / "share" / "tts" / name)
    for hub in _hf_hub_dirs():
        snaps = hub / XTTS_HF_REPO / "snapshots"
        try:
            candidates += sorted((d for d in snaps.iterdir() if d.is_dir()), key=lambda d: d.stat().st_mtime, reverse=True)
        except OSError:
            pass
    for c in candidates:
        if _model_complete(c):
            return c
    return candidates[0]


def xtts_license_ok() -> bool:
    """Coqui-Lizenz (CPML) hat der Nutzer beim Einrichten selbst bestätigt."""
    name = XTTS_MODEL.replace("/", "--")
    places = [XTTS_HOME / "tts" / name] + [Path(os.environ[e]) / "tts" / name for e in ("LOCALAPPDATA", "XDG_DATA_HOME")
                                           if os.environ.get(e)] + [Path.home() / ".local" / "share" / "tts" / name]
    return os.environ.get("COQUI_TOS_AGREED") == "1" or any((p / "tos_agreed.txt").exists() for p in places)


def xtts_status() -> dict:
    d = xtts_model_dir()
    st = {"installed": xtts_installed(), "model": _model_complete(d), "license": xtts_license_ok(), "dir": str(d)}
    st["ready"] = st["installed"] and st["model"] and st["license"]
    if not st["installed"]:
        st["problem"] = "Stimmen-Paket (coqui-tts) fehlt – stimme_einrichten.bat ausführen"
    elif not st["license"]:
        st["problem"] = "Lizenz noch nicht bestätigt – stimme_einrichten.bat ausführen und selbst mit y zustimmen"
    elif not st["model"]:
        st["problem"] = "Stimmenmodell (1,8 GB) nicht gefunden – stimme_einrichten.bat noch einmal ausführen"
    return st


def xtts_ready() -> bool:
    """Paket da, Modell heruntergeladen und Lizenz vom Nutzer bestätigt."""
    return xtts_status()["ready"]


def xtts_text(text: str) -> str:
    """Text so aufbereiten, dass XTTS sauber endet: ohne Satzzeichen am Ende „brabbelt“ es oft weiter (komische Geräusche)."""
    t = re.sub(r"[\u2600-\u27bf\U0001f000-\U0001faff]", "", text or "")  # Emojis
    t = re.sub(r"\.{2,}|…", ",", t)
    t = re.sub(r"[*_#~`<>\[\]{}|]", " ", t)
    t = re.sub(r"\s+", " ", t).strip(" ,;:-")
    if not t:
        return "Okay."
    if t[-1] not in ".!?":
        t += "."
    return t


def cap_length(wav, rate: int, text: str, speed: float = 1.0):
    """Längenbremse für kurze Sätze: XTTS dichtet bei „Okay.“ manchmal ein Wort dazu („Okay. Wine.“).
    Bei Texten bis 25 Zeichen wird alles nach der höchstens plausiblen Dauer abgeschnitten (mit Ausblenden)."""
    import numpy as np

    n = len((text or "").strip())
    if wav is None or n == 0 or n > 25:
        return wav
    limit = int((0.45 + 0.09 * n) / max(0.5, float(speed or 1.0)) * rate)
    if len(wav) <= limit:
        return wav
    out = np.asarray(wav)[:limit].copy()
    fade = min(len(out), int(0.06 * rate))
    if fade > 1:
        out[-fade:] *= np.linspace(1.0, 0.0, fade, dtype=out.dtype)
    return out


def trim_tail(wav, rate: int):
    """Nachlauf abschneiden: XTTS hängt manchmal Atmer/Rauschen/Brabbeln nach dem letzten Wort an."""
    import numpy as np

    if wav is None or len(wav) < rate // 4:
        return wav
    win = max(1, rate // 50)  # 20 ms
    n = len(wav) // win
    rms = np.sqrt(np.mean(wav[: n * win].reshape(n, win) ** 2, axis=1))
    loud = rms > max(0.02, float(rms.max()) * 0.12)
    idx = np.where(loud)[0]
    if not len(idx):
        return wav
    # letzter zusammenhängender Sprachblock; kurze Fetzen nach >350 ms Pause am Ende gelten als Artefakt
    last = idx[-1]
    gaps = np.where(np.diff(idx) > int(0.35 * 50))[0]
    if len(gaps):
        tail_start = idx[gaps[-1] + 1]
        if (last - tail_start) < int(0.25 * 50) and (n - tail_start) < int(1.2 * 50):
            last = idx[gaps[-1]]
    end = min(len(wav), (last + 1) * win + int(0.12 * rate))
    out = wav[:end].copy()
    fade = min(len(out), int(0.03 * rate))
    if fade > 1:
        out[-fade:] *= np.linspace(1.0, 0.0, fade, dtype=out.dtype)
    return out


class TTS:
    def __init__(self, cfg) -> None:
        self.cfg = cfg
        CACHE.mkdir(parents=True, exist_ok=True)
        VOICES_DIR.mkdir(parents=True, exist_ok=True)
        self._piper: dict[str, object] = {}
        self.extra_english: list[str] = []  # z. B. englische Map-Callouts („Top Bridge“)
        self._xtts = None
        self._xtts_lock = threading.Lock()
        self._xtts_failed: str | None = None
        self.last_used: dict = {}
        self.xtts_device = ""
        self._latents: dict[str, tuple] = {}
        self._warned: set[str] = set()

    def opt(self, key: str, default=None, opts: dict | None = None):
        """Einstellung lesen – beim Vorhören können Werte vorübergehend überschrieben werden."""
        if opts and opts.get(key) not in (None, ""):
            return opts[key]
        val = self.cfg.get_path(f"tts.{key}", default)
        return default if val in (None, "") else val

    @property
    def engine(self) -> str:
        return self.cfg.get_path("tts.engine", "piper")

    def xtts_speed(self, opts: dict | None = None) -> float:
        """XTTS-Tempo. Wichtig: XTTS klingt ab ~1.2 blechern/robotisch (die Sprachmerkmale werden gestaucht).
        Deshalb eigener Wert (tts.xtts_speed, Standard 1.05) und hart begrenzt – das Piper-Tempo (oft 1.5) gilt hier nicht."""
        try:
            v = float(self.opt("xtts_speed", 1.05, opts))
        except (TypeError, ValueError):
            v = 1.05
        return min(1.15, max(0.85, v))

    def speed(self, opts: dict | None = None) -> float:
        try:
            return min(2.0, max(0.5, float(self.opt("piper_speed", 1.0, opts))))
        except (TypeError, ValueError):
            return 1.0

    def _path(self, text: str, lang: str, ext: str, opts: dict | None = None, engine: str | None = None) -> Path:
        eng = engine or self.engine
        voice = self.xtts_voice(lang, opts) if eng == "xtts" else self.supertonic_voice(opts) if eng == "supertonic" \
            else self.opt("piper_" + lang, "", opts)
        if str(voice).startswith("custom:"):  # neu aufgenommene Stimme mit gleichem Namen -> neu erzeugen
            wav = CUSTOM_DIR / f"{slug_voice(str(voice)[7:])}.wav"
            voice = f"{voice}@{int(wav.stat().st_mtime) if wav.exists() else 0}"
        same = self.opt("same_voice", False, opts)
        key = f"{eng}|{lang}|mix5|{self.cfg.get_path('tts.rate')}|{voice}|{same}|{self.xtts_speed(opts) if eng == "xtts" else self.speed(opts)}|{text}"
        if opts and opts.get("xtts_temperature") not in (None, "") and eng == "xtts":  # Caster-Emotion
            key += f"|t{opts['xtts_temperature']}"
        return CACHE / f"{hashlib.sha1(key.encode()).hexdigest()[:16]}.{ext}"

    async def synth(self, text: str, lang: str, opts: dict | None = None) -> Path:
        return (await self.synth_info(text, lang, opts))["path"]

    async def synth_info(self, text: str, lang: str, opts: dict | None = None) -> dict:
        """Wie synth, sagt aber auch, welche Engine/Stimme wirklich gesprochen hat und was davor schiefging."""
        engine = (opts or {}).get("engine") or self.engine
        order = {"xtts": ["xtts", "supertonic", "piper", "system"], "supertonic": ["supertonic", "piper", "system"],
                 "piper": ["piper", "edge", "system"], "edge": ["edge", "piper", "system"]}.get(engine, ["system"])
        from ..safety import allowed

        if not allowed(self.cfg, "cloud_voice"):
            order = [e for e in order if e != "edge"] or ["piper", "system"]  # Edge schickt Text an Microsoft
        last_err, errors = None, {}
        for eng in order:
            try:
                if eng == "xtts":
                    path = await asyncio.to_thread(self._xtts_synth, text, lang, opts)
                elif eng == "supertonic":
                    path = await asyncio.to_thread(self._supertonic_synth, text, lang, opts)
                elif eng == "piper":
                    path = await asyncio.to_thread(self._piper_synth, text, lang, opts)
                elif eng == "edge":
                    path = await self._edge(text, lang)
                else:
                    path = await asyncio.to_thread(self._system, text, lang)
                voice = self.xtts_voice(lang, opts) if eng == "xtts" else self.opt("piper_" + lang, "", opts) \
                    if eng == "piper" else self.supertonic_voice(opts) if eng == "supertonic" else eng
                self.last_used = {"wanted": engine, "engine": eng, "voice": voice, "lang": lang, "errors": errors}
                return {"path": path, **self.last_used}
            except Exception as e:  # nächste Engine probieren
                last_err = e
                errors[eng] = str(e)
                msg = f"TTS-Engine {eng} fehlgeschlagen: {e}"
                if msg not in self._warned:  # nicht bei jedem Satz dieselbe Warnung
                    self._warned.add(msg)
                    log.warning(msg)
        raise RuntimeError(f"Keine Sprachausgabe möglich: {last_err}")

    # ----------------------------------------------------------------- xtts
    def reset_xtts(self) -> None:
        """Fehlerzustand vergessen (z. B. nach dem Einrichten) – nächster Satz versucht XTTS erneut."""
        self._xtts_failed = None
        self._warned = {w for w in self._warned if "xtts" not in w}

    def _xtts_load(self):
        if self._xtts is not None:
            return self._xtts
        if self._xtts_failed:
            raise RuntimeError(self._xtts_failed)
        st = xtts_status()
        if not st["ready"]:
            self._xtts_failed = f"XTTS nicht bereit: {st['problem']}"
            raise RuntimeError(self._xtts_failed)
        mdir = Path(st["dir"])
        try:
            import torch
            from TTS.tts.configs.xtts_config import XttsConfig
            from TTS.tts.models.xtts import Xtts

            dev = "cuda" if torch.cuda.is_available() else "cpu"
            log.info("Lade XTTS-Stimme aus %s (%s) …", mdir, "Grafikkarte" if dev == "cuda" else "CPU – langsam")
            conf = XttsConfig()
            conf.load_json(str(mdir / "config.json"))
            model = Xtts.init_from_config(conf)
            model.load_checkpoint(conf, checkpoint_dir=str(mdir), eval=True, use_deepspeed=False)
            try:
                model.to(dev)
            except Exception as e:  # Grafikkarte macht Probleme -> lieber langsamer auf der CPU als Standardstimme
                if dev == "cpu":
                    raise
                log.warning("XTTS auf der Grafikkarte nicht möglich (%s) – nutze CPU (langsamer)", e)
                dev = "cpu"
                model.to(dev)
            self.xtts_device = dev
        except Exception as e:
            log.exception("XTTS konnte nicht geladen werden")
            hint = (" – Yvara AI einmal komplett neu starten (die neue Version lädt die Grafikkarten-Bibliotheken in der "
                    "richtigen Reihenfolge). Hilft das nicht: stimme_einrichten.bat erneut ausführen."
                    if "WinError 127" in str(e) or "cudnn" in str(e).lower() else "")
            self._xtts_failed = f"XTTS konnte nicht geladen werden: {e}{hint}"
            raise RuntimeError(self._xtts_failed) from e
        self._xtts = model
        log.info("XTTS-Stimme bereit (eine Stimme für Deutsch und Englisch)")
        return model

    def xtts_speakers(self) -> list[str]:
        try:
            model = self._xtts
            return list(model.speaker_manager.speakers.keys()) if model is not None else []
        except Exception:
            return []

    def _xtts_latents(self, model, voice: str):
        """Stimm-Merkmale einmal berechnen und merken – sonst kostet jede Zeile extra Zeit."""
        if voice in self._latents:
            return self._latents[voice]
        if voice.startswith("custom:"):
            wav = CUSTOM_DIR / f"{slug_voice(voice[7:])}.wav"
            if not wav.exists():
                raise FileNotFoundError(f"Eigene Stimme fehlt: {wav.name}")
            # Standard nutzt nur die ersten 6 s der Probe -> klingt kaum nach der Person. Ganze Probe (bis 30 s)
            # in 4-s-Stücken mitteln: deutlich ähnlicher und stabiler.
            try:
                lat = model.get_conditioning_latents(audio_path=[str(wav)], gpt_cond_len=30, gpt_cond_chunk_len=4,
                                                     max_ref_length=30)
            except TypeError:  # ältere/fremde Modelle ohne diese Parameter
                lat = model.get_conditioning_latents(audio_path=[str(wav)])
        else:
            speakers = model.speaker_manager.speakers
            name = voice if voice in speakers else next(iter(speakers))
            sp = speakers[name]
            lat = (sp["gpt_cond_latent"], sp["speaker_embedding"])
        self._latents[voice] = (lat[0], lat[1])
        return self._latents[voice]

    def xtts_sampling(self, opts: dict | None = None) -> dict:
        """Etwas ruhigeres Sampling als Coqui-Standard (0.75): bleibt näher an der geklonten Stimme."""
        def num(key, default):
            try:
                return float(self.opt(key, default, opts))
            except (TypeError, ValueError):
                return default
        return {"temperature": num("xtts_temperature", 0.65), "top_p": num("xtts_top_p", 0.8),
                "repetition_penalty": num("xtts_repetition_penalty", 10.0)}

    def xtts_voice(self, lang: str, opts: dict | None = None) -> str:
        """Standard: eine Stimme für beide Sprachen. Optional eigene Stimme fürs Englische (xtts_voice_en)."""
        main = str(self.opt("xtts_voice", XTTS_SPEAKERS[0][0], opts))
        if lang == "en":
            if opts and opts.get("xtts_voice_en") is not None:  # Vorhören: leer heißt bewusst „gleiche Stimme“
                return str(opts["xtts_voice_en"] or main)
            return str(self.opt("xtts_voice_en", "", opts) or main)
        return main

    def forget_voice(self, voice: str) -> None:
        self._latents.pop(voice, None)

    def _xtts_synth(self, text: str, lang: str, opts: dict | None = None) -> Path:
        out = self._path(text, lang, "wav", opts, "xtts")
        if out.exists():
            return out
        import numpy as np

        with self._xtts_lock:  # eine Grafikkarte, ein Satz nach dem anderen
            model = self._xtts_load()
            voice = self.xtts_voice(lang, opts)
            gpt, spk = self._xtts_latents(model, voice)
            log.debug("XTTS spricht %s mit Stimme %s", lang, voice)
            res = model.inference(xtts_text(text), lang if lang in ("de", "en") else "de", gpt, spk,
                                  speed=self.xtts_speed(opts), enable_text_splitting=False,
                                  **self.xtts_sampling(opts))
        wav = res["wav"]
        wav = wav.detach().cpu().numpy() if hasattr(wav, "detach") else np.asarray(wav)
        wav = trim_tail(np.asarray(wav, dtype=np.float32).squeeze(), XTTS_RATE)
        wav = cap_length(wav, XTTS_RATE, xtts_text(text), self.xtts_speed(opts))
        pcm = (np.clip(wav, -1, 1) * 32767).astype("<i2")
        tmp = out.with_suffix(".tmp")
        with wave.open(str(tmp), "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(XTTS_RATE)
            wf.writeframes(pcm.tobytes())
        tmp.replace(out)
        return out

    # ---------------------------------------------------------------- piper
    def _piper_voice(self, lang: str, opts: dict | None = None):
        name = self.opt(f"piper_{lang}", "de_DE-thorsten-high" if lang == "de" else "en_US-ryan-high", opts)
        if name in self._piper:
            return self._piper[name]
        from piper import PiperVoice

        model = Path(name) if name.endswith(".onnx") else VOICES_DIR / f"{name}.onnx"
        if not model.exists():
            log.info("Lade Piper-Stimme %s herunter (einmalig) ...", name)
            subprocess.run([sys.executable, "-m", "piper.download_voices", "--download-dir", str(VOICES_DIR), name], check=True)
        voice = PiperVoice.load(str(model))
        self._piper[name] = voice
        return voice

    # ----------------------------------------------------------- supertonic
    def supertonic_voice(self, opts: dict | None = None) -> str:
        v = str(self.opt("supertonic_voice", "M1", opts))
        return v if v in SUPERTONIC_VOICES else "M1"

    def _supertonic_load(self):
        if getattr(self, "_supertonic", None) is None:
            from supertonic import TTS as _ST

            log.info("Lade Supertonic-Stimme (schnell, läuft auf der CPU) – beim ersten Mal wird das Modell geladen")
            self._supertonic = _ST(model="supertonic-3")
        return self._supertonic

    def _supertonic_synth(self, text: str, lang: str, opts: dict | None = None) -> Path:
        out = self._path(text, lang, "wav", opts, "supertonic")
        if out.exists():
            return out
        import numpy as np

        with self._xtts_lock:
            st = self._supertonic_load()
            style = st.get_voice_style(self.supertonic_voice(opts))
            wav, _dur = st.synthesize(xtts_text(text), voice_style=style, lang=lang if lang in ("de", "en") else "de",
                                      speed=min(1.3, max(0.8, self.speed(opts))))
        wav = np.asarray(wav, dtype=np.float32).squeeze()
        pcm = (np.clip(wav, -1, 1) * 32767).astype("<i2")
        tmp = out.with_suffix(".tmp")
        with wave.open(str(tmp), "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(int(st.sample_rate))
            wf.writeframes(pcm.tobytes())
        tmp.replace(out)
        return out

    def _piper_synth(self, text: str, lang: str, opts: dict | None = None) -> Path:
        out = self._path(text, lang, "wav", opts, "piper")
        if out.exists():
            return out
        from piper import SynthesisConfig

        same = bool(self.opt("same_voice", False, opts))
        voice = self._piper_voice("de" if same else lang, opts)
        syn = SynthesisConfig(length_scale=1.0 / self.speed(opts))
        spoken = text
        if same and lang == "en":
            # Gleiche (deutsche) Stimme, aber englische Aussprache über Lautschrift
            try:
                spoken = " ".join(f"[[ {_english_phonemes(part)}{punct} ]]"
                                  for part, punct in re.findall(r"([^.!?]+)([.!?]*)", text) if part.strip())
            except Exception as e:
                log.debug("Englische Lautschrift nicht möglich: %s", e)
        elif lang == "de" and self.cfg.get_path("tts.english_terms", True):
            try:
                spoken = mark_english(text, list(self.cfg.get_path("tts.english_words", []) or []) + self.extra_english)
            except Exception as e:
                log.debug("Englische Aussprache nicht möglich: %s", e)
        with wave.open(str(out), "wb") as wf:
            voice.synthesize_wav(spoken, wf, syn_config=syn)
        return out

    # ----------------------------------------------------------------- edge
    async def _edge(self, text: str, lang: str) -> Path:
        import edge_tts

        out = self._path(text, lang, "mp3")
        if not out.exists():
            voice = self.cfg.get_path(f"tts.voice_{lang}") or self.cfg.get_path("tts.voice_de")
            comm = edge_tts.Communicate(text, voice, rate=self.cfg.get_path("tts.rate", "+0%"))
            await comm.save(str(out))
        return out

    # --------------------------------------------------------------- system
    def _system(self, text: str, lang: str) -> Path:
        if sys.platform == "darwin":
            out = self._path(text, lang, "aiff")
            if not out.exists():
                voice = "Anna" if lang == "de" else "Daniel"
                subprocess.run(["say", "-v", voice, "-o", str(out), text], check=True)
            return out
        import pyttsx3

        out = self._path(text, lang, "wav")
        if out.exists():
            return out
        eng = pyttsx3.init()
        want = "german" if lang == "de" else "english"
        for v in eng.getProperty("voices"):
            label = f"{v.name} {getattr(v, 'languages', '')}".lower()
            if want in label or (lang == "de" and "deutsch" in label):
                eng.setProperty("voice", v.id)
                break
        eng.save_to_file(text, str(out))
        eng.runAndWait()
        return out
