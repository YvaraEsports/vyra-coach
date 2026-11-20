import asyncio
import json
import socket

import cv2
import numpy as np
from aiohttp.test_utils import TestClient, TestServer

from coach import sync


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def test_receiver_gets_picture_and_sound_only_with_code(monkeypatch):
    port = _free_port()
    rcv = sync.SyncReceiver(port, code="123456")

    async def go():
        await rcv.start()
        # falscher Code -> abgelehnt
        r, w = await asyncio.open_connection("127.0.0.1", port)
        w.write(json.dumps({"hello": "yvara-sync", "code": "000000"}).encode() + b"\n")
        await w.drain()
        assert b'"ok":false' in await r.readline()
        w.close()
        # richtiger Code -> Bild + Ton kommen an
        r, w = await asyncio.open_connection("127.0.0.1", port)
        w.write(json.dumps({"hello": "yvara-sync", "code": "123456", "video": True, "audio": True}).encode() + b"\n")
        await w.drain()
        assert b'"ok":true' in await r.readline()
        img = np.full((90, 160, 3), 200, np.uint8)
        ok, buf = cv2.imencode(".jpg", img)
        w.write(sync.pack(b"V", buf.tobytes()))
        tone = (np.ones(960) * 0.5 * 32767).astype("<i2").tobytes()
        w.write(sync.pack(b"A", tone))
        await w.drain()
        await asyncio.sleep(0.2)
        assert rcv.connected and rcv.status()["picture"]
        assert rcv.latest().shape == (90, 160, 3)
        block = rcv.read_block("A", timeout=0.5)
        assert len(block) == sync.BLOCK and abs(float(block[0]) - 0.5) < 0.01
        assert not rcv.read_block("M", timeout=0.05).any()  # nichts gekommen -> Stille
        w.close()
        await asyncio.sleep(0.1)
        rcv.close()

    asyncio.run(go())


def test_sender_connects_and_rejects_internet_hosts():
    port = _free_port()
    rcv = sync.SyncReceiver(port, code="654321")

    async def go():
        await rcv.start()
        snd = sync.SyncSender("127.0.0.1", "654321", port, video=False, audio=False)
        assert await asyncio.to_thread(snd._connect)
        await asyncio.sleep(0.1)
        assert rcv.connected
        assert snd._send(b"V", cv2.imencode(".jpg", np.zeros((10, 10, 3), np.uint8))[1].tobytes())
        snd.stop()
        bad = sync.SyncSender("127.0.0.1", "111111", port, video=False, audio=False)
        assert not await asyncio.to_thread(bad._connect) and "Code" in bad.error
        rcv.close()

    asyncio.run(go())
    far = sync.SyncSender("8.8.8.8", "123456")
    assert not far._connect() and "Heimnetz" in far.error


def test_sync_feed_and_sender_ui(tmp_path, monkeypatch):
    from coach.config import Config
    from coach.vision.feeds import FeedSpec, make_feed
    from coach.web.sender import SenderUI

    monkeypatch.setattr(sync, "RECEIVER", None)
    f = make_feed(FeedSpec("PC", "sync", ""))
    assert f.latest() is None and "aus" in f.error
    cfg = Config({"sync": {"host": "", "code": ""}})
    monkeypatch.setattr("coach.web.sender.save_user_value", lambda c, k, v: c.set_path(k, v))
    ui = SenderUI(cfg, sync.sender_from_cfg(cfg))

    async def go():
        async with TestClient(TestServer(ui.app)) as c:
            assert (await c.get("/")).status == 200
            st = await (await c.get("/api/sync")).json()
            assert st["role"] == "send" and not st["connected"]
            r = await (await c.post("/api/sync", json={"host": "127.0.0.1", "code": "123456", "mic": True})).json()
            assert r["ok"] and r["mic"] and cfg.get_path("sync.code") == "123456"
            ui.sender.stop()

    asyncio.run(go())


def test_main_app_sync_settings(tmp_path, monkeypatch):
    from coach.web.server import WebUI
    from tests.test_core import _coach
    from tests.test_web import FakeTTS

    coach = _coach(tmp_path, monkeypatch)
    monkeypatch.setattr("coach.web.server.save_user_value", lambda cfg, k, v: cfg.set_path(k, v))
    monkeypatch.setattr(sync, "CODE_FILE", tmp_path / "code.txt")
    ui = WebUI(coach.cfg, coach, FakeTTS())

    async def go():
        async with TestClient(TestServer(ui.app)) as c:
            assert (await (await c.get("/api/sync")).json())["role"] == "off"
            assert (await c.post("/api/sync", json={"code": "12ab"})).status == 400
            r = await (await c.post("/api/sync", json={"role": "send", "host": "192.168.1.5", "code": "123456"})).json()
            assert r["ok"] and coach.cfg.get_path("sync.role") == "send"
            await c.post("/api/sync", json={"role": "receive"})
            st = await (await c.get("/api/sync")).json()
            assert len(st["code"]) == 6 and st["receiver"] is None

    asyncio.run(go())
