"""Startet alles: Hören, Sehen, Denken, Sprechen, Discord."""
from __future__ import annotations

import asyncio
import logging
import os
import time

from .audio.output import DiscordPlayer, LocalPlayer, Voice
from .audio.tts import TTS
from .brain.coach import Coach
from .brain.knowledge import Knowledge
from .brain.llm import make_llm
from .teams import TeamManager

log = logging.getLogger(__name__)


async def run(cfg, use_discord: bool = True, use_vision: bool = True, use_audio: bool = True,
              use_console: bool = False) -> None:
    loop = asyncio.get_running_loop()
    role = str(cfg.get_path("sync.role", "off") or "off")
    if role == "send":  # Gaming-PC im Sync-Modus: nur Bild/Ton zum Laptop schicken, keine KI hier
        from .web.sender import run_sender

        await run_sender(cfg)
        return
    receiver = None
    if role == "receive":
        from . import sync

        receiver = sync.SyncReceiver(int(cfg.get_path("sync.port", sync.PORT) or sync.PORT))
        try:
            await receiver.start()
            sync.RECEIVER = receiver
        except OSError as e:
            log.error("Sync-Empfang konnte nicht starten (Port belegt?): %s", e)
            receiver = None
    output = cfg.get_path("tts.output", "discord") if use_discord else "local"
    player = DiscordPlayer() if output == "discord" else LocalPlayer()

    audio_in = None
    utter_q: asyncio.Queue = asyncio.Queue(maxsize=50)

    def on_speaking(active: bool) -> None:
        # Halbduplex: solange der Coach spricht, nicht auf Loopback/Mikro hören
        if audio_in:
            audio_in.speaking = active
            audio_in.muted_until = time.time() + (3600 if active else 0.4)

    tts = TTS(cfg)
    voice = Voice(tts, player, on_speaking=on_speaking)
    teams = TeamManager(cfg)

    capture = hud = None
    if use_vision:
        try:
            from .vision.capture import ScreenCapture
            from .vision.hud import HudReader

            capture, hud = ScreenCapture(cfg), HudReader(cfg)
        except Exception as e:
            log.warning("Bildschirmaufnahme nicht verfügbar: %s", e)

    if receiver is not None and cfg.get_path("sync.use_picture", True):
        capture = receiver  # Bild kommt vom Gaming-PC
        if hud is None:
            try:
                from .vision.hud import HudReader

                hud = HudReader(cfg)
            except Exception as e:
                log.warning("HUD-Erkennung nicht verfügbar: %s", e)

    stt = None
    audio_problem = ""
    if use_audio:
        try:
            from .audio.input import AudioInput
            from .audio.stt import make_transcriber

            stt = make_transcriber(cfg)
            audio_in = AudioInput(cfg, loop, utter_q)
            audio_in.on_barge_in = lambda: voice.interrupt()
        except Exception as e:
            log.warning("Spracheingabe nicht verfügbar: %s", e)
            audio_problem = str(e)[:200]

    bot = None
    notify = None
    if use_discord:
        from .discord_bot import CoachBot

        bot = CoachBot(cfg, player)
        notify = bot.notify

    coach = Coach(cfg, make_llm(cfg), teams, Knowledge(), voice, notify=notify, capture=capture, hud=hud, stt=stt)
    if bot:
        bot.coach = coach
        coach.bot = bot
    if audio_problem:
        coach.startup_problems["audio"] = audio_problem

    from .study.library import Library
    from .study.study import Studier

    coach.library = Library()
    coach.studier = Studier(cfg, coach.llm, coach.library, stt=stt, capture=capture, lang_fn=lambda: coach.lang, coach=coach)
    from .study.autolearn import AutoLearner

    coach.autolearn = AutoLearner(cfg, coach.llm, coach.library, coach.studier, coach)
    from .caster import Caster

    coach.caster = Caster(cfg, coach, tts, voice)
    from .vision.feeds import FeedManager

    try:
        coach.feeds = FeedManager.from_cfg(cfg)
    except Exception as e:
        log.warning("Feeds nicht verfügbar: %s", e)

    async def utter_worker() -> None:
        while True:
            utt = await utter_q.get()
            try:
                await coach.handle_utterance(utt)
            except Exception as e:
                log.exception("Fehler bei Spracheingabe: %s", e)

    async def warm_voice() -> None:
        """Stimme vorab laden, damit die erste Ansage nicht wartet."""
        try:
            t0 = time.time()
            await tts.synth("Okay.", coach.lang if coach.lang in ("de", "en") else "de")
            log.info("Stimme bereit (%.1fs)", time.time() - t0)
        except Exception as e:
            log.warning("Stimme konnte nicht vorgeladen werden: %s", e)

    from .perf import GamingMode

    gaming = GamingMode(cfg, coach)
    coach.gaming_mode = gaming

    async def keep_model_warm() -> None:
        """Ollama entlädt Modelle nach 5 Min. Leerlauf – regelmäßig vorwärmen, damit Antworten schnell kommen."""
        first = True
        try:
            await gaming.tick()  # läuft CoD schon? Dann gar nicht erst das große Modell laden
        except Exception:
            pass
        while True:
            llm = coach.llm
            if hasattr(llm, "ensure_model"):
                await llm.ensure_model()
            if hasattr(llm, "warmup"):
                t0 = time.time()
                ok = await llm.warmup()
                if first and ok:
                    log.info("KI-Modell geladen und bereit (%.0fs)", time.time() - t0)
                first = False
            await asyncio.sleep(240)

    async def autostart_learning() -> None:
        """Selbstständig weiterlernen, solange der PC an ist (pausiert automatisch während Matches)."""
        if not cfg.get_path("autolearn.autostart", True):
            return
        await asyncio.sleep(float(cfg.get_path("autolearn.autostart_delay_s", 90)))
        try:
            st = await coach.brain_status(force=True)
            if not st.get("ok", True):
                log.info("Hintergrund-Lernen wartet: KI noch nicht bereit")
                await asyncio.sleep(300)
            if not coach.autolearn.active:
                coach.autolearn.start(str(cfg.get_path("autolearn.topics", "") or ""))
                log.info("Hintergrund-Lernen gestartet (%s: %s)", coach.autolearn.game, ", ".join(coach.autolearn.modes))
        except Exception as e:
            log.warning("Hintergrund-Lernen nicht gestartet: %s", e)

    tasks = [asyncio.create_task(voice.run()), asyncio.create_task(utter_worker()),
             asyncio.create_task(keep_model_warm()), asyncio.create_task(warm_voice()),
             asyncio.create_task(autostart_learning())]
    tasks.append(asyncio.create_task(gaming.run()))
    from . import health

    tasks.append(asyncio.create_task(health.watch_loop(coach)))  # Selbst-Check: Probleme ehrlich melden
    tasks += [asyncio.create_task(coach.caster.run()), asyncio.create_task(coach.caster.fast_loop())]
    if cfg.get("coaching_mode") == "cast" or cfg.get_path("caster.autostart", False):
        coach.set_mode("cast", announce=False)
    if capture:
        tasks.append(asyncio.create_task(coach.hud_loop()))
    tasks.append(asyncio.create_task(coach.vision_loop()))
    if audio_in:
        audio_in.start()

    if cfg.get_path("web.enabled", True):
        from .web.server import WebUI

        port = int(cfg.get_path("web.port", 8765))
        try:
            await WebUI(cfg, coach, tts).start(port)
        except OSError as e:
            if getattr(e, "errno", None) in (98, 10048) or "10048" in str(e):
                log.error("Yvara AI läuft schon (Port %s ist belegt). Bitte das andere Yvara-AI-Fenster schließen und neu starten.",
                          port)
                if audio_in:
                    audio_in.stop()
                return
            raise
        if cfg.get_path("web.open_browser", True) and not os.environ.get("VYRA_DESKTOP"):
            from .desktop import open_app

            open_app(f"http://127.0.0.1:{port}/?v={__import__('coach').__version__}", bool(cfg.get_path("web.app_window", True)),
                     str(cfg.get_path("web.app_browser", "chrome")))
    log.info("%s ist bereit. Team: %s", coach.name, coach.team.name)

    try:
        token = (cfg.get("discord_token") or "").strip()
        if bot and token and not token.startswith("dein-"):
            ok = await start_discord(bot, token, cfg, coach)
            if ok:
                return
            voice.player = LocalPlayer()  # Discord ging nicht – Yvara AI läuft trotzdem weiter (Chat-App, Lernen …)
            await asyncio.Event().wait()
        else:
            if bot:
                log.warning("DISCORD_TOKEN fehlt in .env – Discord aus, nur Chat-Oberfläche + lokale Ausgabe.")
                voice.player = LocalPlayer()
            if use_console:
                await console(coach)
            else:
                await asyncio.Event().wait()  # läuft, bis Strg+C
    finally:
        if audio_in:
            audio_in.stop()
        for t in tasks:
            t.cancel()


async def start_discord(bot, token: str, cfg, coach) -> bool:
    """Discord-Bot starten. Fehler beenden NICHT das ganze Programm, sondern werden klar gemeldet.
    True = Bot lief und wurde normal beendet, False = Discord nicht verfügbar."""
    import discord

    from .discord_bot import CoachBot

    for attempt in range(2):
        try:
            await bot.start(token)
            return True
        except discord.LoginFailure:
            if coach is not None:
                coach.discord_error = "Discord-Token ungültig – neuen Token im Developer-Portal holen und in die .env eintragen"
            log.error("Discord-Token ungültig. Im Developer-Portal unter Bot → Reset Token einen neuen Token holen und "
                      "in der .env hinter DISCORD_TOKEN= einfügen (ohne Leerzeichen/Anführungszeichen). "
                      "Yvara AI läuft ohne Discord weiter.")
            break
        except discord.PrivilegedIntentsRequired:
            if attempt == 0 and cfg.get_path("discord.text_chat", True):
                log.warning("Im Developer-Portal ist der „Message Content Intent“ nicht eingeschaltet – starte ohne "
                            "@Yvara-AI-Textchat. (Developer-Portal → Bot → Message Content Intent einschalten, dann geht er.)")
                cfg.set_path("discord.text_chat", False)
                await bot.close()
                bot = CoachBot(cfg, bot.player)
                bot.coach = coach
                coach.bot = bot
                coach.notify = bot.notify
                continue
            log.error("Discord verlangt zusätzliche Rechte (Privileged Intents). Yvara AI läuft ohne Discord weiter.")
            if coach is not None:
                coach.discord_error = "Discord verlangt zusätzliche Rechte (Developer-Portal → Bot → Intents einschalten)"
            break
        except Exception as e:
            if coach is not None:
                coach.discord_error = f"Discord-Verbindung fehlgeschlagen: {e}"
            log.exception("Discord-Verbindung fehlgeschlagen (%s) – Yvara AI läuft ohne Discord weiter.", e)
            break
    try:
        await bot.close()
    except Exception:
        pass
    return False


async def console(coach: Coach) -> None:
    """Lokaler Modus ohne Discord: Befehle im Terminal."""
    help_text = ("Befehle: start <mode> <map> [gegner] | end [uns] [gegner] | plan <mode> <map> [gegner] | "
                 "review | ask <frage> | stats | profile <player|team|opponent> <name> | quit")
    print(help_text)
    while True:
        line = (await asyncio.to_thread(input, "coach> ")).strip()
        if not line:
            continue
        cmd, *args = line.split()
        try:
            if cmd == "quit":
                return
            elif cmd == "start":
                await coach.start_match(args[0] if args else "hardpoint", args[1] if len(args) > 1 else "", " ".join(args[2:]))
            elif cmd == "end":
                await coach.end_match(*(int(a) for a in args[:2]))
            elif cmd == "plan":
                print(await coach.make_plan(args[0], args[1] if len(args) > 1 else "", " ".join(args[2:])))
            elif cmd == "review":
                print(await coach.review_round(force=True))
            elif cmd == "ask":
                print(await coach.answer(" ".join(args)))
            elif cmd == "stats":
                print(coach.stats_text())
            elif cmd == "profile" and len(args) >= 2:
                print(coach.profile_text(args[0], " ".join(args[1:])))
            else:
                print(help_text)
        except Exception as e:
            print(f"Fehler: {e}")
