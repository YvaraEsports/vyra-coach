"""Selbst-Check: Was funktioniert bei Yvara AI gerade NICHT – und wie behebt man es?

Damit Yvara AI ehrlich sagt, wenn etwas kaputt ist (statt so zu tun, als ginge alles), und Schritt für Schritt
beim Beheben hilft – per Stimme und in der App (Start-Seite, Kasten „Probleme“).
"""
from __future__ import annotations

import asyncio
import logging
import time

log = logging.getLogger(__name__)


def _p(key: str, problem: str, fix: str, severity: str = "error") -> dict:
    return {"key": key, "problem": problem, "fix": fix, "severity": severity}


async def check(coach) -> list[dict]:
    """Alle Teile prüfen. Gibt nur Probleme zurück (leere Liste = alles gut)."""
    out: list[dict] = []
    cfg = coach.cfg

    # --- KI-Gehirn (Ollama / LM Studio / Claude)
    try:
        brain = await coach.brain_status()
    except Exception as e:
        brain = {"ok": False, "message": str(e)}
    if not brain.get("ok"):
        out.append(_p("brain", f"Mein KI-Gehirn antwortet nicht ({brain.get('message') or 'keine Verbindung'}).",
                      "1. Prüf, ob Ollama läuft (Ollama-App öffnen, unten rechts in der Taskleiste bzw. oben in der "
                      "Menüleiste). 2. In Einstellungen → KI das Modell prüfen; beim ersten Mal wird es heruntergeladen, "
                      "das dauert ein paar Minuten. 3. Danach Yvara AI neu starten."))
    elif time.time() - float(getattr(coach, "last_llm_fail", 0) or 0) < 300:
        out.append(_p("brain_slow", "Auf die letzte Frage kam von der KI keine Antwort.",
                      "Oft ist das Modell zu groß für den Grafikspeicher oder noch am Laden. In Einstellungen → KI ein "
                      "kleineres Modell wählen oder kurz warten und nochmal fragen. Spiele und andere KI-Apps belegen "
                      "auch Grafikspeicher.", "warn"))

    # --- Hören (Mikro + Spracherkennung)
    start = getattr(coach, "startup_problems", {}) or {}
    if start.get("audio"):
        out.append(_p("hearing", f"Ich kann euch nicht hören: {start['audio']}",
                      "1. In Einstellungen → Audio das richtige Mikrofon wählen. 2. Windows: Einstellungen → Datenschutz → "
                      "Mikrofon → Desktop-Apps erlauben; Mac: Systemeinstellungen → Datenschutz → Mikrofon → Terminal/Yvara "
                      "erlauben. 3. Yvara AI neu starten. Im Discord-Voice höre ich über den Loopback (was aus deinen "
                      "Lautsprechern kommt)."))
    elif getattr(coach, "stt", None) is None and cfg.get_path("audio.enabled", True):
        out.append(_p("hearing", "Meine Spracherkennung ist nicht geladen, ich höre also nichts.",
                      "Yvara AI über start_windows.bat bzw. start_mac.command neu starten – beim ersten Start wird die "
                      "Spracherkennung (~700 MB) geladen. Steht in der Konsole ein Fehler, schick ihn mir.", "warn"))

    # --- Sprechen (TTS)
    tts = getattr(getattr(coach, "voice", None), "tts", None)
    lu = getattr(tts, "last_used", None) or {}
    if isinstance(lu, dict) and lu.get("wanted") and lu.get("engine") and lu["wanted"] != lu["engine"]:
        err = next(iter((lu.get("errors") or {}).values()), "")
        out.append(_p("voice", f"Meine eingestellte Stimme ({lu['wanted']}) geht gerade nicht, ich spreche ersatzweise mit "
                               f"{lu['engine']}{(': ' + str(err)[:160]) if err else ''}.",
                      "Für XTTS: einmal gpu_einrichten.bat bzw. stimme_einrichten.bat ausführen (braucht eine NVIDIA-"
                      "Grafikkarte). Auf dem MacBook in Einstellungen → Stimme „Supertonic“ wählen – das ist schnell und "
                      "läuft ohne Grafikkarte.", "warn"))
    elif getattr(tts, "_xtts_failed", None) and str(cfg.get_path("tts.engine", "")) == "xtts":
        out.append(_p("voice", f"Meine XTTS-Stimme lädt nicht: {str(tts._xtts_failed)[:200]}",
                      "gpu_einrichten.bat bzw. stimme_einrichten.bat ausführen oder in Einstellungen → Stimme "
                      "„Supertonic“ wählen.", "warn"))

    # --- Discord
    if getattr(coach, "discord_error", ""):
        out.append(_p("discord", f"Discord geht nicht: {coach.discord_error}.",
                      "Den Discord-Token findest du im Discord Developer Portal → deine App → Bot → Reset Token. Den neuen "
                      "Token in die Datei .env bei DISCORD_TOKEN eintragen und Yvara AI neu starten."))
    else:
        bot = getattr(coach, "bot", None)
        try:
            ready = bot is None or bool(bot.is_ready())
        except Exception:
            ready = False
        if not ready and time.time() - float(getattr(coach, "started_at", 0) or 0) > 60:
            out.append(_p("discord", "Ich bin gerade nicht mit Discord verbunden.",
                          "Internet prüfen. Klappt es nach einer Minute nicht, Yvara AI neu starten. In der Konsole steht, "
                          "warum Discord abgelehnt hat.", "warn"))

    # --- Bild beim Zuschauen
    if getattr(coach, "watch", {}).get("on") and hasattr(coach, "watch_seeing"):
        ok, why = coach.watch_seeing()
        if not ok:
            src = str(coach.watch.get("source") or "")
            fix = ("Prüf, ob der Kanal gerade live ist und richtig geschrieben ist. Twitch hat 2–5 Sekunden Verzögerung."
                   if src.startswith("twitch:") else
                   "Das Fenster (z. B. die Discord-Übertragung) offen und sichtbar lassen, nicht minimieren. Ist das Bild "
                   "schwarz: in Discord Einstellungen → Erweitert → Hardwarebeschleunigung aus, oder im Tab Match als "
                   "Quelle den ganzen Bildschirm wählen. Discord-Übertragungen direkt kann ein Bot nicht sehen.")
            out.append(_p("picture", f"Ich sehe nichts: {why}.", fix))

    # --- Caster: Bildquelle + Twitch-Chat
    caster = getattr(coach, "caster", None)
    if caster is not None and getattr(caster, "active", False):
        try:
            src = caster.source_status()
        except Exception:
            src = {"ok": True}
        if not src.get("ok"):
            out.append(_p("cast_picture", f"Beim Casten kommt kein Bild an ({src.get('error') or caster.last_error or 'Quelle leer'}).",
                          "Im Tab Caster die Bildquelle prüfen: gleicher PC = „Bildschirm“; Spiel auf anderem PC = „Sync“ "
                          "oder OBS per SRT (Port 9000). Bei OBS muss die Aufnahme/der Stream laufen."))
        tw = getattr(caster, "twitch", None)
        if tw is not None and not tw.connected and tw.error:
            out.append(_p("twitch_chat", f"Den Twitch-Chat bekomme ich nicht: {tw.error}.",
                          "Im Tab Caster den Kanalnamen ohne https://twitch.tv/ eintragen. Internet prüfen.", "warn"))
    return out


def summary_for_llm(problems: list[dict]) -> str:
    if not problems:
        return ""
    lines = "\n".join(f"- {p['problem']} FIX: {p['fix']}" for p in problems)
    return ("YOUR OWN STATUS - THESE THINGS ARE BROKEN RIGHT NOW (be honest about them; never pretend they work. If the "
            "person asks about one of them or something fails, say plainly what is not working and walk them through "
            "the FIX one step at a time):\n" + lines)


def spoken_notice(problem: dict, lang: str) -> str:
    if lang == "de":
        return f"Kurz ehrlich: {problem['problem']} Sag „Coach, hilf mir das zu beheben“, dann führe ich dich durch."
    return "Quick heads up: something of mine is not working. Say \"Coach, help me fix it\" and I'll walk you through it."


async def watch_loop(coach, interval: float = 30.0) -> None:
    """Regelmäßig prüfen und NEUE Probleme einmal ehrlich ansagen (nicht während eines laufenden Matches)."""
    seen: set[str] = set()
    await asyncio.sleep(45)  # Start abwarten (Modelle laden, Discord verbindet)
    while True:
        try:
            probs = await check(coach)
            coach.health = probs
            keys = {p["key"] for p in probs}
            new = [p for p in probs if p["key"] not in seen and p["severity"] == "error"]
            for p in probs:
                if p["key"] not in seen:
                    log.warning("Selbst-Check: %s → %s", p["problem"], p["fix"])
            seen = keys
            live = coach.tracker.state.active and coach.mode == "live"
            if new and not live and coach.mode != "cast" and coach.cfg.get_path("health.announce", True):
                coach.say(spoken_notice(new[0], coach.lang), priority=1, max_age=30)
        except Exception as e:
            log.debug("Selbst-Check: %s", e)
        await asyncio.sleep(interval)
