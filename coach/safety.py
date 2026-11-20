"""Sicherheits-Regeln, technisch erzwungen (nicht nur per Prompt).

1. Netzwerk-Sperre: Yvara AI darf nur zu fest freigegebenen Diensten (Discord, Video-Lernen auf YouTube/Twitch,
   Modelle von Hugging Face, Updates vom eigenen GitHub-Repo). Claude-API, Edge-Stimmen und Websuche sind
   gesperrt, bis privacy.allow_cloud_ai / allow_cloud_voice / allow_web_search ausdrücklich true ist. Jede andere Adresse wird schon bei der Namensauflösung blockiert und geloggt.
2. Die KI kann nichts ausführen: es gibt keine Werkzeuge für Befehle, Dateien, Code oder Uploads. KI-Antworten
   werden nur als Text/Sprache verwendet. Websuche liefert nur Such-Schnipsel (keine Seiten-Aufrufe durch die KI).
3. Keine Selbst-Änderung: Updates kommen nur vom eingestellten GitHub-Repo (update.repo), nie aus KI-Antworten,
   und schützen eure Daten. Yvara AI lädt nichts von sich selbst irgendwohin hoch.
Abschalten (nicht empfohlen): safety.network_guard: false
"""
from __future__ import annotations

import ipaddress
import logging
import socket

log = logging.getLogger(__name__)

ALLOWED_SUFFIXES = (
    # Discord (Bot, Sprachkanal)
    "discord.com", "discord.gg", "discordapp.com", "discordapp.net", "discord.media",
    # Video-/Stream-Lernen
    "youtube.com", "youtu.be", "youtube-nocookie.com", "googlevideo.com", "ytimg.com", "ggpht.com",
    "youtubei.googleapis.com", "jnn-pa.googleapis.com",
    "twitch.tv", "ttvnw.net", "jtvnw.net", "twitchcdn.net", "twitchsvc.net", "live-video.net",
    # Modelle (Spracherkennung, Stimmen)
    "huggingface.co", "hf.co",
    # Updates vom eigenen Repo
    "api.github.com", "codeload.github.com", "github.com", "objects.githubusercontent.com",
)

# Dienste, an die eure Inhalte (Text, Gedächtnis, Bilder) gehen würden – standardmäßig GESPERRT.
# Nur freigeschaltet, wenn privacy.allow_* ausdrücklich auf true steht.
CLOUD_HOSTS = {
    "cloud_ai": ("api.anthropic.com",),          # Claude-API statt lokaler KI
    "cloud_voice": ("speech.platform.bing.com",),  # Edge-Stimmen (Text geht zu Microsoft)
    "web_search": ("duckduckgo.com",),          # Websuche (Suchbegriffe gehen raus)
}


def allowed(cfg, what: str) -> bool:
    """Darf Yvara AI Inhalte an diesen Online-Dienst schicken? Standard: nein (alles bleibt lokal)."""
    if cfg is None:
        return False
    return bool(cfg.get_path(f"privacy.allow_{what}", False))

_blocked_logged: set[str] = set()
_orig_getaddrinfo = socket.getaddrinfo
_extra: tuple[str, ...] = ()


def host_allowed(host: str | bytes | None) -> bool:
    if host is None:
        return True
    if isinstance(host, bytes):
        host = host.decode("ascii", "ignore")
    h = host.strip().rstrip(".").lower()
    if h in ("", "localhost") or h.endswith(".localhost"):
        return True
    try:
        ipaddress.ip_address(h.strip("[]"))
        return True  # direkte IPs: lokal (Ollama) und Discord-Sprachserver (kommen aus Discords Antwort, nicht von der KI)
    except ValueError:
        pass
    return any(h == s or h.endswith("." + s) for s in ALLOWED_SUFFIXES + _extra)


def _guarded_getaddrinfo(host, *args, **kwargs):
    if not host_allowed(host):
        name = host.decode() if isinstance(host, bytes) else str(host)
        if name not in _blocked_logged:
            _blocked_logged.add(name)
            log.warning("🔒 Sicherheit: Verbindung zu „%s“ blockiert (nicht freigegeben)", name)
        raise socket.gaierror(socket.EAI_NONAME, f"Yvara-AI-Sicherheit: {name} ist nicht freigegeben")
    return _orig_getaddrinfo(host, *args, **kwargs)


def install(cfg=None) -> bool:
    """Netzwerk-Sperre aktivieren (einmal beim Start). True = aktiv."""
    global _extra
    if cfg is not None and not cfg.get_path("safety.network_guard", True):
        log.warning("Netzwerk-Sperre ist AUS (safety.network_guard: false)")
        return False
    extra = list((cfg.get_path("safety.extra_hosts", []) if cfg is not None else []) or [])
    cloud = [h for what, hosts in CLOUD_HOSTS.items() if allowed(cfg, what) for h in hosts]
    _extra = tuple(str(x).lower().strip() for x in extra + cloud if str(x).strip())
    socket.getaddrinfo = _guarded_getaddrinfo
    log.info("🔒 Netzwerk-Sperre aktiv: nur Discord, Video-Lernen, Modelle, Updates%s",
             " und eigene Freigaben" if _extra else "")
    return True


def uninstall() -> None:
    socket.getaddrinfo = _orig_getaddrinfo
