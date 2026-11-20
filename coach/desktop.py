"""Yvara AI als Desktop-App: eigenes Fenster (ohne Tabs/Adressleiste) und Desktop-Verknüpfung.

Standard unter Windows: „python -m coach desktop“ über pythonw (ohne schwarzes Konsolenfenster).
  1. Startet Yvara AI unsichtbar im Hintergrund (eigener Prozess, Log in data/logs).
  2. Öffnet ein echtes App-Fenster (pywebview / WebView2). Fenster zu = Yvara AI beendet.
Fallback, falls pywebview fehlt: Edge oder Chrome im App-Modus mit eigenem Profil (data/app_window).
"""
from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys
import webbrowser
from pathlib import Path

from .config import DATA_DIR, ROOT

log = logging.getLogger(__name__)
PROFILE = DATA_DIR / "app_window"
ICON = DATA_DIR / "vyra.ico"


WIN_PATHS = {
    "chrome": r"Google\Chrome\Application\chrome.exe",
    "edge": r"Microsoft\Edge\Application\msedge.exe",
    "brave": r"BraveSoftware\Brave-Browser\Application\brave.exe",
}
MAC_APPS = {"chrome": "Google Chrome", "edge": "Microsoft Edge", "brave": "Brave Browser"}


def _order(prefer: str) -> list[str]:
    keys = ["chrome", "edge", "brave"]  # Standard: Chrome zuerst
    prefer = (prefer or "auto").lower()
    return [prefer] + [k for k in keys if k != prefer] if prefer in keys else keys


def _windows_browsers(prefer: str = "auto") -> list[str]:
    roots = [os.environ.get(k, "") for k in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA")]
    return [str(Path(r) / WIN_PATHS[k]) for k in _order(prefer) for r in roots if r and (Path(r) / WIN_PATHS[k]).exists()]


def _mac_browsers(prefer: str = "auto") -> list[str]:
    apps = [MAC_APPS[k] for k in _order(prefer)] + ["Chromium"]
    return [a for a in apps if Path(f"/Applications/{a}.app").exists()]


def _linux_browsers() -> list[str]:
    return [p for p in (shutil.which(n) for n in ("chromium", "chromium-browser", "google-chrome", "microsoft-edge")) if p]


def app_window_command(url: str, prefer: str = "auto") -> list[str] | None:
    args = [f"--app={url}", f"--user-data-dir={PROFILE}", "--window-size=1400,900", "--no-first-run",
            "--no-default-browser-check"]
    if sys.platform == "win32":
        found = _windows_browsers(prefer)
        return [found[0], *args] if found else None
    if sys.platform == "darwin":
        found = _mac_browsers(prefer)
        return ["open", "-na", found[0], "--args", *args] if found else None
    found = _linux_browsers()
    return [found[0], *args] if found else None


def open_app(url: str, app_window: bool = True, prefer: str = "auto") -> None:
    """Yvara AI in einem eigenen App-Fenster öffnen (Fallback: normaler Browser). prefer: chrome | edge | brave | auto"""
    cmd = app_window_command(url, prefer) if app_window else None
    if cmd:
        try:
            PROFILE.mkdir(parents=True, exist_ok=True)
            flags = 0x00000008 if sys.platform == "win32" else 0  # DETACHED_PROCESS
            subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=flags)
            return
        except Exception as e:
            log.warning("App-Fenster ging nicht auf (%s) – öffne im Browser", e)
    webbrowser.open(url)


WEBVIEW_DATA = DATA_DIR / "app_webview"
SHORTCUT_STAMP = DATA_DIR / "shortcut_version.txt"
SHORTCUT_VERSION = "desktop-app-3-yvara-ai"

SPLASH = """<!doctype html><html><head><meta charset="utf-8"><style>
body{margin:0;height:100vh;display:flex;align-items:center;justify-content:center;flex-direction:column;
background:#0b0d12;color:#e8ebf2;font-family:Segoe UI,system-ui,sans-serif}
.l{width:96px;height:96px;animation:p 1.6s infinite}
@keyframes p{50%{transform:scale(.93);opacity:.8}} p{color:#8b93a7}</style></head><body>
<img class="l" src="__LOGO__" alt="Yvara AI"><h2>Yvara AI startet …</h2><p id="m">KI, Stimme und Discord werden geladen.</p>
<script>
const url = "__URL__"; let n = 0;
async function go(){ n++; try { await fetch(url.split("?")[0] + "api/state", {mode: "no-cors", cache: "no-store"});
  location.replace(url); } catch(e) { if (n > 90) document.getElementById("m").textContent =
  "Yvara AI braucht ungewöhnlich lange – Details in data/logs/vyra.log"; setTimeout(go, 1000); } }
go();
</script></body></html>"""


def server_up(port: int, timeout: float = 0.5) -> bool:
    import socket

    try:
        with socket.create_connection(("127.0.0.1", port), timeout=timeout):
            return True
    except OSError:
        return False


def pythonw() -> str:
    """pythonw.exe neben dem aktuellen Python (startet ohne Konsolenfenster)."""
    exe = Path(sys.executable)
    w = exe.with_name("pythonw.exe")
    return str(w if w.exists() else exe)


def spawn_background(args: list[str], delay_s: float = 0.0) -> None:
    """Yvara-AI-Prozess unsichtbar starten (ohne Konsolenfenster)."""
    env = dict(os.environ, VYRA_DESKTOP="1")
    if delay_s:
        env["VYRA_START_DELAY"] = str(delay_s)
    flags = 0
    if sys.platform == "win32":
        flags = 0x00000008 | 0x00000200 | 0x08000000  # DETACHED | NEW_PROCESS_GROUP | NO_WINDOW
    subprocess.Popen([pythonw(), "-m", "coach", *args], cwd=str(ROOT), env=env, creationflags=flags,
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def run_desktop(cfg, local: bool = False) -> None:
    """Yvara AI als App: Hintergrund-Prozess + eigenes Fenster. Fenster schließen beendet Yvara AI."""
    from . import __version__

    port = int(cfg.get_path("web.port", 8765))
    url = f"http://127.0.0.1:{port}/?v={__version__}"
    started_here = False
    if not server_up(port):
        spawn_background(["local" if local else "run"])
        started_here = True
    try:
        import webview
    except Exception as e:  # pywebview fehlt -> Browser im App-Modus
        log.warning("App-Fenster (pywebview) nicht verfügbar (%s) – nutze Browser-Fenster", e)
        import time

        for _ in range(120):
            if server_up(port):
                break
            time.sleep(1)
        open_app(url, True, str(cfg.get_path("web.app_browser", "chrome")))
        return
    WEBVIEW_DATA.mkdir(parents=True, exist_ok=True)
    try:
        import base64

        logo = "data:image/png;base64," + base64.b64encode((LOGO_ICO.parent / "logo-256.png").read_bytes()).decode()
    except OSError:
        logo = ""
    try:
        import ctypes

        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("Yvara.Vyra.Coach")  # eigenes Taskleisten-Symbol
    except Exception:
        pass
    win = webview.create_window("Yvara AI", html=SPLASH.replace("__URL__", url).replace("__LOGO__", logo), width=1440, height=920,
                                min_size=(900, 600), background_color="#0b0d12", text_select=True)

    def on_closed() -> None:
        if started_here or cfg.get_path("desktop.quit_on_close", True):
            try:
                import urllib.request

                urllib.request.urlopen(urllib.request.Request(f"http://127.0.0.1:{port}/api/quit", data=b"{}",
                                                              method="POST"), timeout=2)
            except Exception:
                pass

    win.events.closed += on_closed
    win.events.shown += lambda: set_window_icon("Yvara AI", make_icon())
    webview.start(private_mode=False, storage_path=str(WEBVIEW_DATA))


def ensure_app_shortcut() -> None:
    """Einmalig: Desktop-Verknüpfung auf die echte App umstellen (ohne Konsolenfenster)."""
    if sys.platform != "win32":
        return
    try:
        if SHORTCUT_STAMP.exists() and SHORTCUT_STAMP.read_text(encoding="utf-8").strip() == SHORTCUT_VERSION:
            return
        path = create_shortcut()
        SHORTCUT_STAMP.write_text(SHORTCUT_VERSION, encoding="utf-8")
        log.info("Desktop-Verknüpfung startet Yvara AI jetzt als App ohne Konsolenfenster: %s", path)
    except Exception as e:
        log.warning("Desktop-Verknüpfung nicht aktualisiert: %s", e)


LOGO_ICO = Path(__file__).parent / "web" / "static" / "logo.ico"


def make_icon(path: Path = ICON) -> Path:
    """Yvara-AI-Logo (Fadenkreuz mit AI-Abzeichen) als .ico für Verknüpfung und Fenster."""
    import shutil

    path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(LOGO_ICO, path)
    return path


def set_window_icon(title: str, icon: Path = ICON) -> None:
    """Windows: eigenes Logo statt Python-Symbol in Titelleiste und Taskleiste."""
    if sys.platform != "win32":
        return
    try:
        import ctypes

        u32 = ctypes.windll.user32
        hwnd = u32.FindWindowW(None, title)
        if not hwnd:
            return
        for kind, size in ((1, 32), (0, 16)):  # ICON_BIG, ICON_SMALL
            h = u32.LoadImageW(None, str(icon), 1, size, size, 0x10)  # IMAGE_ICON, LR_LOADFROMFILE
            if h:
                u32.SendMessageW(hwnd, 0x80, kind, h)  # WM_SETICON
    except Exception as e:
        log.debug("Fenster-Icon nicht gesetzt: %s", e)


def create_shortcut(local: bool = False, console: bool = False) -> str:
    """Desktop-Verknüpfung „Yvara AI“ anlegen (Standard: App ohne Konsolenfenster)."""
    if sys.platform != "win32":
        return "Verknüpfungen werden automatisch nur unter Windows angelegt."
    icon = make_icon()
    name = "Yvara AI (ohne Discord)" if local else "Yvara AI"
    if console:
        target, args = ROOT / "start_mit_konsole.bat", ""
    else:
        target, args = pythonw(), "-m coach desktop" + (" --local" if local else "")
    ps = (  # Desktop + Startmenü (wie bei Discord/Spotify: Windows-Taste, „Yvara AI“ tippen)
        "foreach($f in @('Desktop','Programs')){$d=[Environment]::GetFolderPath($f);"
        # alte, von uns angelegte Verknüpfungen mit dem früheren Namen entfernen (nur genau diese zwei Dateien)
        "foreach($o in @('Vyra Coach.lnk','Vyra Coach (ohne Discord).lnk')){$p=Join-Path $d $o;if(Test-Path $p){Remove-Item $p}};"
        f"$s=(New-Object -ComObject WScript.Shell).CreateShortcut((Join-Path $d '{name}.lnk'));"
        f"$s.TargetPath='{target}';$s.Arguments='{args}';$s.WorkingDirectory='{ROOT}';$s.IconLocation='{icon}';"
        "$s.WindowStyle=1;$s.Description='Yvara AI – Coach und Caster für Call of Duty';$s.Save()};"
        f"Write-Output (Join-Path ([Environment]::GetFolderPath('Desktop')) '{name}.lnk')"
    )
    out = subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", ps],
                         capture_output=True, text=True)
    if out.returncode != 0:
        raise RuntimeError(out.stderr.strip() or "PowerShell-Fehler")
    return out.stdout.strip()
