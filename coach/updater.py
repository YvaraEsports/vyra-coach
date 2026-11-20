"""Auto-Update: holt beim Start (und per Knopf) die neueste Version von GitHub.

Eure Daten bleiben unangetastet: config.yaml, .env, data/teams, data/library,
Stimmen, Cache und die virtuelle Umgebung werden nie überschrieben.
Ändert sich requirements.txt, werden neue Pakete automatisch installiert.
Nach einem Update startet Yvara AI neu (Exit-Code 42, die Start-Skripte starten erneut).
"""
from __future__ import annotations

import io
import json
import logging
import os
import subprocess
import sys
import urllib.request
import zipfile
from pathlib import Path

from .config import ROOT

log = logging.getLogger(__name__)
VERSION_FILE = ROOT / "data" / "installed_version.json"
RESTART_CODE = 42
REQ_STAMP = ROOT / "data" / "requirements.sha"

# Wird nie überschrieben oder gelöscht
PROTECTED = (".env", "config.yaml", ".venv", ".git", "data/teams", "data/library", "data/voices", "data/tts_cache",
             "data/study_videos", "data/app_window", "data/logs", "data/active_team.txt", "data/installed_version.json", "data/requirements.sha", "reports")


def _protected(rel: str) -> bool:
    rel = rel.replace("\\", "/")
    return any(rel == p or rel.startswith(p + "/") for p in PROTECTED)


def _get(url: str, token: str | None, accept: str = "application/vnd.github+json") -> bytes:
    req = urllib.request.Request(url, headers={"Accept": accept, "User-Agent": "vyra-coach-updater"})
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(req, timeout=60) as r:
        return r.read()


def installed() -> dict:
    try:
        return json.loads(VERSION_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _settings(cfg) -> tuple[str, str, str | None]:
    repo = cfg.get_path("update.repo") or ""
    branch = cfg.get_path("update.branch") or "main"
    token = os.environ.get("GITHUB_TOKEN") or None  # nur für private Repos nötig
    return repo, branch, token


def check(cfg) -> dict:
    """{'available': bool, 'latest': sha, 'current': sha, 'message': str}"""
    repo, branch, token = _settings(cfg)
    if not repo:
        return {"available": False, "message": "Kein Update-Repo eingestellt (update.repo)"}
    data = json.loads(_get(f"https://api.github.com/repos/{repo}/commits/{branch}", token))
    latest = data["sha"]
    cur = installed().get("sha")
    return {"available": latest != cur, "latest": latest, "current": cur,
            "message": data.get("commit", {}).get("message", "").split("\n")[0]}


def apply_zip(zip_bytes: bytes, target: Path = ROOT) -> dict:
    """Zip (GitHub-Zipball) über die Installation kopieren. Gibt Infos über Änderungen zurück."""
    old_req = (target / "requirements.txt").read_bytes() if (target / "requirements.txt").exists() else b""
    changed = 0
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        names = [n for n in zf.namelist() if not n.endswith("/")]
        prefix = os.path.commonprefix(names).rsplit("/", 1)[0] + "/" if names and "/" in names[0] else ""
        for name in names:
            rel = name[len(prefix):]
            if not rel or _protected(rel):
                continue
            dest = target / rel
            data = zf.read(name)
            if dest.exists() and dest.read_bytes() == data:
                continue
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(data)
            if rel.endswith((".sh", ".command")):
                dest.chmod(0o755)
            changed += 1
    new_req = (target / "requirements.txt").read_bytes() if (target / "requirements.txt").exists() else b""
    return {"changed_files": changed, "requirements_changed": old_req != new_req}


def update(cfg, force: bool = False) -> dict:
    info = check(cfg)
    if not info.get("available") and not force:
        return {**info, "updated": False}
    repo, branch, token = _settings(cfg)
    log.info("Lade Update %s …", info.get("latest", "")[:7])
    z = _get(f"https://api.github.com/repos/{repo}/zipball/{branch}", token, accept="application/vnd.github+json")
    res = apply_zip(z)
    if res["requirements_changed"]:
        log.info("Neue Pakete werden installiert …")
        subprocess.run([sys.executable, "-m", "pip", "install", "-q", "-r", str(ROOT / "requirements.txt")], check=False)
    VERSION_FILE.parent.mkdir(parents=True, exist_ok=True)
    VERSION_FILE.write_text(json.dumps({"sha": info.get("latest"), "message": info.get("message")}), encoding="utf-8")
    log.info("Update installiert (%s Dateien): %s", res["changed_files"], info.get("message"))
    return {**info, **res, "updated": True}


def startup_update(cfg) -> bool:
    """Beim Start: Update prüfen und installieren. True = Neustart nötig."""
    if not cfg.get_path("update.auto", True) or not cfg.get_path("update.repo"):
        return False
    try:
        res = update(cfg)
        return bool(res.get("updated") and res.get("changed_files"))
    except Exception as e:
        if "404" in str(e):
            log.info("Auto-Update: GitHub-Repo „%s“ gibt es noch nicht – starte mit der aktuellen Version",
                     cfg.get_path("update.repo"))
        else:
            log.warning("Update-Prüfung fehlgeschlagen (%s) – starte mit der aktuellen Version", e)
        return False


def relaunch_desktop(delay_s: float = 2.0) -> bool:
    """Im App-Modus (ohne Start-Skript): neuen unsichtbaren Yvara-AI-Prozess starten. True = gestartet."""
    if not os.environ.get("VYRA_DESKTOP"):
        return False
    from .desktop import spawn_background

    args = [a for a in sys.argv[1:] if a in ("run", "local")] or ["run"]
    spawn_background(args + [a for a in sys.argv[1:] if a.startswith("--") and a != "--console"], delay_s)
    return True


def restart_now() -> None:
    """Neustart: im App-Modus neuen Hintergrund-Prozess starten, sonst Code 42 (Start-Skripte starten neu)."""
    try:
        if sys.stdout:
            sys.stdout.flush()
    except Exception:
        pass
    if relaunch_desktop():
        os._exit(0)
    os._exit(RESTART_CODE)



def ensure_requirements(req: Path | None = None, stamp: Path | None = None, runner=subprocess.run) -> bool:
    """Neue Pakete nachinstallieren, wenn sich requirements.txt geändert hat (z. B. nach manuellem ZIP-Update).
    True = pip wurde ausgeführt."""
    import hashlib

    req, stamp = req or ROOT / "requirements.txt", stamp or REQ_STAMP
    try:
        digest = hashlib.sha256(req.read_bytes()).hexdigest()
    except OSError:
        return False
    if stamp.exists() and stamp.read_text(encoding="utf-8").strip() == digest:
        return False
    log.info("Neue/aktualisierte Pakete werden installiert … (einmalig, kann 1–2 Minuten dauern)")
    res = runner([sys.executable, "-m", "pip", "install", "-q", "--disable-pip-version-check", "-r", str(req)], check=False)
    if getattr(res, "returncode", 1) == 0:
        stamp.parent.mkdir(parents=True, exist_ok=True)
        stamp.write_text(digest, encoding="utf-8")
    else:
        log.warning("Paket-Installation hat nicht ganz geklappt – Yvara AI startet trotzdem.")
    return True
