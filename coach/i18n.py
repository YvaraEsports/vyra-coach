"""Feste Texte auf Deutsch und Englisch (für Regel-Calls und Bot-Meldungen)."""
from __future__ import annotations

STRINGS: dict[str, dict[str, str]] = {
    "hello": {
        "de": "Hey, what's up, Jungs! {name} ist da. Erst Spaß, dann Arbeit – ich hör zu.|"
              "Yo, was geht! {name} im Channel. Wer heute nicht tradet, zahlt die Pizza. Los geht's.|"
              "Moin Leute, {name} hier. Ich hab Popcorn und Notizblock dabei. Zeigt mir was.",
        "en": "Hey, what's up, boys! {name} is in. Fun first, then work – I'm listening.|"
              "Yo, what's good! {name} here. Whoever doesn't trade today buys the pizza. Let's go.|"
              "Hey team, {name} in the house. Got popcorn and a notepad. Show me something.",
    },
    "bye": {"de": "Alles klar, bin raus. Gut gespielt!", "en": "Alright, I'm out. Well played!"},
    "muted": {"de": "Okay, ich bin ruhig.", "en": "Okay, going quiet."},
    "unmuted": {"de": "Ich bin wieder da.", "en": "I'm back on calls."},
    "hill_rotate": {
        "de": "Hill dreht in {s} Sekunden. Zwei Mann vorrotieren, Rest hält.",
        "en": "Hill rotates in {s}. Send two to pre-rotate, rest hold.",
    },
    "time_low": {
        "de": "Noch {s} Sekunden. Jetzt Entscheidung treffen.",
        "en": "{s} seconds left. Make the call now.",
    },
    "score_swing": {
        "de": "Die holen auf, {n} Punkte in kurzer Zeit. Reset, zusammen spielen, Trades!",
        "en": "They're making a run, {n} points fast. Reset, play together, trade!",
    },
    "round_won": {"de": "Runde gewonnen. Stand {ours} zu {enemy}.", "en": "Round won. {ours} to {enemy}."},
    "round_lost": {
        "de": "Runde verloren. Stand {ours} zu {enemy}. Kopf hoch, kurz sammeln.",
        "en": "Round lost. {ours} to {enemy}. Heads up, regroup.",
    },
    "match_point_us": {"de": "Matchpoint für uns. Diszipliniert bleiben.", "en": "Match point us. Stay disciplined."},
    "match_point_them": {
        "de": "Matchpoint für die. Wir brauchen jetzt Struktur, nicht Hero-Plays.",
        "en": "Their match point. We need structure, not hero plays.",
    },
    "match_started": {
        "de": "Match gestartet: {mode} auf {map}. Ich tracke alles.",
        "en": "Match started: {mode} on {map}. Tracking everything.",
    },
    "match_ended": {
        "de": "Match beendet. Ich bereite das Review vor.",
        "en": "Match over. Preparing the review.",
    },
    "thinking_error": {
        "de": "Ich hab gerade keine Verbindung zum Gehirn. Versuch's gleich nochmal.",
        "en": "Lost my brain connection for a sec. Try again shortly.",
    },
}


def t(key: str, lang: str = "de", **kw) -> str:
    """Text holen; mehrere Varianten (mit | getrennt) werden zufällig gewählt."""
    import random

    entry = STRINGS.get(key, {})
    text = entry.get(lang) or entry.get("de") or key
    text = random.choice(text.split("|"))
    try:
        return text.format(**kw)
    except KeyError:
        return text


def norm_lang(lang: str | None, default: str = "de") -> str:
    if not lang:
        return default
    lang = lang.lower()[:2]
    return lang if lang in ("de", "en") else default
