"""Einstieg:  python -m coach <befehl>

  run            Coach starten (Discord + Hören + Sehen)
  local          ohne Discord: Chat-Oberfläche + Ausgabe über deine Lautsprecher
  calibrate      HUD-Bereiche (Score, Timer) per Maus festlegen
  devices        Audio-Geräte anzeigen
  test-tts       Testansage DE + EN
  test-mic       5 Sekunden aufnehmen und transkribieren
  test-capture   Screenshot + HUD-Lesung testen
  update         jetzt nach Updates suchen und installieren
  desktop        Yvara AI als App (eigenes Fenster, ohne Konsole) – das nutzt die Desktop-Verknüpfung
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys

from .config import load_config


_crash_file = None


def _file_log() -> None:
    """Zusätzlich in data/logs/vyra.log schreiben (hilft bei der Fehlersuche)."""
    from logging.handlers import RotatingFileHandler

    from .config import DATA_DIR

    try:
        (DATA_DIR / "logs").mkdir(parents=True, exist_ok=True)
        h = RotatingFileHandler(DATA_DIR / "logs" / "vyra.log", maxBytes=2_000_000, backupCount=3, encoding="utf-8")
        h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
        # Discord-Voice/E2EE-Details nur in die Datei (zur Fehlersuche), nicht ins Fenster
        h.addFilter(lambda r: r.levelno >= logging.INFO or r.name in ("discord.voice_state", "discord.voice_client")
                    or (r.name == "discord.gateway" and any(k in r.getMessage() for k in ("DAVE", "MLS", "transition"))))
        logging.getLogger().addHandler(h)
        for name in ("discord.voice_state", "discord.gateway", "discord.voice_client"):
            logging.getLogger(name).setLevel(logging.DEBUG)
        for ch in logging.getLogger().handlers:
            if ch is not h:
                ch.setLevel(logging.INFO)
        import faulthandler

        global _crash_file  # offen lassen: harte Abstürze (z. B. in Audio-Treibern) landen in data/logs/crash.log
        _crash_file = open(DATA_DIR / "logs" / "crash.log", "a", encoding="utf-8")
        faulthandler.enable(_crash_file)
    except OSError:
        pass


def _migrate_config(cfg) -> None:
    """Einmalige Umstellungen alter Einstellungen (config.yaml bleibt ansonsten unangetastet)."""
    from .config import save_user_value

    try:
        if cfg.get_path("llm.local_model") == "qwen3-vl:8b" and not cfg.get_path("llm.model_migrated"):
            # qwen3-vl denkt vor jeder Antwort lange nach -> viel zu langsam für Live-Coaching
            save_user_value(cfg, "llm.local_model", "gemma3:12b")
            save_user_value(cfg, "llm.model_migrated", True)
            logging.getLogger("coach").info("KI-Modell auf gemma3:12b umgestellt (schneller, versteht auch Bilder). "
                                            "Wird beim ersten Start automatisch geladen.")
        if cfg.get_path("coach.name") in ("Vyra", None, "") and not cfg.get_path("coach.renamed"):
            save_user_value(cfg, "coach.name", "Yvara AI")  # neuer Name; „Coach“ und „Vyra“ funktionieren weiter
            save_user_value(cfg, "coach.renamed", True)
    except Exception as e:
        logging.getLogger("coach").debug("Einstellungen nicht umgestellt: %s", e)


def _no_console_output() -> None:
    """pythonw (App-Modus) hat keine Konsole: Ausgaben in data/logs/console.log statt ins Leere."""
    if sys.stdout is not None and sys.stderr is not None:
        return
    from .config import DATA_DIR

    try:
        (DATA_DIR / "logs").mkdir(parents=True, exist_ok=True)
        f = open(DATA_DIR / "logs" / "console.log", "a", encoding="utf-8", buffering=1)
    except OSError:
        f = open(os.devnull, "w")
    if sys.stdout is None:
        sys.stdout = f
    if sys.stderr is None:
        sys.stderr = f


def main() -> None:
    _no_console_output()
    ap = argparse.ArgumentParser(prog="coach", description="Yvara AI – Coach")
    ap.add_argument("command", nargs="?", default="run",
                    choices=["run", "local", "calibrate", "devices", "test-tts", "test-mic", "test-capture", "update", "shortcut", "app", "desktop"])
    ap.add_argument("--config", default=None)
    ap.add_argument("--no-vision", action="store_true")
    ap.add_argument("--no-audio", action="store_true")
    ap.add_argument("--no-update", action="store_true", help="beim Start nicht nach Updates suchen")
    ap.add_argument("--console", action="store_true", help="zusätzlich Befehle im Terminal")
    ap.add_argument("--local", action="store_true", help="desktop/shortcut: ohne Discord")
    ap.add_argument("--caster", action="store_true", help="direkt im Caster-Modus starten (z. B. auf dem MacBook)")
    ap.add_argument("-v", "--verbose", action="store_true")
    a = ap.parse_args()
    logging.basicConfig(level=logging.DEBUG if a.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s", datefmt="%H:%M:%S")
    if not a.verbose:  # Netzwerk-Details (Modell-Downloads, API-Aufrufe) nicht ins Fenster schreiben
        for noisy in ("httpx", "httpx2", "httpcore", "httpcore2", "huggingface_hub", "urllib3", "faster_whisper"):
            logging.getLogger(noisy).setLevel(logging.WARNING)
    os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")
    _file_log()
    cfg = load_config(a.config)
    from .safety import install as _net_guard

    _net_guard(cfg)  # Netzwerk-Sperre: nur freigegebene Dienste

    if a.command == "desktop":
        from .desktop import run_desktop

        run_desktop(cfg, local=a.local)
        sys.exit(0)
    if a.command in ("run", "local"):
        delay = float(os.environ.pop("VYRA_START_DELAY", "0") or 0)
        if delay > 0:  # nach einem Update: alten Prozess erst ganz beenden lassen
            import time

            time.sleep(delay)
        _migrate_config(cfg)
        from . import updater

        if not getattr(sys, "frozen", False) and (os.environ.get("VIRTUAL_ENV") or sys.prefix != sys.base_prefix):
            updater.ensure_requirements()
        if not a.no_update and updater.startup_update(cfg):
            print("Update installiert – Yvara AI startet neu …")
            if updater.relaunch_desktop():
                sys.exit(0)
            sys.exit(updater.RESTART_CODE)
        if sys.platform == "win32":
            from .desktop import ensure_app_shortcut

            ensure_app_shortcut()
            from .audio.stt import preload_torch

            preload_torch()  # vor allem anderen, was CUDA-DLLs lädt
        from .app import run

        if a.caster:
            cfg.setdefault("caster", {})["autostart"] = True
        try:
            asyncio.run(run(cfg, use_discord=a.command == "run", use_vision=not a.no_vision, use_audio=not a.no_audio,
                            use_console=a.console))
        except KeyboardInterrupt:
            pass
        except Exception:
            logging.getLogger("coach").exception("Yvara AI ist abgestürzt")
            raise
    elif a.command == "update":
        from . import updater

        try:
            res = updater.update(cfg)
            print("Update installiert:" if res.get("updated") else "Schon aktuell:", res.get("message", ""))
        except Exception as e:
            print(f"Update fehlgeschlagen: {e} (Repo '{cfg.get_path('update.repo')}' erreichbar? Bei privatem Repo GITHUB_TOKEN in .env)")
    elif a.command == "shortcut":
        from .desktop import create_shortcut

        try:
            print("Verknüpfung angelegt:", create_shortcut(local=a.local))
        except Exception as e:
            print(f"Verknüpfung konnte nicht angelegt werden: {e}")
    elif a.command == "app":
        from .desktop import open_app

        open_app(f"http://127.0.0.1:{int(cfg.get_path('web.port', 8765))}/?v={__import__('coach').__version__}", True, str(cfg.get_path("web.app_browser", "chrome")))
    elif a.command == "calibrate":
        from .calibrate import calibrate

        calibrate(cfg)
    elif a.command == "devices":
        from .audio.input import list_devices

        print(list_devices())
    elif a.command == "test-tts":
        from .audio.output import LocalPlayer
        from .audio.tts import TTS

        async def _t():
            tts, pl = TTS(cfg), LocalPlayer()
            await pl.play(await tts.synth("Rotiert zum nächsten Hill, zwei Mann vor.", "de"))
            await pl.play(await tts.synth("Rotate to the next hill, send two early.", "en"))

        asyncio.run(_t())
    elif a.command == "test-mic":
        import soundcard as sc

        from .audio.stt import make_transcriber

        print("Sprich jetzt 5 Sekunden ...")
        audio = sc.default_microphone().record(samplerate=16000, numframes=16000 * 5, channels=1)[:, 0]
        tr = make_transcriber(cfg).transcribe_sync(audio)
        print(f"[{tr.language}] {tr.text}")
    elif a.command == "test-capture":
        import cv2

        from .vision.capture import ScreenCapture
        from .vision.hud import HudReader

        frame = ScreenCapture(cfg).grab()
        cv2.imwrite("capture_test.png", frame)
        print("Gespeichert: capture_test.png", frame.shape)
        print(HudReader(cfg).read(frame))
    sys.exit(0)


if __name__ == "__main__":
    main()
