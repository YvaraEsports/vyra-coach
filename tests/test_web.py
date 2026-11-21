import asyncio
from pathlib import Path

from aiohttp.test_utils import TestClient, TestServer

from coach.web.server import WebUI, _strip_md
from tests.test_core import _coach


class FakeTTS:
    async def synth(self, text, lang):
        p = Path("data/tts_cache/0123456789abcdef.wav")
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"RIFF")
        return p


def test_web_api_flow(tmp_path, monkeypatch):
    coach = _coach(tmp_path, monkeypatch)
    monkeypatch.setattr("coach.web.server.save_user_value", lambda cfg, k, v: cfg.set_path(k, v))
    ui = WebUI(coach.cfg, coach, FakeTTS())

    async def go():
        async with TestClient(TestServer(ui.app)) as c:
            r = await c.get("/")
            assert r.status == 200 and "Yvara AI" in await r.text()
            st = await (await c.get("/api/state")).json()
            assert st["name"] == "Vyra" and st["team"]["name"] == "Yvara Main"
            assert st["coaching_mode"] == "talk" and st["llm"]["provider"] == "ollama"

            assert (await (await c.post("/api/mode", json={"mode": "observe", "announce": False})).json())["mode"] == "observe"
            assert (await c.post("/api/mode", json={"mode": "quatsch"})).status == 400
            les = await (await c.post("/api/lesson", json={"topic": "B-Retake"})).json()
            assert les["ok"] and (await (await c.get("/api/state")).json())["lesson"]["idx"] == 0
            await c.post("/api/lesson", json={"action": "next"})
            assert (await (await c.get("/api/state")).json())["lesson"]["idx"] == 1
            await c.post("/api/lesson", json={"action": "stop"})

            from coach.study.library import Library
            coach.library = Library(tmp_path / "lib.db")
            assert (await c.post("/api/callouts", json={"map": "Munition", "name": "Top Bridge", "zone": "C2",
                                                       "aliases": "Brücke oben, TB"})).status == 200
            co = await (await c.get("/api/callouts?map=Munition")).json()
            assert co["callouts"][0]["aliases"] == ["Brücke oben", "TB"] and "munition" in co["maps"]
            await c.delete(f"/api/callouts/{co['callouts'][0]['id']}")
            assert (await (await c.get("/api/callouts?map=Munition")).json())["callouts"] == []


            res = await (await c.post("/api/chat", json={"text": "Wie spielen wir B?", "speak": True})).json()
            assert res["reply"] and res["audio"] == "/audio/0123456789abcdef.wav"
            sessions = await (await c.get("/api/sessions")).json()
            assert sessions[0]["title"] == "Wie spielen wir B?"
            msgs = await (await c.get(f"/api/sessions/{res['session_id']}")).json()
            assert len(msgs) == 2

            mems = await (await c.get("/api/memories")).json()
            assert mems and mems[0]["text"].startswith("Player1")

            t = await (await c.post("/api/teams", json={"name": "Yvara Z8", "players": [{"ingame": "Z8One", "role": "SMG"}]})).json()
            assert t["slug"] == "yvara-z8"
            assert (await (await c.post("/api/teams/activate", json={"slug": "yvara-z8"})).json())["ok"]
            st = await (await c.get("/api/state")).json()
            assert st["team"]["name"] == "Yvara Z8" and st["team"]["players"][0]["ingame"] == "Z8One"
            assert await (await c.get("/api/memories")).json() == []  # eigenes Gedächtnis

            await c.post("/api/settings", json={"coach.name": "Kairo", "web.research": True})
            assert (await (await c.get("/api/state")).json())["name"] == "Kairo"
            assert not coach.cfg.get_path("web.research")  # Websuche gesperrt ohne privacy.allow_web_search

            await c.post("/api/match/start", json={"mode": "snd", "map": "MapA", "opponent": "TeamX"})
            st = await (await c.get("/api/state")).json()
            assert st["match"]["active"] and st["match"]["mode"] == "snd"
            r = await (await c.post("/api/teams/activate", json={"slug": "yvara-main"})).json()
            assert r["ok"] is False  # kein Wechsel während Match

            await c.post("/api/focus", json={"player": "Z8One"})
            assert (await (await c.get("/api/state")).json())["focus"] == "Z8One"
            assert (await c.get("/audio/../../etc/passwd")).status == 404

            await c.post("/api/settings", json={"llm.provider": "ollama", "llm.local_model": "gemma3:12b"})
            assert coach.llm.model == "gemma3:12b"

    asyncio.run(go())


def test_strip_md():
    assert "#" not in _strip_md("## Plan\n- **Rotiert** früh")


def test_team_create_rename_and_errors(tmp_path, monkeypatch):
    coach = _coach(tmp_path, monkeypatch)
    ui = WebUI(coach.cfg, coach, FakeTTS())

    async def go():
        async with TestClient(TestServer(ui.app)) as c:
            r = await c.post("/api/teams", json={"name": "Yvara Z8", "create": True})
            z8 = await r.json()
            assert r.status == 200 and z8["ok"] and z8["slug"] == "yvara-z8"
            r = await c.post("/api/teams", json={"name": "yvara z8", "create": True})
            assert r.status == 409 and "schon" in (await r.json())["error"]
            r = await c.post("/api/teams", json={"name": "  "})
            assert r.status == 400
            a = await (await c.post("/api/teams/activate", json={"slug": "yvara-z8"})).json()
            assert a["ok"] and a["team"]["name"] == "Yvara Z8" and coach.team.slug == "yvara-z8"
            assert (await c.post("/api/teams/activate", json={"slug": "gibtsnicht"})).status == 404
            # Umbenennen legt KEIN neues Team an
            r = await c.post("/api/teams", json={"slug": "yvara-z8", "name": "Yvara Academy", "players": [{"ingame": "A", "role": "AR"}]})
            assert r.status == 200
            names = [t["name"] for t in coach.teams.list()]
            assert "Yvara Academy" in names and "Yvara Z8" not in names and len(names) == 2
            r = await c.post("/api/teams", json={"slug": "yvara-z8", "name": "Yvara Main"})
            assert r.status == 409

    asyncio.run(go())


def test_desktop_icon_and_command(tmp_path, monkeypatch):
    from coach import desktop

    ico = desktop.make_icon(tmp_path / "v.ico")
    assert ico.exists() and ico.stat().st_size > 1000
    monkeypatch.setattr(desktop.sys, "platform", "linux")
    monkeypatch.setattr(desktop, "_linux_browsers", lambda: ["/usr/bin/chromium"])
    cmd = desktop.app_window_command("http://127.0.0.1:8765")
    assert cmd[0] == "/usr/bin/chromium" and "--app=http://127.0.0.1:8765" in cmd


def test_brain_status_blocks_autolearn(tmp_path, monkeypatch):
    coach = _coach(tmp_path, monkeypatch)

    async def bad():
        return False, "Ollama läuft nicht"
    coach.llm.check = bad
    coach.autolearn = type("AL", (), {"start": lambda *a, **k: None, "status": {}})()
    ui = WebUI(coach.cfg, coach, FakeTTS())

    async def go():
        async with TestClient(TestServer(ui.app)) as c:
            st = await (await c.get("/api/state")).json()
            assert st["brain"] == {"ok": False, "message": "Ollama läuft nicht"}
            r = await c.post("/api/autolearn", json={"topics": "x"})
            assert r.status == 409 and (await r.json())["error"] == "Ollama läuft nicht"

    asyncio.run(go())


def test_local_llm_check(tmp_path):
    from aiohttp import web
    from aiohttp.test_utils import TestServer

    from coach.brain.local_llm import LocalLLM
    from tests.test_core import _cfg

    models = {"data": []}

    async def lst(_req):
        return web.json_response(models)

    async def go():
        app = web.Application()
        app.router.add_get("/v1/models", lst)
        async with TestServer(app) as srv:
            llm = LocalLLM(_cfg(llm={"provider": "ollama", "base_url": str(srv.make_url("/v1")), "local_model": "qwen3-vl:8b"}))
            ok, msg = await llm.check()
            assert not ok and "ollama pull qwen3-vl:8b" in msg
            models["data"] = [{"id": "qwen3-vl:8b"}]
            assert (await llm.check())[0]
        llm.base_url = "http://127.0.0.1:9/v1"
        ok, msg = await llm.check()
        assert not ok and "läuft nicht" in msg

    asyncio.run(go())


def test_desktop_prefers_chrome(tmp_path, monkeypatch):
    from coach import desktop

    monkeypatch.setattr(desktop, "WIN_PATHS", {k: v.replace("\\", "/") for k, v in desktop.WIN_PATHS.items()})
    pf = tmp_path / "pf"
    for k in ("chrome", "edge"):
        p = pf / desktop.WIN_PATHS[k]
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("x")
    for k in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA"):
        monkeypatch.setenv(k, str(pf) if k == "PROGRAMFILES" else "")
    assert "chrome" in desktop._windows_browsers()[0].lower()
    assert "msedge" in desktop._windows_browsers("edge")[0].lower()


def test_logs_endpoint_hides_access_lines(tmp_path, monkeypatch):
    coach = _coach(tmp_path, monkeypatch)
    monkeypatch.setattr("coach.config.DATA_DIR", tmp_path)
    (tmp_path / "logs").mkdir()
    (tmp_path / "logs" / "vyra.log").write_text("a INFO aiohttp.access GET /api/state\nb INFO coach: Vyra bereit\n", encoding="utf-8")
    ui = WebUI(coach.cfg, coach, FakeTTS())

    async def go():
        async with TestClient(TestServer(ui.app)) as c:
            r = await (await c.get("/api/logs")).json()
            assert r["lines"] == ["b INFO coach: Vyra bereit"]
            st = await (await c.get("/api/state")).json()
            assert st["discord"] == {"enabled": False, "ready": False, "voice": ""}
    asyncio.run(go())


def test_app_call_turn_returns_spoken_reply(tmp_path, monkeypatch):
    import io
    import wave as _wave

    import numpy as np

    from coach.audio.stt import Transcript
    coach = _coach(tmp_path, monkeypatch)

    class FakeSTT:
        async def transcribe(self, audio, forced=None):
            assert len(audio) == 16000
            return Transcript("Wie geht es dir heute", "de", 0.9)
    coach.stt = FakeSTT()

    async def fake_cmd(text, lang):
        coach.say("Mir geht's gut, danke!", lang=lang)
    coach.on_command = fake_cmd
    ui = WebUI(coach.cfg, coach, FakeTTS())
    buf = io.BytesIO()
    with _wave.open(buf, "wb") as wf:
        wf.setnchannels(1); wf.setsampwidth(2); wf.setframerate(16000)
        wf.writeframes((np.zeros(16000) * 0).astype("<i2").tobytes())

    async def go():
        async with TestClient(TestServer(ui.app)) as c:
            from aiohttp import FormData
            fd = FormData(); fd.add_field("audio", buf.getvalue(), filename="c.wav", content_type="audio/wav")
            r = await (await c.post("/api/call", data=fd)).json()
            assert r["text"] == "Wie geht es dir heute" and r["replies"][0]["text"] == "Mir geht's gut, danke!"
            assert coach._say_capture is None
    asyncio.run(go())


def test_parakeet_language_guess():
    from coach.audio.stt_parakeet import guess_language
    assert guess_language("Wie geht es dir heute, Alter?") == "de"
    assert guess_language("How are you doing today bro?") == "en"
    assert guess_language("Hardpoint P2 rotate") in ("de", "en")
    assert guess_language("Schön") == "de"


def test_api_errors_come_back_as_json_reason(tmp_path, monkeypatch):
    """Die App zeigt bei jeder fehlgeschlagenen Aktion den Grund – dafür muss die API ihn als JSON liefern."""
    coach = _coach(tmp_path, monkeypatch)
    ui = WebUI(coach.cfg, coach, FakeTTS())

    async def boom(request):
        raise RuntimeError("Datenbank gesperrt")
    ui.app.router.add_get("/api/_boom", boom)

    async def go():
        async with TestClient(TestServer(ui.app)) as c:
            r = await c.post("/api/mode", json={"mode": "quatsch"})
            assert r.status == 400
            assert (await r.json()) == {"ok": False, "error": "mode: talk | live | observe | teach | cast"}
            r = await c.get("/api/_boom")
            assert r.status == 500
            assert (await r.json())["error"] == "RuntimeError: Datenbank gesperrt"
            r = await c.get("/")  # normale Seiten bleiben unverändert
            assert r.status == 200 and "Yvara AI" in await r.text()
    asyncio.run(go())
