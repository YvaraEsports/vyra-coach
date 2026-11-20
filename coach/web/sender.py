"""Schlanke Oberfläche für den Gaming-PC im Sync-Modus „sendet“: keine KI, kein Discord, keine Stimme –
nur Bild/Ton an den Laptop schicken. Spart Grafikspeicher und CPU für CoD."""
from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path

from aiohttp import web

from ..config import save_user_value

log = logging.getLogger(__name__)
STATIC = Path(__file__).parent / "static"
LOCAL = ("127.0.0.1", "::1")


class SenderUI:
    def __init__(self, cfg, sender) -> None:
        self.cfg, self.sender = cfg, sender
        self.app = web.Application()
        r = self.app.router
        r.add_get("/", self.index)
        r.add_get("/api/sync", self.get)
        r.add_post("/api/sync", self.set)
        r.add_post("/api/quit", self.quit)
        r.add_static("/static/", STATIC)

    async def start(self, port: int) -> None:
        runner = web.AppRunner(self.app)
        await runner.setup()
        await web.TCPSite(runner, "127.0.0.1", port).start()
        log.info("Sync-Sender-Oberfläche: http://127.0.0.1:%s", port)

    async def index(self, _req):
        return web.FileResponse(STATIC / "sync.html", headers={"Cache-Control": "no-store"})

    async def get(self, _req):
        from .. import __version__

        return web.json_response({**self.sender.status(), "code": self.cfg.get_path("sync.code", "") or "",
                                  "monitor": self.sender.monitor, "version": __version__})

    async def set(self, req):
        if req.remote not in LOCAL:
            raise web.HTTPForbidden()
        d = await req.json()
        keys = {"host": str, "code": str, "video": bool, "audio": bool, "mic": bool, "monitor": int, "role": str}
        for k, typ in keys.items():
            if k in d:
                v = typ(d[k])
                if k == "role" and v not in ("off", "send", "receive"):
                    continue
                save_user_value(self.cfg, f"sync.{k}", v)
        if d.get("role") and d["role"] != "send":
            from .. import updater

            self.sender.stop()
            asyncio.get_running_loop().call_later(1.0, updater.restart_now)
            return web.json_response({"ok": True, "restart": True})
        from ..sync import sender_from_cfg

        self.sender.stop()
        self.sender = sender_from_cfg(self.cfg)
        self.sender.start()
        return web.json_response({"ok": True, **self.sender.status()})

    async def quit(self, req):
        if req.remote not in LOCAL:
            raise web.HTTPForbidden()
        self.sender.stop()
        asyncio.get_running_loop().call_later(0.3, os._exit, 0)
        return web.json_response({"ok": True})


async def run_sender(cfg) -> None:
    """App-Start im Sender-Modus (Gaming-PC)."""
    from ..sync import sender_from_cfg

    sender = sender_from_cfg(cfg)
    if sender.host and sender.code:
        sender.start()
    else:
        log.info("Sync: Laptop-Adresse und Kopplungs-Code fehlen noch – in der App eintragen.")
    port = int(cfg.get_path("web.port", 8765))
    await SenderUI(cfg, sender).start(port)
    if cfg.get_path("web.open_browser", True) and not os.environ.get("VYRA_DESKTOP"):
        from ..desktop import open_app

        open_app(f"http://127.0.0.1:{port}/", bool(cfg.get_path("web.app_window", True)),
                 str(cfg.get_path("web.app_browser", "chrome")))
    log.info("Yvara AI sendet Bild/Ton an den Laptop (Sync). Die KI läuft dort, nicht auf diesem PC.")
    await asyncio.Event().wait()
