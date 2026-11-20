import asyncio
import json
import time

import numpy as np
import pytest

from coach.audio.output import SpeechItem, SpeechQueue
from coach.audio.segmenter import Segmenter
from coach.brain.coach import Coach, detect_intent, is_addressed
from coach.brain.knowledge import Knowledge, norm_mode
from coach.config import Config, deep_merge
from coach.stats.db import DB
from coach.teams import TeamManager, slugify
from coach.vision.hud import HudReading, parse_int, parse_timer
from coach.vision.state import StateTracker


# ---------------------------------------------------------------- audio
def test_segmenter_detects_speech_segment():
    seg = Segmenter(threshold=0.01, hangover_s=0.3, min_speech_s=0.2)
    sr, block = 16000, 480
    silence = np.zeros(block, np.float32)
    tone = (0.2 * np.sin(np.linspace(0, 100, block))).astype(np.float32)
    out = []
    for _ in range(20):
        out += seg.feed(silence)
    for _ in range(30):  # ~0.9 s Sprache
        out += seg.feed(tone)
    for _ in range(20):
        out += seg.feed(silence)
    assert len(out) == 1
    assert len(out[0]) / sr > 0.8


def test_segmenter_ignores_short_click():
    seg = Segmenter(threshold=0.01, hangover_s=0.3, min_speech_s=0.3)
    click = np.full(480, 0.5, np.float32)
    out = seg.feed(click)
    for _ in range(20):
        out += seg.feed(np.zeros(480, np.float32))
    assert out == []


def test_speech_queue_priority_and_staleness():
    q = SpeechQueue()
    q.put(SpeechItem(priority=1, text="info"))
    q.put(SpeechItem(priority=3, text="rotate"))
    old = SpeechItem(priority=3, text="old", max_age=1)
    old.created = time.time() - 10
    q.put(old)
    assert q.pop_fresh().text == "rotate"
    assert q.pop_fresh().text == "info"
    assert q.pop_fresh() is None


# ----------------------------------------------------------------- hud
@pytest.mark.parametrize("txt,val", [("1:23", 83), ("0.45", 45), ("O:3O", 30), ("45", 45), ("", None), ("abc", None)])
def test_parse_timer(txt, val):
    assert parse_timer(txt) == val


@pytest.mark.parametrize("txt,val", [("127", 127), ("I2O", 120), ("score 5", 5), (None, None)])
def test_parse_int(txt, val):
    assert parse_int(txt) == val


def _feed(tr, **kw):
    ev = tr.update(HudReading(**kw))
    ev += tr.update(HudReading(**kw))  # 2x für Stabilisierung
    return ev


def test_state_snd_round_and_match_point():
    tr = StateTracker(Knowledge().modes)
    tr.start_match("snd", "Map", "Gegner")
    assert _feed(tr, score_ours=0, score_enemy=0) == []
    ev = _feed(tr, score_ours=1, score_enemy=0)
    assert [e.kind for e in ev] == ["round_end"] and ev[0].data["won"] is True
    for i in range(2, 6):
        ev = _feed(tr, score_ours=i, score_enemy=0)
    assert "match_point" in [e.kind for e in ev]
    ev = _feed(tr, score_ours=6, score_enemy=0)
    assert {"round_end", "match_end"} <= {e.kind for e in ev}


def test_state_rejects_ocr_garbage():
    tr = StateTracker(Knowledge().modes)
    tr.start_match("snd")
    _feed(tr, score_ours=4, score_enemy=0)  # Sprung um 4 -> unplausibel
    assert tr.state.score_ours == 0


def test_state_hill_rotate_once_per_hill():
    tr = StateTracker(Knowledge().modes)
    tr.start_match("hardpoint")
    kinds = []
    for t in (40, 30, 20, 15, 14, 13, 60, 50, 15):
        kinds += [e.kind for e in _feed(tr, score_ours=10, score_enemy=10, objective_timer=t)]
    assert kinds.count("hill_rotate") == 2


def test_state_time_warning():
    tr = StateTracker(Knowledge().modes)
    tr.start_match("snd")
    kinds = []
    for t in (90, 60, 31, 29, 20, 14):
        kinds += [e.kind for e in _feed(tr, score_ours=0, score_enemy=0, game_timer=t)]
    assert kinds.count("time_low") == 2


# --------------------------------------------------------------- brain
@pytest.mark.parametrize("text,ok,rest", [
    ("Coach, wann rotieren wir?", True, "wann rotieren wir"),
    ("hey Vyra was machen die", True, "was machen die"),
    ("ok vyra review", True, "review"),
    ("die spielen alle B", False, None),
])
def test_wake_word(text, ok, rest):
    got, r = is_addressed(text, ["coach", "vyra", "hey vyra"])
    assert got is ok
    if ok:
        assert r == rest


@pytest.mark.parametrize("text,intent", [
    ("sei ruhig", "mute"), ("calls an", "unmute"), ("wie war die runde", "review"),
    ("mach mir einen plan", "plan"), ("schau auf Player2", "focus"), ("wechsel zu Yvara Z8", "team"),
    ("warum verlieren wir immer den zweiten hill wenn wir rotieren und die gegner schneller sind", None),
])
def test_intents(text, intent):
    assert detect_intent(text) == intent


def test_norm_mode():
    assert norm_mode("S&D") == "snd" and norm_mode("HP") == "hardpoint" and norm_mode("Kontrolle") == "control"


def test_knowledge_context():
    ctx = Knowledge().context("hardpoint", "example_map")
    assert "Hardpoint" in ctx and "principle" in ctx and "Gelbes Haus" in ctx


# ------------------------------------------------------------------ db
def test_db_memory_chat_stats(tmp_path):
    db = DB(tmp_path / "c.db")
    mid = db.start_match("snd", "MapA", "TeamX")
    db.end_match(mid, 6, 3)
    st = db.stats()
    assert st["total"]["wins"] == 1 and st["by_opponent"][0]["key"] == "TeamX"
    a = db.add_memory("Player2 ankert B")
    assert db.add_memory("player2 ankert b") == a  # keine Duplikate
    db.add_memory("Trainingstag ist Mittwoch")
    assert db.relevant_memories("wer ankert B?")[0] == "Player2 ankert B"
    sid = db.new_session("t")
    db.add_chat(sid, "user", "hi")
    db.add_chat(sid, "assistant", "hey")
    assert [m["role"] for m in db.chat(sid)] == ["user", "assistant"]
    db.set_profile("player", "P1", {"summary": "x"})
    assert db.get_profile("player", "p1") == {"summary": "x"}


def test_config_merge():
    c = Config(deep_merge({"a": {"b": 1, "c": 2}}, {"a": {"b": 5}}))
    assert c.get_path("a.b") == 5 and c.get_path("a.c") == 2 and c.get_path("x.y", 7) == 7


# --------------------------------------------------------------- teams
def _cfg(**extra):
    base = {"coach": {"name": "Vyra", "personality": "direkt"}, "language": "auto", "default_language": "de",
            "team": {"name": "Yvara Main", "players": [{"ingame": "Player1", "role": "AR"}]},
            "audio": {"wake_words": ["coach"]}, "callouts": {"enabled": True, "min_gap_s": 0, "rule_calls": True},
            "llm": {"max_answer_words": 30}, "web": {"research": False}}
    return Config(deep_merge(base, extra))


def test_teams_are_separate(tmp_path, monkeypatch):
    monkeypatch.setattr("coach.teams.ACTIVE_FILE", tmp_path / "active.txt")
    tm = TeamManager(_cfg(), root=tmp_path / "teams")
    assert tm.active.name == "Yvara Main"
    tm.active.db.add_memory("Main-Fakt")
    z8 = tm.create("Yvara Z8")
    assert slugify("Yvara Z8") == "yvara-z8"
    tm.activate("Yvara Z8")
    assert tm.active is z8 and tm.active.db.memories() == []
    tm.activate("yvara-main")
    assert tm.active.db.memories()[0]["text"] == "Main-Fakt"


class FakeVoice:
    def __init__(self):
        self.said = []
        self.enabled = True
        self.spoken_log = []

    def say(self, text, lang="de", priority=1, max_age=8.0):
        self.said.append((text, lang, priority))


class FakeLLM:
    def __init__(self):
        self.calls = []

    async def ask(self, system, content, **kw):
        self.calls.append(("ask", system, content, kw))
        props = (kw.get("schema") or {}).get("properties", {})
        if "lessons" in props:
            return {"is_gameplay": True, "mode_detected": "hardpoint", "map_detected": "MapA", "summary": "s",
                    "lessons": [{"mode": "hardpoint", "map": "MapA", "team": "ProTeam", "situation": "Hill rotiert",
                                 "action": "2 vorrotieren", "why": "Spawns", "lesson": "Zwei Spieler 15 s früher zum nächsten Hill",
                                 "tags": ["rotation"], "confidence": 0.8}]}
        if "principles" in props:
            return {"summary": "HP-Playbook", "principles": ["Früh rotieren"], "setups": [], "rotations": [],
                    "mistakes_to_avoid": [], "meta_notes": [], "drills": []}
        if "report_markdown" in props:
            return {"spoken": "GG", "report_markdown": "# Review", "team_good": ["a"], "team_bad": ["b"],
                    "players": [{"player": "Player1", "good": [], "bad": [], "focus": "traden", "rating": 7.5}],
                    "mvp": "Player1", "enemy_tendencies": ["rush"], "training_focus": ["trades"]}
        if "steps" in props:
            return {"title": "B-Retake", "intro": "Okay Jungs, B-Retake.",
                    "steps": [{"say": "Alle zu B-Treppen.", "check": "4 Spieler an B"}, {"say": "Zwei peeken Plant.", "check": "Trade"}],
                    "outro": "Sauber."}
        if "matches_seen" in props:
            return {"summary": "x", "strengths": [], "weaknesses": [], "tendencies": [], "focus": [], "progress": [], "matches_seen": 0}
        if kw.get("schema"):
            return {"spoken": "Gute Runde, weiter so.", "good": ["Trades"], "bad": [], "next_round": "gleich", "enemy_read": "rush B"}
        return "Rotiert früher."

    async def chat(self, system, messages, tool_handler=None, **kw):
        self.calls.append(("chat", system, messages, kw))
        await tool_handler("save_memory", {"fact": "Player1 spielt gern aggressiv"})
        return "Notiert. Player1 sollte mehr traden."


def _coach(tmp_path, monkeypatch, **cfg):
    monkeypatch.setattr("coach.teams.ACTIVE_FILE", tmp_path / "active.txt")
    c = _cfg(**cfg)
    return Coach(c, FakeLLM(), TeamManager(c, root=tmp_path / "teams"), Knowledge(), FakeVoice())


def test_coach_chat_saves_memory_and_history(tmp_path, monkeypatch):
    coach = _coach(tmp_path, monkeypatch)
    sid = coach.db.new_session()
    reply = asyncio.run(coach.chat(sid, "Was soll Player1 besser machen?"))
    assert "traden" in reply
    assert coach.db.memories()[0]["text"] == "Player1 spielt gern aggressiv"
    assert [m["role"] for m in coach.db.chat(sid)] == ["user", "assistant"]
    system = coach.llm.calls[0][1]
    assert "Vyra" in system and "Yvara Main" in system and "direkt" in system
    assert coach.last_lang == "de"


def test_coach_events_and_round_review(tmp_path, monkeypatch):
    coach = _coach(tmp_path, monkeypatch)
    coach.set_mode("live", announce=False)

    async def go():
        await coach.start_match("hardpoint", "MapA", "TeamX")
        from coach.vision.state import GameEvent
        await coach.handle_events([GameEvent("hill_rotate", {"seconds": 15})])
        await coach.handle_events([GameEvent("round_end", {"won": True, "round": 1, "ours": 1, "enemy": 0})])
        await asyncio.sleep(0.05)

    asyncio.run(go())
    texts = [s[0] for s in coach.voice.said]
    assert any("Hill dreht" in t for t in texts)
    assert any("Gute Runde" in t for t in texts)
    assert coach.db.rounds(coach.match_id)[0]["review"]


def test_coach_focus_and_team_switch_by_voice(tmp_path, monkeypatch):
    coach = _coach(tmp_path, monkeypatch)
    coach.teams.create("Yvara Z8")
    asyncio.run(coach.on_command("schau auf player1", "de"))
    assert coach.focus_player == "Player1"
    asyncio.run(coach.on_command("wechsel zu yvara z8", "de"))
    assert coach.team.name == "Yvara Z8" and coach.focus_player is None


def test_coach_answer_uses_language(tmp_path, monkeypatch):
    coach = _coach(tmp_path, monkeypatch)
    asyncio.run(coach.answer("when do we rotate", "en"))
    assert coach.voice.said[-1] == ("Rotiert früher.", "en", 2)
    assert "ANSWER IN: en" in json.dumps(coach.llm.calls[-1][2])


def test_match_end_review_ratings_profiles_clips(tmp_path, monkeypatch):
    coach = _coach(tmp_path, monkeypatch)

    async def go():
        await coach.start_match("snd", "MapA", "TeamX")
        coach.save_clip("guter Trade")
        await coach.end_match(6, 2)

    asyncio.run(go())
    assert coach.db.player_ratings()[0]["player"] == "Player1"
    assert coach.db.get_profile("player", "Player1")["matches_seen"] == 1
    assert coach.db.get_profile("opponent", "TeamX")
    assert (coach.team.reports / f"match_1_review.md").exists()
    assert coach.db.clips()[0]["note"] == "guter Trade"
    assert "Ratings" in coach.stats_text()


def test_tilt_timeout_after_three_lost_rounds(tmp_path, monkeypatch):
    coach = _coach(tmp_path, monkeypatch)
    coach.set_mode("live", announce=False)
    from coach.vision.state import GameEvent

    async def go():
        await coach.start_match("snd")
        for i in range(3):
            await coach.handle_events([GameEvent("round_end", {"won": False, "round": i + 1, "ours": 0, "enemy": i + 1})])
        await asyncio.sleep(0.05)

    asyncio.run(go())
    assert any("Timeout" in s[0] for s in coach.voice.said)


def test_library_and_study_screen(tmp_path, monkeypatch):
    from coach.study.library import Library
    from coach.study.study import Studier

    coach = _coach(tmp_path, monkeypatch)
    lib = Library(tmp_path / "lib.db")
    coach.library = lib

    class Cap:
        def grab(self):
            return np.zeros((72, 128, 3), np.uint8)

    cfg = coach.cfg
    cfg.set_path("study.window_s", 0.2)
    cfg.set_path("study.frames_per_window", 2)
    st = Studier(cfg, coach.llm, lib, capture=Cap(), coach=coach)

    async def go():
        st.start("screen", "CDL Test", "hardpoint")
        st.feed_transcript("they rotate early")
        await asyncio.sleep(0.5)
        st.stop()
        await st.task

    asyncio.run(go())
    assert lib.count("hardpoint") >= 1
    assert lib.playbook("hardpoint")["principles"] == ["Früh rotieren"]
    assert "Zwei Spieler" in lib.context("hardpoint", "MapA")
    assert "LEARNED" in coach._library_context("hardpoint", "MapA")
    assert st.status["message"] == "fertig"

    # Eigenes Team: Lektionen ins Team-Gedächtnis, nicht in die Bibliothek
    before = lib.count()

    async def own():
        st.start("screen", "Liga-Stream", own_team=True)
        await asyncio.sleep(0.3)
        st.stop()
        await st.task

    asyncio.run(own())
    assert lib.count() == before
    assert any("Stream-Analyse" in m["text"] for m in coach.db.memories())


def test_study_video_file(tmp_path, monkeypatch):
    import shutil
    import subprocess

    if not shutil.which("ffmpeg"):
        pytest.skip("ffmpeg fehlt")
    from coach.study.library import Library
    from coach.study.study import Studier

    vid = tmp_path / "scrim.mp4"
    subprocess.run(["ffmpeg", "-v", "quiet", "-f", "lavfi", "-i", "testsrc=size=320x180:rate=10", "-t", "3", str(vid)], check=True)
    coach = _coach(tmp_path, monkeypatch)
    lib = Library(tmp_path / "lib.db")
    coach.cfg.set_path("study.window_s", 1)
    coach.cfg.set_path("study.frames_per_window", 2)
    st = Studier(coach.cfg, coach.llm, lib, coach=coach)

    async def go():
        st.start(str(vid), "Scrim", "hardpoint")
        await st.task

    asyncio.run(go())
    assert st.status["progress"] == 1.0 and lib.count() == 3
    images = [b for c in coach.llm.calls if c[0] == "ask" and isinstance(c[2], list) for b in c[2] if b.get("type") == "image"]
    assert images and len(images[0]["source"]["data"]) > 100


def test_minimap_tracking():
    import cv2

    from coach.vision.minimap import MinimapTracker, zone

    cfg = _cfg(hud={"minimap": [0.0, 0.0, 0.5, 0.5]})
    mt = MinimapTracker(cfg)
    t0 = 1000.0
    for i in range(10):
        frame = np.zeros((400, 400, 3), np.uint8)
        mm = frame[:200, :200]
        for k in range(4):  # unser Team bewegt sich nach rechts
            cv2.circle(mm, (20 + i * 12, 30 + k * 30), 4, (255, 120, 0), -1)   # BGR blau
        for k in range(4):  # Gegner bleibt
            cv2.circle(mm, (170, 40 + k * 35), 4, (0, 0, 255), -1)            # BGR rot
        ms = mt.update(frame, now=t0 + i)
    assert sum(m.team == "ours" for m in ms) == 4 and sum(m.team == "enemy" for m in ms) == 4
    summ = mt.summary(30, now=t0 + 10)
    assert "ours: 4 visible" in summ and "moving east" in summ and "enemy: 4 visible" in summ and "holding" in summ
    assert zone(0.0, 0.0) == "A1" and zone(0.99, 0.99) == "E5"
    assert "MINIMAP TIMELINE" in mt.round_summary(t0 - 1)
    import tempfile, os
    p = os.path.join(tempfile.mkdtemp(), "h.png")
    assert mt.heatmap(p) and os.path.getsize(p) > 0


def test_autolearn_loop_discovers_studies_and_invents(tmp_path, monkeypatch):
    from coach.study import autolearn as AL
    from coach.study.library import Library

    coach = _coach(tmp_path, monkeypatch)
    lib = Library(tmp_path / "lib.db")
    coach.library = lib
    notified = []

    async def notify(text, path=None):
        notified.append(path)

    coach.notify = notify

    class FakeStudier:
        active = False

        def __init__(self):
            self.status, self.task, self.seen = {}, None, []

        def start(self, url, title="", note="", max_minutes=None, **kw):
            self.seen.append(url)
            self.status = {"message": "fertig"}
            lib.add_source("url", url, title)
            lib.update_source(lib.sources(1)[0]["id"], status="done")

            async def done():
                return None
            self.task = asyncio.ensure_future(done())

        def stop(self):
            pass

    calls = {"n": 0}

    def fake_search(q, n):
        calls["n"] += 1
        return [{"url": f"https://youtube.com/watch?v={q[:3]}{i}", "title": f"{q} {i}", "duration": 600} for i in range(2)]

    monkeypatch.setattr(AL, "_search_videos", fake_search)
    monkeypatch.setattr(AL, "_twitch_vods", lambda ch, n: [])

    class LLM2(FakeLLM):
        async def ask(self, system, content, **kw):
            props = (kw.get("schema") or {}).get("properties", {})
            if "queries" in props:
                return {"queries": ["CDL hardpoint", "hijack setups"], "reason": "x"}
            if "ideas" in props:
                return {"ideas": [{"name": "Split-Rotate", "mode": "hardpoint", "map": "any", "concept": "2-2 Pre-Rotate",
                                   "setup": "", "roles": [], "timings": "", "when_to_use": "", "counters": "", "why_it_fits_us": "", "risk": ""}],
                        "report_markdown": "# Eigene Taktiken"}
            return await super().ask(system, content, **kw)

    coach.cfg.set_path("autolearn.max_videos", 5)
    coach.cfg.set_path("autolearn.reflect_every", 2)
    st = FakeStudier()
    al = AL.AutoLearner(coach.cfg, LLM2(), lib, st, coach)
    coach.autolearn = al

    async def go():
        sid = coach.db.new_session()
        await coach.chat(sid, "Schau dir das an: https://www.youtube.com/watch?v=PRO123 bitte")
        await asyncio.wait_for(al.task, 10)

    asyncio.run(go())
    assert st.seen[0] == "https://www.youtube.com/watch?v=PRO123"  # geschickter Link zuerst
    assert al.status["videos"] == 5 and len(set(st.seen)) == 5
    assert lib.ideas("Yvara Main")[0]["name"] == "Split-Rotate"
    assert any("Split-Rotate" in m["text"] for m in coach.db.memories())
    assert any(p and "eigene_taktiken" in str(p) for p in notified)
    assert "System: 1 video link" in json.dumps(coach.llm.calls[0][2])


def test_multi_feed_auto_focus(tmp_path, monkeypatch):
    from coach.vision.feeds import FeedManager, composite

    coach = _coach(tmp_path, monkeypatch)

    class F:
        def __init__(self, v):
            self.v = v

        def latest(self):
            return np.full((180, 320, 3), self.v, np.uint8)

        def close(self):
            pass

    fm = FeedManager()
    fm._feeds = {"Player1": F(50), "Player2": F(200)}
    coach.feeds = fm
    grid = composite(fm.frames())
    assert grid.shape[1] == 1280

    class LLM3(FakeLLM):
        async def ask(self, system, content, **kw):
            self.calls.append(("ask", system, content, kw))
            return {"call": "Player2, nicht allein peeken!", "priority": 3, "phase": "fight", "observation": "P2 isoliert",
                    "enemy_tendency": "", "player_feedback": [{"player": "Player2", "verdict": "bad", "note": "solo peek"}],
                    "focus_player": "Player2", "focus_reason": "isoliert"}

    coach.llm = LLM3()

    async def go():
        await coach.start_match("snd")
        await coach.analyze_multi()
        await coach.analyze_multi()

    asyncio.run(go())
    assert coach.focus_player == "Player2"
    imgs = [b for b in coach.llm.calls[-1][2] if b.get("type") == "image"]
    assert len(imgs) == 2  # Kachelbild + Fokus-Spieler groß
    assert any("nicht allein" in s[0] for s in coach.voice.said)
    assert "MULTI-VIEW" in coach.llm.calls[-1][1]


def test_updater_keeps_user_data(tmp_path):
    import io
    import zipfile

    from coach import updater

    (tmp_path / "coach").mkdir()
    (tmp_path / "coach" / "a.py").write_text("old")
    (tmp_path / "config.yaml").write_text("mine")
    (tmp_path / ".env").write_text("SECRET=1")
    (tmp_path / "data" / "teams" / "main").mkdir(parents=True)
    (tmp_path / "data" / "teams" / "main" / "coach.db").write_text("db")
    (tmp_path / "requirements.txt").write_text("a")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("YvaraEsports-vyra-coach-abc123/coach/a.py", "new")
        z.writestr("YvaraEsports-vyra-coach-abc123/coach/b.py", "added")
        z.writestr("YvaraEsports-vyra-coach-abc123/config.yaml", "theirs")
        z.writestr("YvaraEsports-vyra-coach-abc123/.env", "X")
        z.writestr("YvaraEsports-vyra-coach-abc123/data/teams/main/coach.db", "wiped")
        z.writestr("YvaraEsports-vyra-coach-abc123/requirements.txt", "a\nb")
    res = updater.apply_zip(buf.getvalue(), tmp_path)
    assert (tmp_path / "coach" / "a.py").read_text() == "new"
    assert (tmp_path / "coach" / "b.py").read_text() == "added"
    assert (tmp_path / "config.yaml").read_text() == "mine"
    assert (tmp_path / ".env").read_text() == "SECRET=1"
    assert (tmp_path / "data" / "teams" / "main" / "coach.db").read_text() == "db"
    assert res == {"changed_files": 3, "requirements_changed": True}


# --------------------------------------------------------------- modes / lessons
def test_detect_mode():
    assert Coach.detect_mode("Vyra, Zuschau-Modus bitte") == "observe"
    assert Coach.detect_mode("training mode") == "teach"
    assert Coach.detect_mode("Live Modus an") == "live"
    assert Coach.detect_mode("wir spielen live") is None


def test_observe_mode_is_silent_and_lesson_flow(tmp_path, monkeypatch):
    coach = _coach(tmp_path, monkeypatch)
    coach.set_mode("observe", announce=False)
    assert coach.mode == "observe"
    coach.voice.said.clear()
    coach._call("Rotiert jetzt", priority=3)
    assert coach.voice.said == []

    res = asyncio.run(coach.start_lesson("B-Retake SnD", "de"))
    assert res and coach.mode == "teach" and coach.lesson["idx"] == 0
    said = " ".join(x[0] for x in coach.voice.said)
    assert "B-Retake" in said and "Schritt 1 von 2" in said
    assert "B-Treppen" in coach._teach_addendum()
    coach.lesson_next()
    assert "Schritt 2 von 2" in coach.voice.said[-1][0]
    coach.lesson_next()
    assert coach.lesson is None and coach.voice.said[-1][0] == "Sauber."


# --------------------------------------------------------------- callouts
def test_parse_and_annotate_calls():
    from coach.brain.callouts import CalloutIndex, annotate, parse_call

    idx = CalloutIndex([{"name": "Top Bridge", "zone": "C2", "aliases": ["Brücke oben"]},
                        {"name": "Bridge", "zone": "C3", "aliases": []}])
    p = parse_call("Zwei Top Bridge, einer ist lit, die pushen", idx)
    assert p["count"] == 2 and p["where"] == ["Top Bridge [C2]"] and "lit/angeschossen" in p["status"] and "push" in p["move"]
    assert parse_call("einer brücke oben", idx)["where"] == ["Top Bridge [C2]"]
    assert parse_call("bridge low", idx)["where"] == ["Bridge [C3]"]
    assert annotate("gg", idx) == "gg"
    assert "⟨" in annotate("3 bridge", idx)


def test_callouts_library_and_context(tmp_path, monkeypatch):
    from coach.study.library import Library

    coach = _coach(tmp_path, monkeypatch)
    coach.library = Library(tmp_path / "lib.db")
    coach.tracker.state.map = "Munition"
    assert coach.add_callout("Munition", "Top Bridge", "c2", ["Brücke oben"])
    coach.library.add_callout("munition", "Top Bridge", "D4", source="learned")  # gelernt überschreibt Manuelles nicht
    cos = coach.callouts("Munition")
    assert cos[0]["zone"] == "C2" and cos[0]["hits"] == 2
    ctx = coach._map_context("hardpoint", "Munition")
    assert "Top Bridge [C2]" in ctx and "MAP CALLOUTS" in ctx

    async def tool_chat(system, messages, tool_handler=None, **kw):
        await tool_handler("save_callout", {"map": "Munition", "name": "Silo", "zone": "A1", "aliases": [], "note": ""})
        return "ok"
    coach.llm.chat = tool_chat
    asyncio.run(coach.chat(coach.db.new_session(), "Silo ist oben links"))
    assert any(c["name"] == "Silo" for c in coach.callouts("Munition"))


# --------------------------------------------------------------- local llm
def test_local_llm_helpers():
    from coach.brain.local_llm import extract_json, parse_ddg, to_openai_content

    assert extract_json('<think>hm</think>```json\n{"a": 1}\n```') == {"a": 1}
    assert extract_json('Klar! {"call": ""} fertig') == {"call": ""}
    conv = to_openai_content([{"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": "QQ=="}},
                              {"type": "text", "text": "hi"}])
    assert conv[0]["image_url"]["url"] == "data:image/jpeg;base64,QQ==" and conv[1] == {"type": "text", "text": "hi"}
    page = ('<a rel="nofollow" class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fx.com%2Fa&amp;rut=1">CDL <b>News</b></a>'
            '<a class="result__snippet" href="#">Patch notes</a>')
    assert parse_ddg(page) == [{"title": "CDL News", "url": "https://x.com/a", "snippet": "Patch notes"}]


def test_local_llm_against_fake_server(tmp_path):
    from aiohttp import web
    from aiohttp.test_utils import TestServer

    from coach.brain.llm import make_llm

    seen = []

    async def completions(req):
        body = await req.json()
        seen.append(body)
        if body.get("tools") and len(seen) == 2:
            return web.json_response({"choices": [{"message": {"content": "", "tool_calls": [
                {"id": "c1", "type": "function", "function": {"name": "save_memory", "arguments": '{"fact": "X"}'}}]}}]})
        if "response_format" in body:
            return web.json_response({"choices": [{"message": {"content": '{"call": "Rotate", "priority": 2}'}}]})
        return web.json_response({"choices": [{"message": {"content": "Hey, what's up"}}]})

    async def go():
        app = web.Application()
        app.router.add_post("/v1/chat/completions", completions)
        async with TestServer(app) as srv:
            cfg = _cfg(llm={"provider": "openai", "base_url": str(srv.make_url("/v1")), "local_model": "qwen3-vl:8b"})
            llm = make_llm(cfg)
            assert llm.model == "qwen3-vl:8b"
            out = await llm.ask("sys", [{"type": "text", "text": "x"}], schema={"type": "object", "properties": {}})
            assert out == {"call": "Rotate", "priority": 2}
            saved = []

            async def handler(name, data):
                saved.append((name, data))
                return "saved"
            txt = await llm.chat("sys", [{"role": "user", "content": "hi"}], client_tools=[__import__("coach.brain.prompts", fromlist=["x"]).SAVE_MEMORY_TOOL],
                                 tool_handler=handler)
            assert txt == "Hey, what's up" and saved == [("save_memory", {"fact": "X"})]
            assert seen[-1]["messages"][-1]["role"] == "tool"

    asyncio.run(go())


def test_discord_auto_join(tmp_path):
    from types import SimpleNamespace as NS

    from coach.discord_bot import CoachBot

    bot = object.__new__(CoachBot)
    bot.cfg = _cfg(discord={"auto_join_channels": ["Scrims"]})
    bot.player = NS(vc=None)
    joined, left = [], []

    async def join_channel(ch, greet=True):
        joined.append(ch.name)
    bot.join_channel = join_channel
    scrims = NS(name="Scrims", id=1, members=[])
    other = NS(name="AFK", id=2, members=[])

    async def disconnect():
        left.append(1)
    guild = NS(voice_client=None)
    m = NS(bot=False, guild=guild)
    assert asyncio.run(bot._auto_join(m, NS(channel=None), NS(channel=scrims))) and joined == ["Scrims"]
    assert not asyncio.run(bot._auto_join(m, NS(channel=None), NS(channel=other)))
    guild.voice_client = NS(channel=scrims, is_connected=lambda: True, disconnect=disconnect)
    asyncio.run(bot._auto_join(m, NS(channel=scrims), NS(channel=None)))
    assert left == [1]


def test_discord_login_failure_does_not_crash():
    import discord

    from coach.app import start_discord

    class Bot:
        player = None
        closed = False

        async def start(self, token):
            raise discord.LoginFailure("bad")

        async def close(self):
            Bot.closed = True

    assert asyncio.run(start_discord(Bot(), "x", _cfg(), None)) is False and Bot.closed


def test_discord_command_groups_within_limit():
    """Discord: max. 25 Unterbefehle pro Gruppe, Namen eindeutig."""
    from types import SimpleNamespace

    from coach.discord_bot import build_groups

    groups = build_groups(SimpleNamespace())
    assert [g.name for g in groups] == ["coach", "match", "learn"]
    for g in groups:
        names = [c.name for c in g.commands]
        assert len(names) <= 25 and len(names) == len(set(names))
    assert {"start", "end", "score"} <= {c.name for c in groups[1].commands}
    assert {"start", "stop", "video"} <= {c.name for c in groups[2].commands}


def test_audio_device_with_bad_format_does_not_crash(monkeypatch, caplog):
    """soundcard wirft AssertionError (nicht-Extensible-Format) -> klare Meldung, kein Absturz."""
    import asyncio as _a
    import sys
    from types import SimpleNamespace

    from coach.audio import input as ai

    class Dev:
        name = "Kopfhörer (Test)"

        def recorder(self, **kw):
            raise AssertionError()

    monkeypatch.setattr(ai, "_pick_device", lambda kind, name: Dev())
    monkeypatch.setitem(sys.modules, "sounddevice", None)
    cfg = SimpleNamespace(get_path=lambda k, d=None: d)
    inp = ai.AudioInput(cfg, _a.new_event_loop(), _a.Queue())
    with caplog.at_level("ERROR"):
        inp._run("loopback", None)
    assert "lässt sich nicht öffnen" in caplog.text


def test_audio_device_reconnects_after_dropout(monkeypatch):
    import asyncio as _a
    from types import SimpleNamespace

    import numpy as np

    from coach.audio import input as ai

    calls = {"open": 0}

    class Rec:
        def __enter__(self):
            calls["open"] += 1
            return self

        def __exit__(self, *a):
            return False

        def record(self, numframes):
            if calls["open"] == 1:
                raise RuntimeError("Gerät weg")
            inp._stop.set()
            return np.zeros((numframes, 1), dtype="float32")

    class Dev:
        name = "Mic"

        def recorder(self, **kw):
            return Rec()

    monkeypatch.setattr(ai, "_pick_device", lambda kind, name: Dev())
    monkeypatch.setattr(ai.time, "sleep", lambda s: None)
    cfg = SimpleNamespace(get_path=lambda k, d=None: d)
    inp = ai.AudioInput(cfg, _a.new_event_loop(), _a.Queue())
    inp._run("loopback", None)
    assert calls["open"] == 2


def test_to_mono_16k_resamples_and_downmixes():
    import numpy as np

    from coach.audio.input import BLOCK, to_mono_16k

    rate, ch = 48000, 2
    n = BLOCK * rate // 16000
    raw = np.full(n * ch, 0.5, dtype=np.float32).tobytes()
    out = to_mono_16k(raw, ch, rate)
    assert len(out) == BLOCK and out.dtype == np.float32 and abs(float(out[len(out) // 2]) - 0.5) < 1e-3


def test_wasapi_device_picks_mic_and_loopback(monkeypatch):
    import sys
    from types import SimpleNamespace

    from coach.audio.input import wasapi_device

    monkeypatch.setitem(sys.modules, "pyaudiowpatch", SimpleNamespace(paWASAPI=13))
    devs = [
        {"index": 0, "name": "Tischmikrofon (RØDE NT-USB Mini)", "hostApi": 2, "maxInputChannels": 2},
        {"index": 1, "name": "Kopfhörer (3- A50 X Voice)", "hostApi": 2, "maxInputChannels": 0},
        {"index": 2, "name": "Kopfhörer (3- A50 X Voice) [Loopback]", "hostApi": 2, "maxInputChannels": 2,
         "isLoopbackDevice": True},
        {"index": 3, "name": "Tischmikrofon (RØDE NT-USB Mini)", "hostApi": 0, "maxInputChannels": 2},
    ]
    pa = SimpleNamespace(get_host_api_info_by_type=lambda t: {"index": 2, "defaultInputDevice": 0, "defaultOutputDevice": 1},
                         get_device_count=lambda: len(devs), get_device_info_by_index=lambda i: devs[i])
    assert wasapi_device(pa, "mic", None)["index"] == 0
    assert wasapi_device(pa, "mic", "RØDE")["index"] == 0
    assert wasapi_device(pa, "loopback", None)["index"] == 2
    assert wasapi_device(pa, "loopback", "Lautsprecher XY") is None


def test_ensure_requirements_runs_pip_once(tmp_path):
    from types import SimpleNamespace

    from coach.updater import ensure_requirements

    req, stamp, calls = tmp_path / "requirements.txt", tmp_path / "stamp", []
    req.write_text("numpy\n")
    run = lambda cmd, check=False: calls.append(cmd) or SimpleNamespace(returncode=0)  # noqa: E731
    assert ensure_requirements(req, stamp, run) is True
    assert ensure_requirements(req, stamp, run) is False
    req.write_text("numpy\nPyAudioWPatch\n")
    assert ensure_requirements(req, stamp, run) is True and len(calls) == 2


class _FakeStream:
    def __init__(self, owner, ch, n):
        self.owner, self.ch, self.n, self.reads = owner, ch, n, 0

    def read(self, n, exception_on_overflow=True):
        import numpy as np

        self.reads += 1
        if self.reads > 3:
            self.owner.inp._stop.set()
        return np.zeros(n * self.ch, dtype=np.float32).tobytes()

    def close(self):
        pass


class _FakePA:
    """Nachbau von pyaudiowpatch.PyAudio mit RØDE-Mikro + A50-Headset (UTF-8 als Latin-1 kaputt)."""
    instances = 0
    threads: set = set()

    def __init__(self, default_loopback=True):
        import threading as _t

        _FakePA.instances += 1
        _FakePA.threads.add(_t.current_thread().name)
        self.default_loopback = default_loopback
        self.opened = []
        self.devs = [
            {"index": 0, "name": "Tischmikrofon (RØDE NT-USB Mini)", "hostApi": 2, "maxInputChannels": 1,
             "defaultSampleRate": 48000.0},
            {"index": 1, "name": "KopfhÃ¶rer (3- A50 X Voice)", "hostApi": 2, "maxInputChannels": 0,
             "defaultSampleRate": 48000.0},
            {"index": 2, "name": "KopfhÃ¶rer (3- A50 X Voice) [Loopback]", "hostApi": 2, "maxInputChannels": 2,
             "defaultSampleRate": 48000.0, "isLoopbackDevice": True},
        ]

    def get_host_api_info_by_type(self, t):
        return {"index": 2, "defaultInputDevice": 0, "defaultOutputDevice": 1}

    def get_device_count(self):
        return len(self.devs)

    def get_device_info_by_index(self, i):
        return self.devs[i]

    def get_default_wasapi_loopback(self):
        if not self.default_loopback:
            raise LookupError("not found")
        return self.devs[2]

    def open(self, **kw):
        self.opened.append(kw["input_device_index"])
        return _FakeStream(self, kw["channels"], kw["frames_per_buffer"])


def _fake_windows_audio(monkeypatch, pa_kwargs=None):
    import asyncio as _a
    import sys
    from types import SimpleNamespace

    from coach.audio import input as ai

    pa_kwargs = pa_kwargs or {}
    holder = {}

    def make_pa():
        pa = _FakePA(**pa_kwargs)
        holder["pa"] = pa
        return pa

    monkeypatch.setitem(sys.modules, "pyaudiowpatch", SimpleNamespace(PyAudio=make_pa, paWASAPI=13, paFloat32=1))
    monkeypatch.setitem(sys.modules, "soundcard", SimpleNamespace())
    monkeypatch.setattr(ai.sys, "platform", "win32")
    srcs = [{"type": "mic"}, {"type": "loopback"}]
    cfg = SimpleNamespace(get_path=lambda k, d=None: srcs if k == "audio.sources" else d)
    inp = ai.AudioInput(cfg, _a.new_event_loop(), _a.Queue())
    return ai, inp, holder


def test_windows_audio_mic_and_loopback_via_wasapi(monkeypatch, caplog):
    import threading as _t

    _FakePA.instances, _FakePA.threads = 0, set()
    ai, inp, holder = _fake_windows_audio(monkeypatch)
    with caplog.at_level("INFO"):
        inp.start()
        holder["pa"].inp = inp
        for th in inp._threads:
            th.join(5)
    assert _FakePA.instances == 1 and _FakePA.threads == {_t.current_thread().name}  # nur einmal, im Haupt-Thread
    assert sorted(holder["pa"].opened) == [0, 2]
    assert "Höre auf mic" in caplog.text and "Höre auf loopback" in caplog.text


def test_windows_loopback_found_without_builtin_helper(monkeypatch):
    _, inp, holder = _fake_windows_audio(monkeypatch, {"default_loopback": False})
    from coach.audio.input import wasapi_device

    inp.start()
    holder["pa"].inp = inp
    for th in inp._threads:
        th.join(5)
    assert wasapi_device(holder["pa"], "loopback", None)["index"] == 2
    assert wasapi_device(holder["pa"], "loopback", "Kopfhörer (3- A50 X Voice)")["index"] == 2


def test_audio_thread_never_raises(monkeypatch, caplog):
    ai, inp, _ = _fake_windows_audio(monkeypatch)

    def boom(*a, **k):
        raise SystemExit("Treiber kaputt")

    monkeypatch.setattr(ai.AudioInput, "_run_wasapi", boom)
    inp._pa = object()
    with caplog.at_level("ERROR"):
        inp._run("mic", None)  # darf nichts nach außen werfen
    assert "läuft ohne sie weiter" in caplog.text


def test_discord_syncs_commands_per_server(monkeypatch, caplog):
    import discord
    from types import SimpleNamespace as NS

    from coach.discord_bot import CoachBot

    calls = []

    class Tree:
        def copy_global_to(self, guild):
            calls.append(("copy", guild.name))

        async def sync(self, guild=None):
            calls.append(("sync", guild.name if guild else None))
            if guild and guild.name == "Gesperrt":
                raise discord.Forbidden(NS(status=403, reason="Forbidden"), "Missing Access")
            return [1, 2, 3] if guild else []

        def clear_commands(self, guild=None):
            calls.append(("clear", guild))

    guilds = [NS(name="YVARA Esports", id=1), NS(name="Gesperrt", id=2)]
    monkeypatch.setattr(CoachBot, "tree", property(lambda self: Tree()))
    monkeypatch.setattr(CoachBot, "guilds", property(lambda self: guilds))
    monkeypatch.setattr(CoachBot, "invite_url", lambda self: "https://discord.com/oauth2/authorize?x")
    bot = object.__new__(CoachBot)
    bot.cfg = _cfg(discord={})
    with caplog.at_level("INFO"):
        asyncio.run(bot.sync_commands())
    assert ("sync", "YVARA Esports") in calls and ("clear", None) in calls and ("sync", None) in calls
    assert "3 Befehle registriert auf Server „YVARA Esports“" in caplog.text
    assert "neu einladen" in caplog.text


def test_voice_command_join_and_leave():
    from types import SimpleNamespace as NS

    from coach.brain.coach import Coach, detect_voice_cmd

    assert detect_voice_cmd("komm rein") == "join" and detect_voice_cmd("Komm mal in den Channel!") == "join"
    assert detect_voice_cmd("geh raus") == "leave" and detect_voice_cmd("rotier rein bei B") is None
    done = []

    async def join():
        done.append("join")

    async def leave():
        done.append("leave")

    fake = NS(join_voice=join, leave_voice=leave)
    asyncio.run(Coach.on_command(fake, "komm rein", "de"))
    asyncio.run(Coach.on_command(fake, "geh raus", "de"))
    assert done == ["join", "leave"]


def test_speaker_channel_prefers_follow_user(monkeypatch):
    from types import SimpleNamespace as NS

    from coach.discord_bot import CoachBot

    me = NS(bot=False, name="danie", id=7, display_name="Danie")
    a = NS(name="VC #1", members=[me])
    b = NS(name="VC #2", members=[NS(bot=False, name="x", id=1, display_name="x"), NS(bot=False, name="y", id=2, display_name="y")])
    monkeypatch.setattr(CoachBot, "guilds", property(lambda self: [NS(voice_channels=[a, b])]))
    bot = object.__new__(CoachBot)
    bot.cfg = _cfg(discord={"follow_user": "danie"})
    assert bot._speaker_channel() is a
    bot.cfg = _cfg(discord={})
    assert bot._speaker_channel() is b


def test_wake_word_variants_and_end_position():
    from coach.brain.coach import NAME_VARIANTS, is_addressed

    ww = ["coach", "Vyra", "hey Vyra"] + NAME_VARIANTS["vyra"] + NAME_VARIANTS["coach"]
    assert is_addressed("Coach hörst du mich?", ww) == (True, "hörst du mich")
    assert is_addressed("A, I, Viro, was geht?", ww) == (True, "was geht")
    assert is_addressed("Wo sind die, Coach?", ww) == (True, "wo sind die")
    assert is_addressed("zwei top bridge einer lit", ww)[0] is False


def test_ollama_native_think_off_with_fallback():
    import json as _j

    from aiohttp import web
    from aiohttp.test_utils import TestServer

    from coach.brain.llm import make_llm

    seen = []

    async def chat(req):
        body = await req.json()
        seen.append(body)
        if "think" in body:
            return web.json_response({"error": "model does not support thinking"}, status=400)
        lines = [{"message": {"content": "<think>hm</think>Ja, laut "}, "done": False},
                 {"message": {"content": "und klar."}, "done": True, "done_reason": "stop"}]
        return web.Response(text="\n".join(_j.dumps(x) for x in lines))

    async def go():
        app = web.Application()
        app.router.add_post("/api/chat", chat)
        async with TestServer(app) as srv:
            llm = make_llm(_cfg(llm={"provider": "ollama", "base_url": str(srv.make_url("/v1"))}))
            assert await llm.ask("sys", [{"type": "text", "text": "hörst du mich"},
                                         {"type": "image", "source": {"media_type": "image/jpeg", "data": "QUJD"}}]) \
                == "Ja, laut und klar."
            assert seen[0]["think"] is False and "think" not in seen[1]
            assert seen[1]["messages"][1]["images"] == ["QUJD"] and seen[1]["options"]["num_ctx"] == 8192

    asyncio.run(go())


def test_ollama_native_json_schema():
    from aiohttp import web
    from aiohttp.test_utils import TestServer

    from coach.brain.llm import make_llm

    async def chat(req):
        body = await req.json()
        assert body["format"]["type"] == "object"
        return web.json_response({"message": {"content": '{"respond": true, "reply": "Ja"}'}, "done": True})

    async def go():
        app = web.Application()
        app.router.add_post("/api/chat", chat)
        async with TestServer(app) as srv:
            llm = make_llm(_cfg(llm={"provider": "ollama", "base_url": str(srv.make_url("/v1"))}))
            return await llm.ask("sys", "x", schema={"type": "object", "properties": {}})

    assert asyncio.run(go()) == {"respond": True, "reply": "Ja"}


def test_answer_after_slow_tts_is_still_spoken():
    """Erste Antwort/Begrüßung darf nicht verfallen, nur weil die Stimme erst geladen wird."""
    from pathlib import Path

    from coach.audio.output import Player, Voice

    played = []

    class SlowTTS:
        async def synth(self, text, lang):
            await asyncio.sleep(0.3)
            return Path("x.wav")

    class P(Player):
        async def play(self, path):
            played.append(path)

    async def go():
        v = Voice(SlowTTS(), P())
        task = asyncio.create_task(v.run())
        v.say("Hey Jungs", priority=2, max_age=0.1)
        await asyncio.sleep(0.5)
        task.cancel()

    asyncio.run(go())
    assert played == [Path("x.wav")]


def test_clean_for_speech():
    from coach.audio.output import clean_for_speech

    assert clean_for_speech("Klar hör ich dich! 😄🔥 (haha) *grinst* Los geht's.") == "Klar hör ich dich! Los geht's."
    assert clean_for_speech("**Rotier** nach B 👉 https://x.y") == "Rotier nach B"


def test_ollama_warmup_hits_native_api():
    from aiohttp import web
    from aiohttp.test_utils import TestServer

    from coach.brain.llm import make_llm

    seen = []

    async def gen(req):
        seen.append(await req.json())
        return web.json_response({"done": True})

    async def go():
        app = web.Application()
        app.router.add_post("/api/generate", gen)
        async with TestServer(app) as srv:
            llm = make_llm(_cfg(llm={"provider": "ollama", "base_url": str(srv.make_url("/v1")), "local_model": "qwen3-vl:8b"}))
            assert await llm.warmup() is True
        assert seen == [{"model": "qwen3-vl:8b", "keep_alive": "30m", "options": {"num_ctx": 8192}}]

    asyncio.run(go())


def test_voice_rights_and_audio_description(tmp_path, caplog):
    import wave
    from types import SimpleNamespace as NS

    from coach.audio.output import describe_audio
    from coach.discord_bot import CoachBot

    wav = tmp_path / "a.wav"
    with wave.open(str(wav), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(22050)
        w.writeframes(b"\0\0" * 22050 * 2)
    me = NS(voice=NS(mute=True, suppress=False))
    vc = NS(_connection=NS(dave_protocol_version=1, can_encrypt=True), guild=NS(me=me))
    d = describe_audio(wav, vc)
    assert "2.0s" in d and "E2EE an" in d and "stummgeschaltet" in d
    ch = NS(name="Community VC #1", guild=NS(me=me), permissions_for=lambda m: NS(speak=False))
    bot = object.__new__(CoachBot)
    with caplog.at_level("ERROR"):
        probs = bot._check_voice_rights(ch)
    assert len(probs) == 2 and "Sprechen" in caplog.text


def test_discord_player_waits_for_e2ee_and_rejoins(caplog):
    from types import SimpleNamespace as NS

    from coach.audio.output import DiscordPlayer, dave_ready

    ses = NS(ready=False, status="pending", epoch=0, get_user_ids=lambda: [1])
    bad = NS(_connection=NS(dave_protocol_version=1, can_encrypt=False, dave_session=ses, dave_pending_transitions={}))
    good = NS(_connection=NS(dave_protocol_version=1, can_encrypt=True, dave_session=ses))
    assert not dave_ready(bad) and dave_ready(good) and dave_ready(NS(_connection=NS(dave_protocol_version=0)))
    p = DiscordPlayer()
    p.vc, p.wait_s = bad, 0.3

    async def rejoin():
        p.vc = good
    p.rejoin = rejoin
    with caplog.at_level("INFO"):
        assert asyncio.run(p._ensure_encryption()) is True
    assert "nicht bereit" in caplog.text and "Status pending" in caplog.text
    p2 = DiscordPlayer()
    p2.vc, p2.wait_s = bad, 0.2
    assert asyncio.run(p2._ensure_encryption()) is False


def test_split_sentences():
    from coach.brain.coach import split_sentences

    assert split_sentences("Hey! Was geht, Jungs? Alles gut bei") == (["Hey! Was geht, Jungs?"], "Alles gut bei")
    assert split_sentences("Map 2.5 Sekunden") == ([], "Map 2.5 Sekunden")


def test_ollama_stream_skips_think_and_answer_speaks_sentence_by_sentence():
    import json as _j
    from types import SimpleNamespace as NS

    from aiohttp import web
    from aiohttp.test_utils import TestServer

    from coach.brain.coach import Coach
    from coach.brain.llm import make_llm

    chunks = ["<thi", "nk>hmm</thi", "nk>Klar hör ich", " dich! Rotiert", " jetzt nach P2. Und", " ruhig bleiben."]

    async def chat(req):
        body = await req.json()
        assert body["stream"] is True and body["think"] is False
        resp = web.StreamResponse()
        await resp.prepare(req)
        for c in chunks:
            await resp.write((_j.dumps({"message": {"content": c}, "done": False}) + "\n").encode())
        await resp.write((_j.dumps({"message": {"content": ""}, "done": True}) + "\n").encode())
        return resp

    said = []

    async def go():
        app = web.Application()
        app.router.add_post("/api/chat", chat)
        async with TestServer(app) as srv:
            llm = make_llm(_cfg(llm={"provider": "ollama", "base_url": str(srv.make_url("/v1"))}))
            fake = NS(llm=llm, lang="de", cfg=_cfg(), feeds=None, last_frame=None, tracker=NS(state=NS(active=False)),
                      _sys=lambda *a, **k: "sys", _live_context=lambda: "ctx",
                      say=lambda text, priority=1, max_age=8, lang=None: said.append(text))
            return await Coach.answer(fake, "hörst du mich", "de")

    reply = asyncio.run(go())
    assert said == ["Klar hör ich dich!", "Rotiert jetzt nach P2.", "Und ruhig bleiben."]
    assert reply == "Klar hör ich dich! Rotiert jetzt nach P2. Und ruhig bleiben."


def test_join_tolerant_and_laughs_removed():
    from coach.audio.output import clean_for_speech
    from coach.brain.coach import detect_voice_cmd

    for t in ("komm r rein", "rein", "komm mal bitte rein", "rein kommen"):
        assert detect_voice_cmd(t) == "join", t
    for t in ("wie spiele ich rein", "rotier rein bei b", "reinpushen"):
        assert detect_voice_cmd(t) is None, t
    assert clean_for_speech("Haha, klar! LOL 😎") == "klar!"


def test_answer_falls_back_when_stream_is_empty():
    from types import SimpleNamespace as NS

    from coach.brain.coach import Coach

    class LLM:
        async def ask_stream(self, system, content):
            if False:
                yield ""

        async def ask(self, system, content, max_tokens=None):
            return "Ich coache live, mache Reviews und Trainingspläne. 😎"

    said = []
    fake = NS(llm=LLM(), lang="de", name="Vyra", cfg=_cfg(), feeds=None, last_frame=None, tracker=NS(state=NS(active=False)),
              _sys=lambda *a, **k: "sys", _live_context=lambda: "ctx",
              say=lambda text, priority=1, max_age=8, lang=None: said.append(text))
    reply = asyncio.run(Coach.answer(fake, "was kannst du", "de"))
    assert reply == "Ich coache live, mache Reviews und Trainingspläne." and said == [reply]


def test_proactive_chime_in_with_cooldown():
    from types import SimpleNamespace as NS

    from coach.brain.coach import Coach, looks_like_question

    assert looks_like_question("Was machen wir jetzt auf Hardpoint?")
    assert looks_like_question("should we rotate early")
    assert not looks_like_question("zwei top bridge einer lit")

    calls = []

    class LLM:
        async def ask(self, system, content, schema=None, max_tokens=None):
            calls.append(content)
            return {"respond": True, "reply": "Früh nach P2 rotieren, zwei halten Spawn. 😎"}

    said = []
    fake = NS(llm=LLM(), lang="de", cfg=_cfg(audio={"proactive_cooldown_s": 20}), mode="live", voice=NS(current=None),
              _recent_comms=lambda s: "",
              _sys=lambda *a, **k: "sys", say=lambda text, priority=1, max_age=8, lang=None: said.append(text))
    asyncio.run(Coach._maybe_chime_in(fake, "Was machen wir jetzt auf Hardpoint?", "de", "loopback"))
    asyncio.run(Coach._maybe_chime_in(fake, "Und auf P3?", "de", "loopback"))  # Cooldown
    assert said == ["Früh nach P2 rotieren, zwei halten Spawn."] and len(calls) == 1


def test_barge_in_interrupts_speech():
    from pathlib import Path

    from coach.audio.output import Player, Voice

    stopped = []

    class P(Player):
        async def play(self, path):
            pass

        def stop(self):
            stopped.append(1)

    class T:
        async def synth(self, text, lang):
            return Path("x.wav")

    v = Voice(T(), P())
    v.say("Satz eins.", priority=2)
    v.say("Satz zwei.", priority=2)
    v.say("Rotate now!", priority=3)
    from coach.audio.output import SpeechItem
    v.current = SpeechItem(priority=2, text="läuft")
    v.interrupt()
    assert stopped == [1] and len(v.queue) == 1 and v.interrupted_at > 0


def test_mic_barge_in_callback_while_speaking():
    import asyncio as _a
    from types import SimpleNamespace as NS

    import numpy as np

    from coach.audio import input as ai

    loop = _a.new_event_loop()
    inp = ai.AudioInput(NS(get_path=lambda k, d=None: d), loop, _a.Queue())
    inp.speaking, inp.muted_until = True, 9e9
    hits = []
    inp.on_barge_in = lambda: hits.append(1)
    frames = iter([np.full(ai.BLOCK, 0.2, dtype=np.float32)] * 20)

    def read():
        try:
            return next(frames)
        except StopIteration:
            inp._stop.set()
            return np.zeros(ai.BLOCK, dtype=np.float32)

    inp._listen("mic", read)
    loop.run_until_complete(_a.sleep(0))
    assert hits == [1]


def test_ollama_ensure_model_pulls_missing():
    import json as _j

    from aiohttp import web
    from aiohttp.test_utils import TestServer

    from coach.brain.llm import make_llm

    pulled = []

    async def tags(req):
        return web.json_response({"models": [{"name": "qwen3-vl:8b"}]})

    async def pull(req):
        pulled.append((await req.json())["model"])
        lines = [{"status": "pulling", "total": 100, "completed": 50}, {"status": "success"}]
        return web.Response(text="\n".join(_j.dumps(x) for x in lines))

    async def go():
        app = web.Application()
        app.router.add_get("/api/tags", tags)
        app.router.add_post("/api/pull", pull)
        async with TestServer(app) as srv:
            url = str(srv.make_url("/v1"))
            have = make_llm(_cfg(llm={"provider": "ollama", "base_url": url, "local_model": "qwen3-vl:8b"}))
            assert await have.ensure_model() is True and pulled == []
            new = make_llm(_cfg(llm={"provider": "ollama", "base_url": url}))
            assert new.model == "gemma3:12b"
            assert await new.ensure_model() is True and pulled == ["gemma3:12b"]

    asyncio.run(go())



def test_mic_question_answered_without_wake_word():
    from types import SimpleNamespace as NS

    import numpy as np

    from coach.brain.coach import Coach

    answered = []

    class STT:
        def __init__(self, text):
            self.text = text

        async def transcribe(self, audio, forced):
            return NS(text=self.text, language="de")

    async def on_command(text, lang):
        answered.append(text)

    def make(text):
        return NS(stt=STT(text), cfg=_cfg(audio={"log_comms": False}), lang="de", studier=None,
                  wake_words=lambda: ["coach"], on_command=on_command, roster=lambda: [{"ingame": "Max"}],
                  _to_teammate=lambda t: Coach._to_teammate(fake_self, t), _maybe_chime_in=lambda *a: asyncio.sleep(0))

    fake_self = NS(roster=lambda: [{"ingame": "Max"}])
    utt = NS(audio=np.zeros(16000, dtype=np.float32), source="mic", ptt=False)
    asyncio.run(Coach.handle_utterance(make("Was machen wir jetzt auf Hardpoint?"), utt))
    asyncio.run(Coach.handle_utterance(make("Max, wo bist du?"), utt))
    assert answered == ["Was machen wir jetzt auf Hardpoint?"]


def test_mark_english_terms_for_german_voice():
    from coach.audio.tts import mark_english

    fake = lambda w: {"push": "pˈʊʃ", "b site": "bˈiː sˈaɪt", "trade": "tɹˈeɪd", "top bridge": "tˈɑːp bɹˈɪdʒ"}.get(w, "X")  # noqa: E731
    out = mark_english("Jetzt pushen auf B Site und den Trade holen, nicht pro Runde rotieren.", phonemes=fake)
    assert out.startswith("Jetzt [[ pˈʊʃ ]]en auf [[ bˈiː sˈaɪt ]] und den [[ tɹˈeɪd ]] holen")
    assert "pro Runde rotieren" in out
    assert "[[ tˈɑːp bɹˈɪdʒ ]]" in mark_english("Zwei auf Top Bridge", extra=["Top Bridge"], phonemes=fake)
    assert mark_english("traden", phonemes=fake) == "[[ tɹˈeɪd ]]n"


def test_english_phonemes_real_espeak():
    import pytest

    pytest.importorskip("piper")
    from coach.audio.tts import _english_phonemes

    assert _english_phonemes("rotate") == __import__("unicodedata").normalize("NFD", "ɹoʊtˈeɪt")


def test_library_learning_history_and_migration(tmp_path):
    import sqlite3
    from coach.study.library import Library
    db = tmp_path / "lib.db"
    old = sqlite3.connect(str(db))  # alte Datenbank ohne neue Spalten
    old.execute("CREATE TABLE sources (id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, kind TEXT, ref TEXT, title TEXT, "
                "status TEXT, progress REAL DEFAULT 0, lessons INTEGER DEFAULT 0, note TEXT)")
    old.execute("INSERT INTO sources (ts, kind, ref, title, status) VALUES (1, 'url', 'x', 'Altes Video', 'done')")
    old.commit(); old.close()
    lib = Library(db)
    assert lib.sources()[0]["title"] == "Altes Video"  # bleibt erhalten
    sid = lib.add_source("url", "y", "CDL Match")
    lib.update_source(sid, duration=600)
    lib.add_watched(sid, 60); lib.add_watched(sid, 60)
    lib.add_lessons(sid, 0, [{"lesson": "Früh rotieren", "mode": "Hardpoint", "confidence": 0.9}])
    t = lib.totals()
    assert t["watched_s"] == 120 and t["lessons"] == 1 and t["sources"] == 2 and t["modes"] == {"hardpoint": 1}
    h = lib.history()
    assert h[0]["watched_s"] == 120 and h[0]["duration"] == 600 and h[0]["top"][0]["lesson"] == "Früh rotieren"


def test_split_first_chunk_early():
    from coach.brain.coach import split_first_chunk
    first, rest = split_first_chunk("Rotier jetzt nach P2, die Gegner sind")
    assert first and first.startswith("Rotier jetzt nach P2") and len(first) >= 18
    assert split_first_chunk("Kurz")[0] in ("", None)


def test_xtts_one_voice_both_languages_and_custom(tmp_path, monkeypatch):
    import asyncio
    import wave
    import numpy as np
    from coach.audio import tts as T
    monkeypatch.setattr(T, "CACHE", tmp_path)
    monkeypatch.setattr(T, "CUSTOM_DIR", tmp_path / "custom")
    calls = []

    class Model:
        class speaker_manager:
            speakers = {"Damien Black": {"gpt_cond_latent": "g", "speaker_embedding": "s"}}

        def get_conditioning_latents(self, audio_path):
            calls.append(("latents", audio_path[0]))
            return ("cg", "cs")

        def inference(self, text, lang, gpt, spk, speed=1.0, enable_text_splitting=False, **kw):
            calls.append((lang, gpt, speed))
            return {"wav": np.zeros(2400, dtype=np.float32)}

    t = T.TTS(_cfg(tts={"engine": "xtts", "xtts_voice": "Damien Black", "piper_speed": 1.5, "xtts_speed": 1.1}))
    t._xtts = Model()
    p1 = asyncio.run(t.synth("Rotiert früh.", "de"))
    p2 = asyncio.run(t.synth("Rotate early.", "en"))
    assert calls == [("de", "g", 1.1), ("en", "g", 1.1)]  # gleicher Sprecher für beide Sprachen
    with wave.open(str(p1)) as wf:
        assert wf.getframerate() == 24000
    assert p1 != p2 and t.xtts_speakers() == ["Damien Black"]
    (tmp_path / "custom").mkdir()
    (tmp_path / "custom" / "daniel.wav").write_bytes(b"x")
    asyncio.run(t.synth("Hallo.", "de", {"xtts_voice": "custom:Daniel"}))
    assert ("latents", str(tmp_path / "custom" / "daniel.wav")) in calls and calls[-1][1] == "cg"
    assert T.custom_voices() == ["daniel"] and T.slug_voice("Mein Mate!") == "mein_mate"


def test_xtts_not_set_up_falls_back_to_piper(tmp_path, monkeypatch):
    import asyncio
    from coach.audio import tts as T
    monkeypatch.setattr(T, "xtts_status", lambda: {"ready": False, "problem": "stimme_einrichten.bat ausführen"})
    t = T.TTS(_cfg(tts={"engine": "xtts"}))
    monkeypatch.setattr(t, "_piper_synth", lambda text, lang, opts=None: tmp_path / "p.wav")
    assert asyncio.run(t.synth("Hallo", "de")) == tmp_path / "p.wav"
    assert "stimme_einrichten" in t._xtts_failed


def test_leave_voice_variants():
    from coach.brain.coach import detect_voice_cmd
    for t in ["geh raus", "Geh mal raus!", "raus", "raus hier", "verpiss dich", "hau ab", "leave", "leave the channel",
              "tschüss", "Tschüss!", "bye bye", "verlass den Channel", "du kannst gehen", "gute nacht", "Feierabend"]:
        assert detect_voice_cmd(t) == "leave", t
    for t in ["raus aus B", "geh raus auf A und hol das Hill", "rotier raus nach P2", "die gehen raus mid", "push raus"]:
        assert detect_voice_cmd(t) != "leave", t


def test_leave_says_bye_then_leaves():
    import asyncio
    from coach.brain.coach import Coach
    order = []
    c = Coach.__new__(Coach)

    class Q:
        _heap = []

        def clear_below(self, p):
            return 0

    c.voice = type("V", (), {"current": None, "queue": Q()})()
    c.say = lambda text, **kw: order.append(("say", text))

    async def join():
        order.append("join")

    async def leave():
        order.append("leave")

    c.join_voice, c.leave_voice = join, leave
    asyncio.run(c.on_command("geh raus", "de"))
    assert order[0][0] == "say" and "raus" in order[0][1] and order[-1] == "leave"


def test_vibe_switch_and_prompt(tmp_path):
    from coach.brain.coach import Coach
    assert Coach.detect_vibe("sei mal toxisch") == "toxic" and Coach.detect_vibe("Roast-Modus an") == "toxic"
    assert Coach.detect_vibe("Flirt-Modus") == "flirty" and Coach.detect_vibe("sei sexy") == "flirty"
    assert Coach.detect_vibe("sei wieder normal") == "normal" and Coach.detect_vibe("hör auf zu roasten") == "normal"
    assert Coach.detect_vibe("die sind toxisch drüben") is None and Coach.detect_vibe("rotiert nach B") is None
    class C(Coach):
        team = type("T", (), {"name": "Yvara"})()

    c = C.__new__(C)
    c.cfg = _cfg()
    said = []
    c.say = lambda text, **kw: said.append(text)
    c.set_vibe("toxic", lang="de")
    assert c.vibe == "toxic" and said and "Roast" in said[0]
    assert "no attacks on origin" in c._sys("{personality}")
    c.pause_vibe()
    assert c.vibe == "normal" and "STYLE OVERRIDE" not in c._sys("{personality}")
    c.set_vibe("flirty", announce=False)
    assert "no sexual content" in c._sys("{personality}")


def test_timer_commands_and_bomb_countdown():
    import asyncio
    from coach.brain import tactics as TC
    d = TC.detect_timer_cmd
    assert d("Timer 30 Sekunden") == ("timer", 30) and d("timer zwei minuten") == ("timer", 120)
    assert d("dreißig sekunden timer") == ("timer", 30) and d("Hill ist neu") == ("hill", 0)
    assert d("Timer aus") == ("stop", 0) and d("Bombe liegt") == ("bomb", 0) and d("rotiert nach B") is None
    said = []
    tc = TC.TimerCalls(_cfg(timers={"bomb_s": 3.5, "defuse_s": 0.3}), lambda text, p: said.append(text), lambda: "de")

    async def run():
        tc.on_comms("die haben geplantet auf A", "snd")
        assert TC.PLANT_RE.search("wir haben das so geplant") is None
        assert "bomb" in tc.active
        await asyncio.sleep(0.75)

    asyncio.run(run())
    assert said[0].startswith("Bombe liegt") and said[-1].startswith("Letzte Chance")

    said.clear()

    async def run2():
        tc.on_comms("planted", "snd")
        tc.on_comms("defused", "snd")
        await asyncio.sleep(0.1)

    asyncio.run(run2())
    assert tc.active == []


def test_enemy_tendencies_pattern_and_memory(tmp_path):
    from coach.brain.tactics import EnemyTendencies, tendency_call
    t = EnemyTendencies(lambda: tmp_path / "tend.json")
    t.reset("OpTic", "Rewind", "snd")
    now = 1000.0
    for r in range(1, 5):
        t.round_no, t.round_start = r, now
        t.add("Top Bridge", now + 5)
        now += 100
    hit = t.check(True, now)
    assert hit == ("Top Bridge", "early") and t.check(True, now + 1) is None
    assert "Top Bridge" in tendency_call(*hit, "de") and "Top Bridge" in t.summary()
    t.save()
    t2 = EnemyTendencies(lambda: tmp_path / "tend.json")
    t2.reset("optic", "rewind", "snd")
    assert t2.history()[0][0] == "Top Bridge"


def test_map_heat_accumulates_and_renders(tmp_path):
    import numpy as np
    from coach.brain.tactics import MapHeat
    h = MapHeat(lambda: tmp_path / "heat")
    heat = {k: np.zeros((50, 50), np.float32) for k in ("ours", "enemy", "fights")}
    assert h.add_match("Rewind", heat, None) is None  # nichts erkannt -> nichts speichern
    heat["enemy"][10, 10] = 5
    heat["fights"][20, 20] = 2
    h.add_match("Rewind", heat, None)
    h.add_match("Rewind", heat, np.zeros((80, 80, 3), np.uint8))
    assert h.maps() == [{"map": "Rewind", "slug": "rewind", "matches": 2}]
    assert float(h.load("Rewind")["enemy"][10, 10]) == 10
    png = h.render("Rewind")
    assert png[:4] == b"\x89PNG" and h.render("Rewind", "fights")[:4] == b"\x89PNG" and h.render("Unbekannt") is None


def test_xtts_separate_english_voice(tmp_path, monkeypatch):
    import asyncio
    import numpy as np
    from coach.audio import tts as T
    monkeypatch.setattr(T, "CACHE", tmp_path)
    used = []

    class Model:
        class speaker_manager:
            speakers = {"Damien Black": {"gpt_cond_latent": "dam", "speaker_embedding": "s"},
                        "Ana Florence": {"gpt_cond_latent": "ana", "speaker_embedding": "s"}}

        def inference(self, text, lang, gpt, spk, speed=1.0, enable_text_splitting=False, **kw):
            used.append((lang, gpt))
            return {"wav": np.zeros(240, dtype=np.float32)}

    t = T.TTS(_cfg(tts={"engine": "xtts", "xtts_voice": "Damien Black", "xtts_voice_en": "Ana Florence"}))
    t._xtts = Model()
    asyncio.run(t.synth("Hallo.", "de"))
    asyncio.run(t.synth("Hello.", "en"))
    asyncio.run(t.synth("Hello again.", "en", {"xtts_voice_en": ""}))  # leer = gleiche Stimme
    assert used == [("de", "dam"), ("en", "ana"), ("en", "dam")]


def test_update_never_touches_voices_and_model(tmp_path):
    import io
    import zipfile
    from coach.updater import apply_zip
    (tmp_path / "data/voices/xtts/tts").mkdir(parents=True)
    (tmp_path / "data/voices/xtts/tts/model.pth").write_bytes(b"MODEL")
    (tmp_path / "data/voices/custom").mkdir(parents=True)
    (tmp_path / "data/voices/custom/daniel.wav").write_bytes(b"ME")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("vyra-coach/coach/x.py", "print(1)")
        z.writestr("vyra-coach/data/voices/custom/daniel.wav", "OVERWRITE")
    apply_zip(buf.getvalue(), tmp_path)
    assert (tmp_path / "data/voices/custom/daniel.wav").read_bytes() == b"ME"
    assert (tmp_path / "data/voices/xtts/tts/model.pth").read_bytes() == b"MODEL"
    assert (tmp_path / "coach/x.py").exists()


def test_old_slow_model_is_switched_once(tmp_path):
    import yaml
    from coach.__main__ import _migrate_config
    from coach.config import Config
    p = tmp_path / "config.yaml"
    p.write_text("llm:\n  local_model: qwen3-vl:8b\n", encoding="utf-8")
    cfg = Config({"_path": str(p), "llm": {"local_model": "qwen3-vl:8b"}})
    _migrate_config(cfg)
    assert cfg.get_path("llm.local_model") == "gemma3:12b"
    assert yaml.safe_load(p.read_text(encoding="utf-8"))["llm"]["local_model"] == "gemma3:12b"
    cfg.set_path("llm.local_model", "qwen3-vl:8b")  # bewusst zurückgestellt -> bleibt so
    _migrate_config(cfg)
    assert cfg.get_path("llm.local_model") == "qwen3-vl:8b"


def test_learns_memories_from_talk_without_merk_dir():
    import asyncio
    from coach.brain.coach import Coach
    saved = []

    class LLM:
        async def ask(self, system, content, **kw):
            assert "CONVERSATION" in content and "Shotzy spielt SMG" in content
            return {"facts": [{"fact": "Shotzy spielt bei uns SMG und entryt auf B.", "about": "Shotzy"}]}

    class DB:
        def memories(self, n):
            return []

        def add_memory(self, text, source):
            saved.append((text, source))

    class C(Coach):
        team = type("T", (), {"name": "Yvara"})()
        db = DB()

    c = C.__new__(C)
    c.cfg = _cfg()
    c.llm = LLM()
    tasks = []
    c._spawn = lambda coro: tasks.append(coro)
    c.remember_talk("Okay hör zu, Shotzy spielt SMG und entryt immer auf B", "Alles klar.", "de")
    c.remember_talk("ok", "", "de")  # zu kurz -> ignoriert
    assert len(tasks) == 1  # "spielt …" = jemand bringt Vyra was bei -> sofort merken
    asyncio.run(tasks[0])
    assert saved == [("Shotzy spielt bei uns SMG und entryt auf B.", "gespräch")]


def test_strats_are_described_for_the_ai(tmp_path):
    import json
    from coach.brain.tactics import strats_context
    (tmp_path / "a.json").write_text(json.dumps({"name": "B-Split", "map": "Rewind", "mode": "snd", "notes": "Smoke B Main",
        "shapes": [{"t": "player", "a": [0.1, 0.9], "s": "Shotzy"}, {"t": "arrow", "a": [0.1, 0.9], "b": [0.7, 0.3]},
                   {"t": "x", "a": [0.8, 0.1]}], "ts": 2}), encoding="utf-8")
    (tmp_path / "b.json").write_text(json.dumps({"name": "Andere", "map": "Hacienda", "shapes": [], "ts": 5}), encoding="utf-8")
    txt = strats_context(tmp_path, "Rewind", "snd", zone_name=lambda z: "Top Bridge" if z == "D2" else "")
    assert txt.index("B-Split") < txt.index("Andere")  # passende Map zuerst
    assert "Shotzy at A5" in txt and "route A5 -> Top Bridge (D2)" in txt and "enemy expected at E1" in txt


def test_watch_screen_switches_source_and_remembers_what_it_saw():
    import asyncio
    import numpy as np
    from coach.brain.coach import Coach

    class Cap:
        mode, monitor, window_title, region = "monitor", 1, "", None

    class LLM:
        async def ask(self, system, content, **kw):
            assert "WATCHING ALONG" in system
            return {"observation": "Shotzy hält B Main, Gegner rotieren zu A.", "game": "MW4", "mode": "snd", "map": "", "comment": ""}

    class C(Coach):
        team = type("T", (), {"name": "Yvara"})()

    c = C.__new__(C)
    c.cfg = _cfg()
    c.capture, c.llm, c.last_lang = Cap(), LLM(), "de"
    c.watch, c.watch_log = {"on": False, "source": "", "since": 0.0, "comment": False}, []
    c.say = lambda *a, **k: None
    c.roster = lambda: [{"ingame": "Shotzy"}]
    c._recent_comms = lambda s=120: ""
    c.set_watch(True, "window:Discord")
    assert c.watch["on"] and c.capture.mode == "window" and c.capture.window_title == "Discord"
    asyncio.run(c.spectate_frame(np.zeros((60, 80, 3), np.uint8)))
    assert "B Main" in c._watch_context()
    assert C.WATCH_ON_RE.search("schau mal zu") and C.WATCH_OFF_RE.search("hör auf zuzuschauen")
    c.set_watch(False)
    assert not c.watch["on"]


def test_desktop_app_mode(monkeypatch):
    import socket

    from coach import desktop, updater

    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    s.listen(1)
    port = s.getsockname()[1]
    assert desktop.server_up(port)
    s.close()
    assert not desktop.server_up(port)
    calls = []
    monkeypatch.setattr(desktop.subprocess, "Popen", lambda cmd, **kw: calls.append((cmd, kw)))
    desktop.spawn_background(["run"], 2)
    cmd, kw = calls[0]
    assert cmd[1:] == ["-m", "coach", "run"] and kw["env"]["VYRA_DESKTOP"] == "1"
    assert kw["env"]["VYRA_START_DELAY"] == "2"
    # Neustart im App-Modus startet neuen Hintergrund-Prozess statt Exit-Code 42
    monkeypatch.delenv("VYRA_DESKTOP", raising=False)
    assert not updater.relaunch_desktop()
    monkeypatch.setenv("VYRA_DESKTOP", "1")
    monkeypatch.setattr(updater.sys, "argv", ["coach", "local", "--no-update"])
    assert updater.relaunch_desktop()
    assert calls[-1][0][3:] == ["local", "--no-update"]
    assert "pywebview" in (desktop.ROOT / "requirements.txt").read_text()


def test_xtts_model_found_in_huggingface_cache(tmp_path, monkeypatch):
    """coqui-tts 0.28 lädt XTTS in den HF-Cache – genau das hat Vyra früher übersehen (-> Standardstimme)."""
    from coach.audio import tts as T
    monkeypatch.setattr(T, "XTTS_HOME", tmp_path / "data/voices/xtts")
    monkeypatch.setenv("HF_HOME", str(tmp_path / "hf"))
    for k in ("HF_HUB_CACHE", "HUGGINGFACE_HUB_CACHE", "LOCALAPPDATA", "XDG_DATA_HOME", "COQUI_TOS_AGREED"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr(T.Path, "home", lambda: tmp_path / "home")
    monkeypatch.setattr(T, "xtts_installed", lambda: True)
    st = T.xtts_status()
    assert not st["ready"] and "Lizenz" in st["problem"]
    tos = tmp_path / "data/voices/xtts/tts/tts_models--multilingual--multi-dataset--xtts_v2"
    tos.mkdir(parents=True)
    (tos / "tos_agreed.txt").write_text("ok")
    assert "nicht gefunden" in T.xtts_status()["problem"]
    snap = tmp_path / "hf/hub/models--tts-hub--XTTS-v2/snapshots/abc123"
    snap.mkdir(parents=True)
    for f in ("model.pth", "config.json", "vocab.json"):
        (snap / f).write_text("x")
    st = T.xtts_status()
    assert st["ready"] and st["dir"] == str(snap) and T.xtts_ready()


def test_custom_voice_reaches_synthesis_per_language(tmp_path, monkeypatch):
    """Ende-zu-Ende: Upload -> config -> live-Sprachausgabe nutzt die Stimmdatei (DE daniel, EN shotzy)."""
    import asyncio
    import numpy as np
    from coach.audio import tts as T
    monkeypatch.setattr(T, "CACHE", tmp_path)
    monkeypatch.setattr(T, "CUSTOM_DIR", tmp_path / "custom")
    (tmp_path / "custom").mkdir()
    for n in ("daniel", "shotzy"):
        (tmp_path / "custom" / f"{n}.wav").write_bytes(n.encode())
    seen = []

    class Model:
        def get_conditioning_latents(self, audio_path):
            return (audio_path[0], "spk")

        def inference(self, text, lang, gpt, spk, speed=1.0, enable_text_splitting=False, **kw):
            seen.append((lang, T.Path(gpt).name))
            return {"wav": np.zeros(240, dtype=np.float32)}

    cfg = _cfg(tts={"engine": "xtts", "xtts_voice": "custom:daniel", "xtts_voice_en": "custom:shotzy"})
    t = T.TTS(cfg)
    t._xtts = Model()
    de = asyncio.run(t.synth_info("Rotiert früh.", "de"))
    en = asyncio.run(t.synth_info("Rotate early.", "en"))
    assert seen == [("de", "daniel.wav"), ("en", "shotzy.wav")]
    assert de["engine"] == "xtts" and de["voice"] == "custom:daniel" and en["voice"] == "custom:shotzy"
    # Fehlt XTTS, sagt synth_info ehrlich, dass Piper gesprochen hat und warum
    t2 = T.TTS(cfg)
    monkeypatch.setattr(T, "xtts_status", lambda: {"ready": False, "problem": "Stimmenmodell nicht gefunden"})
    monkeypatch.setattr(t2, "_piper_synth", lambda text, lang, opts=None: tmp_path / "p.wav")
    info = asyncio.run(t2.synth_info("Hallo", "de"))
    assert info["engine"] == "piper" and "nicht gefunden" in info["errors"]["xtts"]
    t2.reset_xtts()
    assert t2._xtts_failed is None


def test_voice_sample_speech_detection():
    import numpy as np
    from coach.web.server import speech_seconds
    rate = 24000
    assert speech_seconds(np.zeros(rate * 10, np.float32), rate) == 0
    tone = 0.3 * np.sin(np.linspace(0, 2000 * np.pi, rate * 8)).astype(np.float32)
    assert speech_seconds(tone, rate) > 7


def test_torch_loaded_before_whisper_cudnn(monkeypatch):
    """WinError 127 (cudnn_cnn64_9.dll): PyTorch muss vor den nvidia-cudnn-DLLs der Spracherkennung laden."""
    from coach.audio import stt
    order = []
    monkeypatch.setattr(stt.sys, "platform", "win32")
    monkeypatch.setattr(stt, "preload_torch", lambda: order.append("torch"))
    import os
    monkeypatch.setattr(os, "add_dll_directory", lambda d: order.append("nvidia"), raising=False)
    stt._add_nvidia_dll_dirs()
    assert order and order[0] == "torch"


def test_gaming_mode_switches_model_and_pauses(monkeypatch):
    import asyncio
    from coach import perf
    assert perf.GAME_RE.match("cod.exe") and perf.GAME_RE.match("cod24.exe") and perf.GAME_RE.match("BlackOps7.exe")
    assert not perf.GAME_RE.match("code.exe") and not perf.GAME_RE.match("discord.exe")

    class LLM:
        provider, model, num_ctx = "ollama", "gemma3:12b", 8192
        unloaded, warmed = [], []

        async def unload(self, m=None):
            self.unloaded.append(m)

        async def warmup(self):
            self.warmed.append(self.model)
            return True

    class Coach:
        llm = LLM()

    cfg = _cfg(performance={"gaming_mode": "auto", "game_model": "gemma3:4b"})
    gm = perf.GamingMode(cfg, Coach)
    game = {"n": "cod.exe"}
    monkeypatch.setattr(gm, "detect", lambda: game["n"])
    monkeypatch.setattr(perf.GamingMode, "_set_priority", staticmethod(lambda low: None))
    asyncio.run(gm.tick())
    assert gm.active and Coach.gaming and Coach.llm.model == "gemma3:4b" and Coach.llm.num_ctx == 4096
    assert Coach.llm.unloaded == ["gemma3:12b"]
    game["n"] = None
    async def back():
        await gm.tick()
        await asyncio.sleep(0)
    asyncio.run(back())
    assert not gm.active and not Coach.gaming and Coach.llm.model == "gemma3:12b" and Coach.llm.num_ctx == 8192
    assert Coach.llm.unloaded[-1] == "gemma3:4b"
    assert perf.GamingMode(_cfg(performance={"gaming_mode": "off"}), Coach).detect() is None


def test_smalltalk_without_match_and_game_focus_in_match(tmp_path, monkeypatch):
    coach = _coach(tmp_path, monkeypatch)
    coach.set_mode("live", announce=False)
    assert not coach.game_focus()
    reply = asyncio.run(coach.answer("Was hältst du von der neuen Ryzen CPU?", "de"))
    system, content = coach.llm.calls[-1][1], coach.llm.calls[-1][2]
    assert "NOBODY is playing" in system and "RECENT EVENTS" not in str(content)
    assert "not seen them play" in str(content)
    asyncio.run(coach.start_match("hardpoint", "MapA", "TeamX"))
    assert coach.game_focus()
    asyncio.run(coach.answer("Wo soll ich stehen?", "de"))
    assert "NOBODY is playing" not in coach.llm.calls[-1][1]
    assert reply is None or isinstance(reply, str)


def test_voice_switch_by_voice_command(tmp_path, monkeypatch):
    from coach.audio import tts as T
    monkeypatch.setattr(T, "CUSTOM_DIR", tmp_path / "custom")
    (tmp_path / "custom").mkdir()
    for n in ("shotzy", "sinan", "daniel"):
        (tmp_path / "custom" / f"{n}.wav").write_bytes(b"x")
    saved = {}
    monkeypatch.setattr("coach.config.save_user_value", lambda cfg, k, v: (saved.__setitem__(k, v), cfg.set_path(k, v)))
    coach = _coach(tmp_path, monkeypatch)
    from coach.brain.coach import _norm
    assert coach.switch_voice_cmd(_norm("Coach, nimm auf Englisch die Stimme von Shotzy"), "de")
    assert saved["tts.xtts_voice_en"] == "custom:shotzy" and "tts.xtts_voice" not in saved
    assert coach.switch_voice_cmd(_norm("sprich mit Sinans Stimme"), "de")
    assert saved["tts.xtts_voice"] == "custom:sinan" and saved["tts.xtts_voice_en"] == ""
    assert not coach.switch_voice_cmd(_norm("die Stimme war laut im Spiel"), "de")
    assert not coach.switch_voice_cmd(_norm("nimm A und rotier"), "de")
    assert coach.switch_voice_cmd(_norm("Coach, nimm die Frauenstimme"), "de")
    assert saved["tts.xtts_voice"] == "Ana Florence"
    assert coach.switch_voice_cmd(_norm("Coach, nimm wieder die Standard Stimme"), "de")
    assert saved["tts.xtts_voice"] == "Damien Black"


def test_network_guard_allowlist():
    import socket
    from coach import safety
    assert safety.host_allowed("gateway.discord.gg") and safety.host_allowed("rr3---sn-abc.googlevideo.com")
    assert safety.host_allowed("usher.ttvnw.net") and safety.host_allowed("127.0.0.1") and safety.host_allowed("localhost")
    assert safety.host_allowed("huggingface.co") and safety.host_allowed("cas-bridge.xethub.hf.co")
    assert not safety.host_allowed("evil.example.com") and not safety.host_allowed("pastebin.com")
    assert not safety.host_allowed("discord.com.evil.io")
    safety.install(_cfg())
    try:
        import pytest
        with pytest.raises(socket.gaierror):
            socket.getaddrinfo("evil.example.com", 443)
    finally:
        safety.uninstall()


def test_voice_style_and_slots(tmp_path, monkeypatch):
    from coach.audio import tts as T
    monkeypatch.setattr(T, "CUSTOM_DIR", tmp_path / "custom")
    (tmp_path / "custom").mkdir()
    (tmp_path / "custom" / "alluka.wav").write_bytes(b"x")
    T.set_voice_style("Alluka", "locker, sagt ab und zu Bro")
    assert T.voice_style("alluka") == "locker, sagt ab und zu Bro" and T.custom_voices() == ["alluka"]
    coach = _coach(tmp_path, monkeypatch, tts={"engine": "xtts", "xtts_voice": "custom:daniel", "xtts_voice_en": "custom:alluka"})
    note = coach._voice_style_note()
    assert "English" in note and "Bro" in note and "German" not in note
    assert "Bro" in coach._sys("{personality}")
    T.set_voice_style("alluka", "")
    assert T.voice_style("alluka") == "" and T.MAX_VOICE_SLOTS == 10


def test_xtts_speed_capped_text_cleaned_and_tail_trimmed():
    import numpy as np
    from coach.audio import tts as T
    t = T.TTS(_cfg(tts={"engine": "xtts", "piper_speed": 1.5}))
    assert t.xtts_speed() == 1.05  # Piper-Tempo 1.5 macht XTTS blechern -> eigener Wert
    t2 = T.TTS(_cfg(tts={"engine": "xtts", "xtts_speed": 1.6}))
    assert t2.xtts_speed() == 1.15
    assert T.xtts_text("Okay bro 😂") == "Okay bro." and T.xtts_text("Warte... was?") == "Warte, was?"
    rate = 24000
    speech = 0.5 * np.sin(np.linspace(0, 3000, rate * 2)).astype(np.float32)
    junk = np.concatenate([np.zeros(int(0.6 * rate), np.float32), 0.4 * np.ones(int(0.1 * rate), np.float32),
                           np.zeros(int(0.3 * rate), np.float32)])
    out = T.trim_tail(np.concatenate([speech, junk]), rate)
    assert 2.0 * rate <= len(out) <= 2.2 * rate


def test_join_picks_owners_current_channel_live():
    from types import SimpleNamespace as NS
    from coach.discord_bot import CoachBot as VyraBot

    def mem(i, name, bot=False):
        return NS(id=i, name=name, display_name=name, bot=bot)

    g = NS(owner_id=999)
    ch1 = NS(name="Voice 1", guild=g, members=[mem(1, "a"), mem(2, "b"), mem(3, "c")])
    ch2 = NS(name="Voice 2", guild=g, members=[mem(42, "daniel")])
    g.voice_channels = [ch1, ch2]
    bot = NS(guilds=[g], cfg=_cfg(discord={"owner_id": "42"}))
    bot._owner_keys = lambda: VyraBot._owner_keys(bot)
    bot._is_person = VyraBot._is_person
    assert VyraBot._speaker_channel(bot) is ch2  # nicht der volle Channel 1
    ch2.members, ch1.members = [], ch1.members + [mem(42, "daniel")]  # gewechselt -> live neu
    assert VyraBot._speaker_channel(bot) is ch1
    bot.cfg = _cfg()
    ch2.members = [mem(999, "boss")]
    assert VyraBot._speaker_channel(bot) is ch2  # Server-Besitzer vor „vollster Channel“


def test_starts_in_talk_mode_and_switches_by_voice(tmp_path, monkeypatch):
    coach = _coach(tmp_path, monkeypatch)
    assert coach.mode == "talk"
    assert coach.detect_mode("coach match modus") == "live"
    assert coach.detect_mode("vyra konversations modus") == "talk"
    assert coach.detect_mode("training modus bitte") == "teach"
    assert coach.detect_mode("Vyra, Konversationsmodus") == "talk"
    assert coach.detect_mode("Coach, Matchmodus") == "live"
    assert coach.detect_mode("Trainingsmodus") == "teach"
    assert coach.detect_mode("ich hab eine kommode gekauft") is None
    from coach.vision.state import GameEvent
    coach.match_id = None
    asyncio.run(coach.handle_events([GameEvent("match_start", {})]))
    assert coach.match_id is None and not coach.game_focus()
    coach.set_mode("live", announce=False)
    assert coach.mode == "live"


def test_privacy_everything_local_by_default(tmp_path, monkeypatch):
    from coach import safety
    from coach.brain.llm import make_llm
    coach = _coach(tmp_path, monkeypatch)
    cfg = coach.cfg
    cfg.set_path("llm.provider", "claude")
    llm = make_llm(cfg)
    assert getattr(llm, "provider", "") == "ollama" and cfg.get_path("llm.provider") == "ollama"
    assert not coach._web_ok()
    try:
        safety.install(cfg)
        for h in ("api.anthropic.com", "speech.platform.bing.com", "html.duckduckgo.com", "evil.example.com"):
            assert not safety.host_allowed(h), h
        assert safety.host_allowed("gateway.discord.gg") and safety.host_allowed("localhost")
        cfg.set_path("privacy.allow_web_search", True)
        safety.install(cfg)
        assert safety.host_allowed("html.duckduckgo.com") and coach._web_ok()
    finally:
        safety.uninstall()


def test_cap_length_stops_invented_words_on_short_texts():
    import numpy as np
    from coach.audio.tts import cap_length
    rate = 24000
    wav = np.ones(int(2.0 * rate), dtype=np.float32) * 0.3  # „Okay. Wine.“ – viel zu lang
    out = cap_length(wav, rate, "Okay.", 1.05)
    assert len(out) / rate < 1.0 and abs(out[-1]) < 1e-3
    long = np.ones(int(6.0 * rate), dtype=np.float32)
    assert len(cap_length(long, rate, "Achtung, die stacken auf Hardpoint zwei, rotiert links.", 1.05)) == len(long)
    short_ok = np.ones(int(0.5 * rate), dtype=np.float32)
    assert len(cap_length(short_ok, rate, "Okay.", 1.05)) == len(short_ok)


def test_only_two_builtin_voices_and_egirl_style(tmp_path, monkeypatch):
    from coach.audio import tts as T
    assert [n for n, _ in T.XTTS_SPEAKERS] == ["Damien Black", "Ana Florence"]
    coach = _coach(tmp_path, monkeypatch, tts={"engine": "xtts", "xtts_voice": "Ana Florence"})
    note = coach._voice_style_note()
    assert "woman" in note and "flirty" in note
    coach.cfg.set_path("tts.xtts_voice", "Damien Black")
    assert coach._voice_style_note() == ""


def test_vibe_style_goes_last_in_system_prompt(tmp_path, monkeypatch):
    from coach.brain import prompts as P
    coach = _coach(tmp_path, monkeypatch)
    coach.cfg.set_path("coach.vibe", "toxic")
    sys_txt = coach._sys(P.SMALLTALK)
    assert sys_txt.rstrip().endswith(P.VIBES["toxic"].rstrip()) and "TOXIC MODE" in sys_txt
    coach.cfg.set_path("coach.vibe", "flirty")
    assert "FLIRTY MODE" in coach._sys(P.LIVE_ANSWER, max_words=20)
    coach.cfg.set_path("coach.vibe", "normal")
    assert "=== STYLE OVERRIDE" not in coach._sys(P.SMALLTALK)
    assert Coach.detect_vibe("Coach, sei toxisch") == "toxic" and Coach.detect_vibe("Vyra, Flirt-Modus") == "flirty"


def test_supertonic_engine_with_fake_module(tmp_path, monkeypatch):
    import sys
    import types

    import numpy as np

    from coach.audio import tts as T
    calls = {}

    class FakeST:
        sample_rate = 44100

        def __init__(self, model="supertonic-3"):
            calls["model"] = model

        def get_voice_style(self, name):
            calls["voice"] = name
            return name

        def synthesize(self, text, voice_style, lang=None, speed=1.0, **kw):
            calls["lang"], calls["text"] = lang, text
            return np.zeros((1, 4410), dtype=np.float32), 0.1
    monkeypatch.setitem(sys.modules, "supertonic", types.SimpleNamespace(TTS=FakeST))
    monkeypatch.setattr(T, "CACHE", tmp_path)
    t = T.TTS(_cfg(tts={"engine": "supertonic", "supertonic_voice": "F2"}))
    info = asyncio.run(t.synth_info("Hallo Team, rotiert früh.", "de"))
    assert info["engine"] == "supertonic" and info["voice"] == "F2" and info["path"].exists()
    assert calls == {"model": "supertonic-3", "voice": "F2", "lang": "de", "text": "Hallo Team, rotiert früh."}
