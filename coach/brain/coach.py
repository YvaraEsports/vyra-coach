"""Das Coach-Gehirn: verbindet Hören, Sehen, Denken, Sprechen und Lernen."""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from contextvars import ContextVar
from pathlib import Path
from typing import Awaitable, Callable

from ..i18n import norm_lang, t
from ..vision.state import GameEvent, StateTracker
from . import prompts as P
from .knowledge import Knowledge, norm_mode
from .callouts import CalloutIndex, annotate
from .llm import LLM, image_block

from ..audio.output import clean_for_speech  # noqa: E402

# Sätze, die während eines App-Calls gesagt werden, landen nur in DIESEM Call – nicht die Antworten, die
# parallel aufs Discord-Mikro/Loopback reagieren (die sollen nicht doppelt in der App auftauchen).
_CALL_CAPTURE: ContextVar[list | None] = ContextVar("call_capture", default=None)

log = logging.getLogger(__name__)

Notify = Callable[[str, Path | None], Awaitable[None]]

INTENTS = {
    "mute": ["sei ruhig", "sei still", "ruhe", "halt die klappe", "quiet", "be quiet", "shut up", "mute", "stop calls"],
    "unmute": ["calls an", "wieder an", "weiter callen", "unmute", "talk again", "calls on"],
    "review": ["review", "wie war die runde", "analyse die runde", "how was that round", "rate that round"],
    "plan": ["plan", "strategie", "strategy", "gameplan"],
    "comms": ["comms", "kommunikation", "communication"],
    "focus": ["fokus auf", "schau auf", "schau dir", "focus on", "watch", "ganzes team", "whole team"],
    "clip": ["clip das", "clip that", "clippen", "clip it", "markier das", "bookmark", "speicher das"],
    "timeout": ["timeout", "time out", "auszeit"],
    "next": ["weiter", "next", "nächster schritt", "naechster schritt", "next step"],
    "repeat": ["nochmal", "wiederholen", "repeat", "say again"],
    "check": ["passt das", "passt das so", "ist das richtig", "is this right", "check"],
    "team": ["wechsel zu", "wechsle zu", "coach jetzt", "coache jetzt", "jetzt team", "team wechseln", "switch to", "switch team"],
}


def _norm(text: str) -> str:
    return re.sub(r"[^\w\s]", " ", text.lower()).strip()


def is_addressed(text: str, wake_words: list[str]) -> tuple[bool, str]:
    """Prüft, ob der Coach angesprochen wurde (Wake-Word in den ersten Wörtern)."""
    norm = _norm(text)
    words = norm.split()
    for ww in sorted((_norm(w) for w in wake_words), key=len, reverse=True):
        ww_words = ww.split()
        n = len(ww_words)
        for start in range(0, min(3, max(0, len(words) - n + 1))):
            if words[start : start + n] == ww_words:
                return True, " ".join(words[start + n :]).strip()
        # „…, Coach?“ – Wake-Word ganz am Ende
        if len(words) > n and words[-n:] == ww_words:
            return True, " ".join(words[:-n]).strip()
    return False, norm


# So schreibt die Spracherkennung „Yvara AI“ / „Coach“ oft falsch
NAME_VARIANTS = {"yvara ai": ["yvara", "ivara", "iwara", "jvara", "javara", "ivara ai", "yvara ei", "ivara ei", "evara",
                              "ivara ki", "yvara ki", "vyra"],
                 "vyra": ["vira", "viera", "wira", "vyra", "viro", "vyro", "vaira", "wyra", "veira"],
                 "coach": ["couch", "coatch", "kotsch"]}


JOIN_RE = re.compile(r"^(bitte |mal |jetzt |hey |los )*(komm\w*( \w{1,6}){0,3} (rein|dazu|her|in (den|unseren|meinen) "
                     r"(channel|call|voice|talk))|rein( (kommen|da|hier|jetzt|bitte))?$|join( (the|our|my))?"
                     r"( (channel|voice|call|us))?|tritt bei|beitreten)\b")
LEAVE_RE = re.compile(
    r"^(bitte |mal |jetzt |ok |okay |so |du kannst |kannst du |alter )*("
    r"geh( mal| bitte| jetzt| doch)* (raus|offline|weg|schlafen)|raus( hier| mit dir| da)?|"
    r"hau( mal| jetzt| doch)* ab|verpiss dich|zieh leine|mach dich vom acker|"
    r"verlass( den| unseren| bitte den)? (channel|call|voice|talk)|leave( the| our)?( (channel|voice|call))?|"
    r"get out|disconnect|tsch(ü|ue|u)ss?|ciao|bye( bye)?|bis (dann|später|spaeter|morgen)|gute nacht|"
    r"(jetzt )?gehen|feierabend)"
    r"( (bitte|jetzt|mal|sofort|leute|jungs|alle|zusammen|aus dem (channel|call|voice|talk)|vyra|coach))*$")


TACTIC_Q_RE = re.compile(r"(wo soll ich|wo stell ich|wie stell ich|was mach ich|was mache ich|was soll ich|wie positionier|"
                         r"wie spielen wir|wie sollen wir|erklär|erkläre|erklaer|wenn (einer|der|die|jemand|zwei) (kommt|kommen)|"
                         r"where should i|what do i do|how do we play|how should we|explain|position)")

QUESTION_WORDS = {"wer", "was", "wie", "wo", "wohin", "wann", "warum", "wieso", "weshalb", "welche", "welcher", "welches",
                  "soll", "sollen", "sollten", "können", "koennen", "kann", "habt", "hat", "gibt", "ist", "sind", "machen",
                  "kannst", "weißt", "weisst", "hast", "bist", "meinst", "denkst", "sag", "erklär", "erklaer", "erkläre",
                  "who", "what", "how", "where", "when", "why", "which", "should", "can", "could", "do", "does", "is", "are",
                  "tell", "explain", "any"}
ASK_PHRASES = ("was machen wir", "was sollen wir", "wie spielen wir", "wo sollen wir", "was meinst du", "was denkst du",
               "kannst du", "weißt du", "sag mal", "what do we do", "what should we", "any ideas", "can you")
HELP_RE = re.compile(r"(wie funktioniert (die app|das programm|der caster|der sync|du)|wie geht das|wo finde ich|wo stell ich|"
                     r"wie stell ich|wie kann ich (dich|die app|den caster|das overlay)|wie benutze ich|was bedeutet der knopf|"
                     r"how do i (use|set up|turn on)|how does (the app|this app|the caster|sync) work|where do i find)")
EXPLAIN_RE = re.compile(r"(erklär|erklaer|check (das|ich|des) nicht|versteh (das|ich) nicht|verstehe (das )?nicht|kapier|"
                        r"wie funktionier|wie laufen die spawns|wie funktionieren die spawns|was ist (ein|eine|der|die|das) |"
                        r"warum (muss|soll|sollte)|wann muss ich|wann soll ich|explain|don't get|dont get|how do (spawns|rotations) work|"
                        r"what is a |why should)")
CAPS_RE = re.compile(r"(was kannst du|was machst du (alles|eigentlich)|was sind deine (funktionen|fähigkeiten)|"
                     r"wofür bist du|what can you do|what do you do|your (features|abilities)|wer bist du|who are you)")


def looks_like_question(text: str) -> bool:
    norm = _norm(text)
    words = norm.split()
    if len(words) < 3:
        return False
    return text.strip().endswith("?") or words[0] in QUESTION_WORDS or any(p in norm for p in ASK_PHRASES)


_SENT_END = re.compile(r"(.+?[.!?…])(\s+|$)", re.S)


def split_first_chunk(buf: str, min_len: int = 18) -> tuple[str, str]:
    """Für den Antwortbeginn: schon am ersten Komma vorlesen, damit Yvara AI sofort losredet."""
    m = re.match(r"(.{%d,}?[,;:–])\s+" % min_len, buf, re.S)
    if m and len(m.group(1)) <= 90:
        return m.group(1).strip(), buf[m.end():]
    return "", buf


def split_sentences(buf: str, min_len: int = 12) -> tuple[list[str], str]:
    """Fertige Sätze aus einem wachsenden Text holen. Sehr kurze Stücke werden mit dem nächsten Satz verbunden."""
    out, rest, acc = [], buf, ""
    while True:
        m = _SENT_END.match(rest)
        if not m or (not m.group(2) and m.end() == len(rest)):
            break
        acc += m.group(1) + " "
        rest = rest[m.end():]
        if len(acc.strip()) >= min_len:
            out.append(acc.strip())
            acc = ""
    return out, acc + rest


def detect_voice_cmd(text: str) -> str | None:
    """„Coach, komm rein“ -> join, „Coach, geh raus“ -> leave."""
    norm = " ".join(_norm(text).split())
    if JOIN_RE.search(norm):
        return "join"
    if LEAVE_RE.search(norm):
        return "leave"
    return None


def detect_intent(text: str) -> str | None:
    norm = _norm(text)
    if len(norm.split()) > 8:  # lange Sätze sind Fragen, keine Befehle
        return None
    for intent, keys in INTENTS.items():
        if any(re.search(rf"\b{re.escape(k)}\b", norm) for k in keys):
            return intent
    return None


class Coach:
    def __init__(self, cfg, llm: LLM, teams, knowledge: Knowledge, voice, notify: Notify | None = None,
                 capture=None, hud=None, stt=None) -> None:
        self.cfg = cfg
        self.llm = llm
        self.teams = teams
        self.kb = knowledge
        self.voice = voice
        self.notify = notify or (lambda text, path=None: asyncio.sleep(0))
        self.capture = capture
        self.hud = hud
        self.stt = stt
        self.tracker = StateTracker(knowledge.modes)
        from ..vision.minimap import MinimapTracker

        self.minimap = MinimapTracker(cfg)
        from .tactics import EnemyTendencies, MapHeat, TimerCalls

        self.timers = TimerCalls(cfg, self._timer_call, lambda: self.lang)
        self.tendencies = EnemyTendencies(lambda: self.team.dir / "tendencies.json")
        self.mapheat = MapHeat(lambda: self.team.dir / "heatmaps")
        self._last_tend_check = 0.0
        if cfg.get_path("coach.start_in_talk", True):  # jeder Start beginnt im Konversations-Modus
            cfg["coaching_mode"] = "talk"
        self.watch = {"on": False, "source": "", "since": 0.0, "comment": False}
        self.watch_log: list[tuple[float, str]] = []  # was Yvara AI beim Zuschauen gesehen hat
        self.match_id: int | None = None
        self.plan_text: str = ""
        self.last_lang = norm_lang(cfg.get("default_language"), "de")
        self.last_frame = None
        self.last_call_ts = 0.0
        self.recent_events: list[GameEvent] = []
        self.round_started = time.time()
        self.focus_player: str | None = None
        self.lesson: dict | None = None
        self.loss_streak = 0
        self.swings: list[float] = []
        self._last_tilt = 0.0
        self.library = None   # Taktik-Bibliothek (Lern-Modus), von app.py gesetzt
        self._co_cache = None  # (map, ts, CalloutIndex)
        self.discord_error = ""  # wird gesetzt, wenn der Discord-Bot nicht starten konnte
        self.studier = None
        self.autolearn = None
        self.feeds = None     # Mehrere Bildquellen (FeedManager), von app.py gesetzt
        self.caster = None    # Caster-Modus (Kommentator für den Stream), von app.py gesetzt
        self._bg: set[asyncio.Task] = set()

    # ------------------------------------------------------------------ utils
    @property
    def db(self):
        return self.teams.active.db

    @property
    def team(self):
        return self.teams.active

    @property
    def name(self) -> str:
        return self.cfg.get_path("coach.name", "Yvara AI")

    VIBES = ("normal", "toxic", "flirty")

    @property
    def vibe(self) -> str:
        v = self.cfg.get_path("coach.vibe", "normal")
        if v == "toxic" and time.time() < getattr(self, "_vibe_paused_until", 0):
            return "normal"  # bei echtem Tilt Pause vom Roasten
        return v if v in self.VIBES else "normal"

    def set_vibe(self, vibe: str, announce: bool = True, lang: str | None = None) -> None:
        if vibe not in self.VIBES:
            return
        self.cfg.setdefault("coach", {})["vibe"] = vibe
        self._vibe_paused_until = 0
        try:
            from ..config import save_user_value

            if self.cfg.get("_path"):
                save_user_value(self.cfg, "coach.vibe", vibe)
        except Exception as e:
            log.debug("Stil nicht gespeichert: %s", e)
        log.info("Persönlichkeit: %s", vibe)
        if announce:
            lang = lang or self.lang
            msg = {"toxic": ("Okay, Roast-Modus an. Ihr habt es so gewollt.", "Alright, roast mode on. You asked for it."),
                   "flirty": ("Oh, Charme-Modus an. Na, ihr Hübschen?", "Oh, charm mode on. Hey there, gorgeous squad."),
                   "normal": ("Alles klar, wieder ganz normal.", "Alright, back to normal.")}[vibe]
            self.say(msg[0] if lang == "de" else msg[1], priority=2, max_age=10, lang=lang)

    def pause_vibe(self, minutes: float = 10) -> None:
        if self.cfg.get_path("coach.vibe", "normal") == "toxic":
            self._vibe_paused_until = time.time() + minutes * 60
            log.info("Roast-Modus pausiert (%d min) – Team wirkt angespannt", minutes)

    @staticmethod
    def detect_vibe(text: str) -> str | None:
        n = _norm(text)
        if re.search(r"\b(hör|hoer|stop|stopp)\w* (mal |jetzt |bitte )*auf( mit dem| zu)? (roast|flame|flirt)\w*", n) \
                or re.search(r"\bstop (roasting|flaming|flirting)\b", n):
            return "normal"
        if not re.search(r"\b(modus|mode|sei|be|mach|werd|bleib|wieder|jetzt|stil|style|bitte)\b", n):
            return None
        if re.search(r"\b(toxisch|toxic|roast\w*|flame\w*|trash ?talk\w*|frech|salty)\b", n):
            return "toxic"
        if re.search(r"\b(flirt\w*|charmant\w*|charm|sexy|süß|suess|cute)\b", n):
            return "flirty"
        if re.search(r"\b(normal\w*|seriös|serioes|serious|ernst)\b", n) and not re.search(r"\b(live|observe|teach)\b", n):
            return "normal"
        return None

    def _sys(self, template: str, extra: str = "", **kw) -> str:
        from .prompts import BANTER, ROAST, VIBES

        personality = self.cfg.get_path("coach.personality", "") + self._voice_style_note()
        text = template.format(name=self.name, personality=personality, team=self.team.name, **kw) + extra
        if self.roast_level:
            text += BANTER.format(level=ROAST[self.roast_level])
        # Stil GANZ ans Ende: kleine lokale Modelle befolgen die letzte Anweisung am stärksten
        return text + VIBES.get(self.vibe, "")

    @property
    def roast_level(self) -> int:
        """Trash-Talk-Regler 0 (aus) bis 3 (brutal): so hart darf Yvara AI zurückfeuern, wenn man sie beleidigt."""
        try:
            return max(0, min(3, int(self.cfg.get_path("coach.roast_level", 1) or 0)))
        except (TypeError, ValueError):
            return 1

    def _voice_style_note(self) -> str:
        """Eigene Stimme mit Sprechstil (z. B. Alluka: „sagt ab und zu Bro“) -> so reden, wenn diese Stimme spricht."""
        try:
            from ..audio.tts import BUILTIN_STYLES, voice_style
        except Exception:
            return ""
        if str(self.cfg.get_path("tts.engine", "")) != "xtts":
            return ""
        de = str(self.cfg.get_path("tts.xtts_voice", "") or "")
        en = str(self.cfg.get_path("tts.xtts_voice_en", "") or "") or de
        notes = []
        for lang, v in (("German", de), ("English", en)):
            if v.startswith("custom:") and (st := voice_style(v[7:])):
                notes.append(f"When you answer in {lang} you speak with the voice of {v[7:]} - talk in their style: {st}")
            elif st := BUILTIN_STYLES.get(v):
                notes.append(f"When you answer in {lang} you speak with an adult woman's voice (around 30) - talk in this style: {st}")
        if not notes:
            return ""
        return ("\nVOICE STYLE (keep it natural, do not overdo catchphrases - at most once every few replies):\n"
                + "\n".join(dict.fromkeys(notes)))

    def wake_words(self) -> list[str]:
        ww = list(self.cfg.get_path("audio.wake_words", ["coach"]) or [])
        out = ww + [self.name, f"hey {self.name}"]
        for w in list(out):
            out += NAME_VARIANTS.get(_norm(w), [])
        return list(dict.fromkeys(out))

    def set_team(self, key: str) -> bool:
        if self.tracker.state.active:
            return False  # während eines Matches nicht wechseln
        team = self.teams.activate(key)
        if not team:
            return False
        self.match_id = None
        self.plan_text = ""
        self.focus_player = None
        return True

    # ----------------------------------------------------------------- modes
    MODES = ("talk", "live", "observe", "teach", "cast")

    @property
    def mode(self) -> str:
        m = self.cfg.get("coaching_mode", "talk")
        return m if m in self.MODES else "talk"

    def set_mode(self, mode: str, announce: bool = True) -> None:
        if mode not in self.MODES:
            return
        self.cfg["coaching_mode"] = mode
        try:
            from ..config import save_user_value

            if self.cfg.get("_path"):
                save_user_value(self.cfg, "coaching_mode", mode)
        except Exception as e:  # z. B. schreibgeschützt in Tests
            log.debug("Modus nicht gespeichert: %s", e)
        if mode != "teach":
            self.lesson = None
        caster = getattr(self, "caster", None)
        if caster is not None:
            try:
                caster.start() if mode == "cast" else caster.stop()
            except RuntimeError as e:  # ohne laufende Ereignisschleife (Tests)
                log.debug("Caster: %s", e)
        if announce:
            de = {"talk": "Konversations-Modus. Einfach quatschen, ich halte mich mit Spiel-Tipps zurück.",
                  "live": "Match-Modus an. Ich schaue zu und mache Calls.",
                  "observe": "Zuschau-Modus. Ich bin still und gebe euch am Ende Feedback.",
                  "teach": "Training-Modus. Sagt mir, was ich euch zeigen soll.",
                  "cast": "Caster-Modus an. Ich kommentiere das Match für den Stream."}
            en = {"talk": "Conversation mode. Just chat, I'll keep the game tips out of it.",
                  "live": "Match mode on. I'm watching and making calls.",
                  "observe": "Observer mode. I'll stay quiet and give feedback at the end.",
                  "teach": "Training mode. Tell me what you want me to teach.",
                  "cast": "Caster mode on. I'm casting the match for the stream."}
            self.say((de if self.lang == "de" else en)[mode], priority=2)

    @staticmethod
    def detect_mode(text: str) -> str | None:
        n = _norm(text)
        if not re.search(r"(modus|mode)\b", n):
            return None
        if re.search(r"(caster|kommentator|cast\b|casting|shoutcast)", n):
            return "cast"
        if re.search(r"\b(zuschau\w*|beobacht\w*|observ\w*|still|silent|watch)\b", n):
            return "observe"
        if re.search(r"\b(train\w*|lehr\w*|teach\w*|private|lern\w*)\b", n):
            return "teach"
        if re.search(r"\b(konversation\w*|gespräch\w*|quatsch\w*|unterhaltung\w*|talk\w*|chat\w*|smalltalk\w*|normal\w*)", n):
            return "talk"
        if re.search(r"\b(live|call\w*|match\w*|spiel\w*|game)", n):
            return "live"
        return None

    # --------------------------------------------------------------- lessons
    async def start_lesson(self, topic: str, lang: str | None = None) -> dict | None:
        lang = lang or self.lang
        if self.mode != "teach":
            self.set_mode("teach", announce=False)
        st = self.tracker.state
        ideas = self.library.ideas(self.team.name, 10) if self.library else []
        content = "\n\n".join(x for x in [
            f"WRITE IN: {lang}\nTOPIC: {topic}",
            f"ROSTER: {json.dumps(self.roster(), ensure_ascii=False)}",
            self._map_context(st.mode, st.map),
            self._library_context(st.mode, st.map, topic),
            "OWN TACTIC IDEAS: " + json.dumps([i["data"] for i in ideas], ensure_ascii=False)[:8000] if ideas else "",
            self._profiles_context(),
        ] if x)
        res = await self.llm.ask(self._sys(P.LESSON), content, deep=True, schema=P.LESSON_SCHEMA, max_tokens=8000)
        if not res or not res.get("steps"):
            self.say(t("thinking_error", lang), priority=2, lang=lang)
            return None
        self.lesson = {**res, "idx": 0, "lang": lang}
        self.say(res.get("intro", ""), priority=2, max_age=60, lang=lang)
        self._say_step()
        return res

    def _say_step(self) -> None:
        les = self.lesson
        if not les:
            return
        i, steps = les["idx"], les["steps"]
        lang = les["lang"]
        if i >= len(steps):
            self.say(les.get("outro") or ("Das war's. Stark, Jungs." if lang == "de" else "That's it. Nice work."),
                     priority=2, max_age=60, lang=lang)
            self.db.add_memory(f"Training durchgeführt: {les.get('title')}", "teach")
            self.lesson = None
            return
        prefix = f"Schritt {i + 1} von {len(steps)}: " if lang == "de" else f"Step {i + 1} of {len(steps)}: "
        self.say(prefix + steps[i]["say"], priority=2, max_age=90, lang=lang)

    def lesson_next(self) -> None:
        if self.lesson:
            self.lesson["idx"] += 1
            self._say_step()

    def lesson_repeat(self) -> None:
        self._say_step()

    def _teach_addendum(self) -> str:
        les = self.lesson
        if not les or les["idx"] >= len(les["steps"]):
            return ""
        step = les["steps"][les["idx"]]
        return P.TEACH.format(title=les.get("title", ""), n=les["idx"] + 1, total=len(les["steps"]),
                              step=step["say"], check=step.get("check", ""))

    def set_focus(self, player: str | None) -> None:
        self.focus_player = player or None

    @property
    def lang(self) -> str:
        fixed = self.cfg.get("language", "auto")
        return fixed if fixed in ("de", "en") else self.last_lang

    def _spawn(self, coro) -> None:
        task = asyncio.create_task(coro)
        self._bg.add(task)
        task.add_done_callback(self._bg.discard)

    def say(self, text: str, priority: int = 1, max_age: float = 8.0, lang: str | None = None) -> None:
        cap = _CALL_CAPTURE.get()
        if cap is not None:  # Sprach-Call in der App: Antwort geht an die App statt in Discord/Lautsprecher
            cap.append((text, lang or self.lang))
            return
        self.voice.say(text, lang or self.lang, priority=priority, max_age=max_age)

    async def call_turn(self, text: str, lang: str) -> list[tuple[str, str]]:
        """Ein Satz im App-Call: wie im Discord-Voice (Befehle, Antworten, Lernen), Antwort kommt als Liste zurück."""
        lock = self.__dict__.setdefault("_call_lock", asyncio.Lock())
        async with lock:
            self._say_capture = []
            token = _CALL_CAPTURE.set(self._say_capture)
            try:
                self.last_lang = lang
                n = _norm(text)
                fillers = {"okay.", "mhm.", "gute frage.", "right.", "good question."}
                await self.on_command(text, lang)
                out = [(tx, lg) for tx, lg in self._say_capture if tx.strip().lower() not in fillers or len(self._say_capture) == 1]
                log.info("App-Call: „%s“ -> %d Satz/Sätze", n[:80], len(out))
                return out
            finally:
                _CALL_CAPTURE.reset(token)
                self._say_capture = None

    def roster(self) -> list[dict]:
        return self.team.roster

    def _profiles_context(self) -> str:
        out = []
        team = self.db.get_profile("team", self.team.name)
        if team:
            out.append(f"TEAM PROFILE: {json.dumps(team, ensure_ascii=False)}")
        for p in self.roster():
            prof = self.db.get_profile("player", p["ingame"])
            line = f"PLAYER {p['ingame']} (role {p.get('role', '?')})"
            if prof:
                line += f": {json.dumps({k: prof.get(k) for k in ('summary', 'weaknesses', 'focus')}, ensure_ascii=False)}"
            out.append(line)
        opp = self.tracker.state.opponent
        if opp:
            op = self.db.get_profile("opponent", opp)
            if op:
                out.append(f"OPPONENT {opp}: {json.dumps(op, ensure_ascii=False)}")
        if self.team.notes:
            out.append(f"TEAM NOTES: {self.team.notes}")
        mems = self.db.relevant_memories(self.tracker.state.map + " " + self.tracker.state.opponent, limit=20)
        if mems:
            out.append("LONG-TERM MEMORY:\n" + "\n".join(f"- {m}" for m in mems))
        return "\n".join(out)

    def _library_context(self, mode: str, map_name: str = "", query: str = "") -> str:
        return self.library.context(mode, map_name, query) if self.library else ""

    # ----------------------------------------------------------------- brain
    async def brain_status(self, force: bool = False) -> dict:
        """Ist das KI-Modell erreichbar? Ergebnis 20 s gecacht."""
        cached = getattr(self, "_brain_cache", None)
        if cached and not force and time.time() - cached[0] < 20 and cached[2] is self.llm:
            return cached[1]
        check = getattr(self.llm, "check", None)
        try:
            ok, msg = await check() if check else (True, "")
        except Exception as e:
            ok, msg = False, f"Prüfung fehlgeschlagen: {e}"
        res = {"ok": ok, "message": msg}
        self._brain_cache = (time.time(), res, self.llm)
        return res

    # -------------------------------------------------------------- callouts
    def callouts(self, map_name: str | None = None) -> list[dict]:
        """Bekannte Callouts der Map: eigene/gelernte (Bibliothek) + maps.yaml."""
        map_name = map_name if map_name is not None else self.tracker.state.map
        if not map_name:
            return []
        items = self.library.callouts(map_name) if self.library else []
        have = {c["name"].lower() for c in items}
        for name in self.kb.find_map(map_name).get("callouts", []) or []:
            if str(name).lower() not in have:
                items.append({"name": str(name), "zone": "", "aliases": [], "note": "", "source": "maps.yaml"})
        return items

    def callout_index(self, map_name: str | None = None) -> CalloutIndex:
        key = (map_name if map_name is not None else self.tracker.state.map) or ""
        cached = self._co_cache
        if cached and cached[0] == key.lower() and time.time() - cached[1] < 30:
            return cached[2]
        idx = CalloutIndex(self.callouts(key))
        self._co_cache = (key.lower(), time.time(), idx)
        if self.stt is not None and key == (self.tracker.state.map or ""):
            self.stt.extra_vocab = ", ".join(c["name"] for c in idx.items[:40])
        tts = getattr(getattr(self, "voice", None), "tts", None)
        if tts is not None and hasattr(tts, "extra_english"):
            # englisch klingende Callouts (nur ASCII, ohne deutsche Umlaute) englisch aussprechen
            en = {"top", "bottom", "mid", "bridge", "window", "stairs", "back", "long", "short", "site", "garage", "balcony",
                  "heaven", "tower", "red", "blue", "green", "yellow", "white", "big", "small", "office", "spawn", "street",
                  "alley", "roof", "truck", "car", "bus", "boat", "shop", "store", "hall", "room", "door", "gate", "wall",
                  "hill", "corner", "lane", "pit", "kitchen", "bar", "lab", "main", "side", "center", "connector", "tunnel"}
            tts.extra_english = [c["name"] for c in idx.items
                                 if c["name"].isascii() and en & set(_norm(c["name"]).split())][:60]
        return idx

    def add_callout(self, map_name: str, name: str, zone: str = "", aliases: list[str] | None = None,
                    note: str = "", source: str = "manual") -> bool:
        if not self.library or not (map_name or self.tracker.state.map):
            return False
        self.library.add_callout(map_name or self.tracker.state.map, name, zone, aliases, note, source)
        self._co_cache = None
        return True

    def _map_context(self, mode: str, map_name: str | None) -> str:
        out = self.kb.context(mode, map_name)
        legend = self.callout_index(map_name or "").legend()
        if legend:
            out += (f"\nMAP CALLOUTS for {map_name} (name [minimap zone]): {legend}\n"
                    "Use these exact callout names when you talk about positions, and the zone to match minimap data.")
        return out

    def _comms_lines(self, rows: list[dict], map_name: str | None = None) -> str:
        idx = self.callout_index(map_name)
        return "\n".join(f"[{r['source']}] {annotate(r['text'], idx)}" for r in rows)

    def _recent_comms(self, seconds: float = 120) -> str:
        if not self.match_id:
            return ""
        rows = self.db.comms(self.match_id, since=time.time() - seconds)
        return self._comms_lines(rows[-30:])

    VOICE_WORD_RE = re.compile(r"(stimme|voice)\b")
    VOICE_ACT_RE = re.compile(r"\b(nimm|nehm|benutz\w*|nutz\w*|wechsel\w*|sprich|red|rede|klingen?|use|switch|talk|speak|sound)\b")

    def switch_voice_cmd(self, n_txt: str, lang: str) -> bool:
        """„Coach, nimm die Stimme von Shotzy“ / „sprich auf Englisch mit Sinans Stimme“ / „use the default voice“."""
        if not (self.VOICE_WORD_RE.search(n_txt) and self.VOICE_ACT_RE.search(n_txt)):
            return False
        from ..audio.tts import custom_voices
        from ..config import save_user_value

        names = custom_voices()
        hit = next((n for n in sorted(names, key=len, reverse=True)
                    if re.search(rf"\b{re.escape(n.replace('_', ' '))}s?\b", n_txt.replace("_", " "))), None)
        girl = None if hit else re.search(r"(frauen\w*|weiblich\w*|e ?girl|girl|female|\bfrau\b|mädchen)", n_txt)
        default = re.search(r"(standard|normal\w*|default|männ\w*|\bmale\b|männerstimme)", n_txt)
        if not hit and not girl and not default:
            return False
        vid = f"custom:{hit}" if hit else "Ana Florence" if girl else "Damien Black"
        target = "en" if re.search(r"\b(englisch\w*|english)\b", n_txt) else "de" if re.search(r"\b(deutsch\w*|german)\b", n_txt) else "both"
        try:
            if target in ("both", "de"):
                save_user_value(self.cfg, "tts.xtts_voice", vid)
            if target == "both":
                save_user_value(self.cfg, "tts.xtts_voice_en", "")
            if target == "en":
                save_user_value(self.cfg, "tts.xtts_voice_en", vid)
            save_user_value(self.cfg, "tts.engine", "xtts")
        except Exception as e:
            log.warning("Stimme nicht gewechselt: %s", e)
            return False
        who = hit or (("Yvara Girl" if girl else "Standard") if lang == "de" else ("Yvara Girl" if girl else "default"))
        log.info("Stimme per Sprachbefehl gewechselt: %s (%s)", vid, target)
        say_lang = "en" if target == "en" else "de" if target == "de" else lang
        self.say((f"Okay, ich klinge jetzt wie {who}." if say_lang == "de" else f"Okay, I sound like {who} now."),
                 priority=2, lang=say_lang)
        return True

    def game_focus(self) -> bool:
        """Spiel-Fokus nur, wenn ein Match läuft, Yvara AI zuschaut oder eine Trainings-Lektion läuft – sonst Smalltalk."""
        if self.mode == "talk":  # Konversations-Modus: nur Zuschauen (auf Wunsch) bringt das Spiel ins Gespräch
            return bool(self.watch.get("on"))
        return bool(self.tracker.state.active or self.watch.get("on") or (self.mode == "teach" and self.lesson))

    def _smalltalk_context(self, question: str) -> str:
        mems = self.db.relevant_memories(question, limit=8)
        parts = ["GAME STATE: no match running, nobody is playing right now. You have not seen them play today."
                 if not self.db_recent_match() else f"LAST MATCH (real data): {self.db_recent_match()}",
                 "THINGS YOU KNOW ABOUT THEM:\n" + "\n".join(f"- {m}" for m in mems) if mems else "",
                 f"RECENT TALK:\n{self._recent_comms(90)}"]
        return "\n\n".join(x for x in parts if x)

    def db_recent_match(self) -> str:
        """Kurzinfo zum letzten echten Match (heute), sonst leer – damit Yvara AI nichts erfindet."""
        try:
            rows = self.db.conn.execute("SELECT mode, map, opponent, score_ours || ':' || score_enemy, started "
                                        "FROM matches ORDER BY id DESC LIMIT 1").fetchall()
        except Exception:
            return ""
        if not rows:
            return ""
        r = rows[0]
        try:
            if time.time() - float(r[4] or 0) > 12 * 3600:
                return ""
        except (TypeError, ValueError):
            return ""
        return f"{r[0]} on {r[1] or '?'} vs {r[2] or '?'}: {r[3] or 'no result'}"

    def _live_context(self) -> str:
        st = self.tracker.state
        ev = ", ".join(f"{e.kind}{e.data}" for e in self.recent_events[-8:])
        parts = [
            f"GAME STATE: {st.summary() if st.active else 'no match running'}",
            f"RECENT EVENTS: {ev or '-'}",
            self.minimap.summary(20) if st.active else "",
            self.tendencies.summary() if st.active else "",
            self._watch_context(),
            self._strats_context(st.map, st.mode),
            f"RUNNING TIMERS: {', '.join(self.timers.active)}" if self.timers.active else "",
            self._map_context(st.mode, st.map) if st.active else "",
            self._library_context(st.mode, st.map) if st.active else "",
            f"PLAN: {self.plan_text[:1500]}" if self.plan_text else "",
            self._profiles_context(),
            f"RECENT COMMS:\n{self._recent_comms()}",
        ]
        return "\n\n".join(p for p in parts if p)

    # ---------------------------------------------------------------- matches
    async def start_match(self, mode: str, map_name: str = "", opponent: str = "") -> None:
        mode = norm_mode(mode)
        self.tracker.start_match(mode, map_name, opponent)
        self.minimap.reset()
        self.timers.stop_all()
        self.tendencies.reset(opponent, map_name, mode)
        self.match_id = self.db.start_match(mode, map_name, opponent)
        self.recent_events.clear()
        self.round_started = time.time()
        if self.plan_text:
            self.db.set_plan(self.match_id, self.plan_text)
        mode_name = self.kb.modes.get(mode, {}).get("name", mode)
        self.say(t("match_started", self.lang, mode=mode_name, map=map_name or "?"), priority=2)
        hist = self.tendencies.history()[:3] if opponent else []
        if hist:
            places = ", ".join(w for w, _c in hist)
            self.say(f"Gegen {opponent} hier letztes Mal oft: {places}." if self.lang == "de"
                     else f"Last time {opponent} here were often: {places}.", priority=2, max_age=20)

    async def end_match(self, ours: int | None = None, enemy: int | None = None) -> None:
        if not self.match_id:
            return
        st = self.tracker.state
        ours = st.score_ours if ours is None else ours
        enemy = st.score_enemy if enemy is None else enemy
        st.score_ours, st.score_enemy = ours, enemy
        st.active = False
        self.timers.stop_all()
        try:
            self.tendencies.save()
            self.mapheat.add_match(st.map, self.minimap.heat, self.minimap.last_crop)
        except Exception as e:
            log.warning("Tendenzen/Heatmap nicht gespeichert: %s", e)
        self.db.end_match(self.match_id, ours, enemy)
        self.say(t("match_ended", self.lang), priority=2)
        mid = self.match_id
        await self.review_match(mid)
        await self.learn_from_match(mid)

    # ------------------------------------------------------------------ voice
    async def handle_utterance(self, utt) -> None:
        if not self.stt:
            return
        forced = self.cfg.get("language") if self.cfg.get("language") in ("de", "en") else None
        t0 = time.time()
        tr = await self.stt.transcribe(utt.audio, forced)
        if not tr.text or len(tr.text) < 2:
            return
        lang = norm_lang(tr.language, self.lang)
        log.info("[%s/%s] %s  (Erkennung %.1fs für %.1fs Sprache)", utt.source, lang, tr.text, time.time() - t0,
                 len(utt.audio) / 16000)
        if self.studier and utt.source == "loopback":
            self.studier.feed_transcript(tr.text)
        if self.cfg.get_path("audio.log_comms", True):
            self.db.add_comms(self.match_id, utt.source, lang, tr.text)
            self._check_comms_tilt(lang)
        try:
            self._comms_tactics(tr.text)
        except Exception as e:
            log.debug("Taktik aus Comms: %s", e)

        addressed, rest = is_addressed(tr.text, self.wake_words())
        if utt.ptt or (utt.source == "mic" and self.cfg.get_path("audio.always_listen_mic", False)):
            addressed, rest = True, (rest if addressed else tr.text)
        if not addressed:
            if utt.source == "mic" and self.cfg.get_path("audio.mic_auto_answer", True) and looks_like_question(tr.text) \
                    and not self._to_teammate(tr.text):
                # Deine eigene Frage am Mikro: direkt beantworten, auch ohne „Coach“
                log.info("Einmischen: ja – Frage am eigenen Mikro")
                self.last_lang = lang
                await self.on_command(tr.text, lang)
            elif self.cfg.get_path("audio.proactive", True) and (looks_like_question(tr.text) or utt.source == "mic"):
                asyncio.create_task(self._maybe_chime_in(tr.text, lang, utt.source))
            return
        self.last_lang = lang
        log.info("Angesprochen (%s): „%s“", utt.source, rest or "…")
        if not rest:
            self.say("Ja?" if lang == "de" else "Yes?", priority=2, max_age=10, lang=lang)
            return
        await self.on_command(rest, lang)

    def _to_teammate(self, text: str) -> bool:
        """Frage beginnt mit dem Namen eines Mates („Max, wo bist du?“) -> nicht für Yvara AI."""
        first = " ".join(_norm(text).split()[:2])
        try:
            names = [_norm(p.get("ingame", "")) for p in self.roster()]
        except Exception:
            names = []
        return any(n and n in first for n in names)

    async def _maybe_chime_in(self, text: str, lang: str, source: str) -> None:
        """Ohne Wake-Word: bei einer passenden Frage in den Comms selbst antworten (mit Pause gegen Spam)."""
        now = time.time()
        cooldown = float(self.cfg.get_path("audio.proactive_cooldown_s", 8))
        if now - getattr(self, "_last_chime", 0.0) < cooldown:
            log.info("Einmischen: nein – Pause (%.0fs) läuft noch", cooldown)
            return
        if getattr(self, "_chiming", False):
            return
        if self.mode == "observe":
            log.info("Einmischen: nein – Modus „Beobachten“")
            return
        if getattr(self.voice, "current", None) is not None:
            log.info("Einmischen: nein – ich rede gerade")
            return
        self._chiming = True
        try:
            t0 = time.time()
            focus = self.game_focus() if hasattr(self, "game_focus") else True
            system = self._sys(P.PROACTIVE if focus else P.SMALLTALK_PROACTIVE,
                               max_words=self.cfg.get_path("llm.max_answer_words", 35))
            res = await self.llm.ask(system, f"RECENT COMMS:\n{self._recent_comms(60)}\n\nJUST SAID ({source}): {text}\n"
                                             f"LANGUAGE: {lang}", schema=P.PROACTIVE_SCHEMA, max_tokens=300)
            reply = clean_for_speech((res or {}).get("reply", "")) if isinstance(res, dict) else ""
            if isinstance(res, dict) and res.get("respond") and reply:
                self._last_chime = time.time()
                log.info("Einmischen: ja (%.1fs) – „%s“ → %s", time.time() - t0, text, reply)
                self.say(reply, priority=2, max_age=15, lang=lang)
            elif res is None:
                log.info("Einmischen: nein – KI hat nicht verständlich geantwortet (%.1fs)", time.time() - t0)
            else:
                log.info("Einmischen: nein – nicht für mich / nichts Hilfreiches (%.1fs): „%s“", time.time() - t0, text)
        except Exception as e:
            log.warning("Einmischen fehlgeschlagen: %s", e)
        finally:
            self._chiming = False

    async def _wait_quiet(self, timeout: float) -> None:
        voice = getattr(self, "voice", None)
        end = time.time() + timeout
        await asyncio.sleep(0.3)
        while voice is not None and time.time() < end and (voice.current or voice.queue._heap):
            await asyncio.sleep(0.2)

    async def on_command(self, text: str, lang: str) -> None:
        vcmd = detect_voice_cmd(text)
        if vcmd and getattr(self, "join_voice", None):
            try:
                if vcmd == "join":
                    await self.join_voice()
                elif getattr(self, "leave_voice", None):
                    log.info("Sprachbefehl: Voice-Channel verlassen („%s“)", text)
                    if getattr(self, "voice", None) is not None:
                        self.voice.queue.clear_below(3)
                        self.say(t("bye", lang), priority=2, max_age=10, lang=lang)
                        await Coach._wait_quiet(self, 6.0)  # erst verabschieden, dann raus
                    await self.leave_voice()
            except Exception as e:
                log.warning("Sprachbefehl %s fehlgeschlagen: %s", vcmd, e)
            return
        from .tactics import detect_timer_cmd

        tcmd = detect_timer_cmd(text)
        timers = getattr(self, "timers", None)
        if tcmd and timers is not None:
            kind, secs = tcmd
            if kind == "stop":
                timers.stop_all()
                self.say("Timer aus." if lang == "de" else "Timers off.", priority=2, lang=lang)
            elif kind == "hill":
                timers.hill_sync()
                self.say("Hill-Uhr läuft." if lang == "de" else "Hill clock running.", priority=2, lang=lang)
            elif kind == "bomb":
                timers.bomb_planted()
            else:
                timers.custom(secs)
            return
        n_txt = _norm(text)
        if self.switch_voice_cmd(n_txt, lang):
            return
        if self.WATCH_OFF_RE.search(n_txt) and hasattr(self, "set_watch"):
            self.set_watch(False)
            return
        tw = self.TWITCH_WATCH_RE.search(n_txt)
        if tw and hasattr(self, "watch_twitch"):
            self.watch_twitch(tw.group("ch") or tw.group("ch2"))
            return
        if self.WATCH_ON_RE.search(n_txt) and hasattr(self, "set_watch"):
            self.set_watch(True)
            return
        new_vibe = Coach.detect_vibe(text)
        if new_vibe and hasattr(self, "set_vibe"):
            self.set_vibe(new_vibe, lang=lang)
            return
        new_mode = self.detect_mode(text)
        if new_mode:
            self.set_mode(new_mode)
            return
        intent = detect_intent(text)
        if self.mode == "teach" and self.lesson and intent in ("next", "repeat", "check"):
            if intent == "next":
                self.lesson_next()
            elif intent == "repeat":
                self.lesson_repeat()
            else:
                await self.answer(f"Check the current training step: {self._teach_addendum()} Is it set up right? "
                                  f"Correct specific players. Player says: {text}", lang)
            return
        if self.mode == "teach" and not self.lesson and text and intent is None:
            await self.start_lesson(text, lang)
            return
        if intent == "mute":
            self.voice.enabled = False
            self.say(t("muted", lang), priority=2, lang=lang)
        elif intent == "unmute":
            self.voice.enabled = True
            self.say(t("unmuted", lang), priority=2, lang=lang)
        elif intent == "review":
            await self.review_round(force=True)
        elif intent == "plan" and not self.tracker.state.active:
            st = self.tracker.state
            await self.make_plan(st.mode, st.map, st.opponent)
        elif intent == "comms":
            await self.comms_check()
        elif intent == "clip":
            self.save_clip(text)
            self.say("Geclippt." if lang == "de" else "Clipped.", priority=2, lang=lang)
        elif intent == "timeout":
            self.timeout_reset(lang)
        elif intent == "focus":
            player = next((p["ingame"] for p in self.roster() if p["ingame"].lower() in _norm(text)), None)
            self.set_focus(player)
            msg = (f"Okay, ich schaue auf {player}." if lang == "de" else f"Okay, watching {player}.") if player else \
                  ("Okay, ganzes Team." if lang == "de" else "Okay, whole team.")
            self.say(msg, priority=2, lang=lang)
        elif intent == "team":
            target = next((t for t in self.teams.list() if _norm(t["name"]) in _norm(text) or t["slug"] in _norm(text).replace(" ", "-")), None)
            if target and self.set_team(target["slug"]):
                self.say(f"Team gewechselt: {self.team.name}." if lang == "de" else f"Switched to {self.team.name}.", priority=2, lang=lang)
        elif text:
            await self.answer(text, lang)

    # ------------------------------------------------------- mental / clips
    def timeout_reset(self, lang: str | None = None) -> None:
        lang = lang or self.lang
        msg = ("Timeout-Moment, Jungs. Durchatmen. Vergesst die letzten Runden. Nächste Runde: zusammen spielen, "
               "erst Info, dann Trade. Einfach. Wir holen das." if lang == "de" else
               "Timeout moment, boys. Breathe. Forget the last rounds. Next one: play together, info first, then trade. "
               "Simple. We get this back.")
        self.say(msg, priority=3, max_age=20, lang=lang)
        self.db.add_event(self.match_id, "timeout_suggested", {})

    def _check_comms_tilt(self, lang: str) -> None:
        """Viele Comms in kurzer Zeit = Hektik/Tilt -> Ruhe reinbringen."""
        if not self.match_id or self.mode != "live" or time.time() - self._last_tilt < 180:
            return
        recent = self.db.comms(self.match_id, since=time.time() - 30)
        if len(recent) >= int(self.cfg.get_path("callouts.tilt_comms_30s", 14)):
            self._last_tilt = time.time()
            self.pause_vibe()
            self.say("Comms ruhiger, Jungs. Nur Infos: wo, wie viele, lit. Rest später." if lang == "de" else
                     "Calm comms, boys. Info only: where, how many, lit. Rest later.", priority=2, lang=lang)

    def save_clip(self, note: str = "") -> int | None:
        path = ""
        if self.last_frame is not None:
            try:
                import cv2

                clip_dir = self.team.dir / "clips"
                clip_dir.mkdir(parents=True, exist_ok=True)
                path = str(clip_dir / f"clip_{int(time.time())}.jpg")
                cv2.imwrite(path, self.last_frame)
            except Exception as e:
                log.warning("Clip-Bild nicht gespeichert: %s", e)
        return self.db.add_clip(self.match_id, self.tracker.state.round_no, note, path)

    def _clip_images(self, match_id: int, limit: int = 4) -> list[dict]:
        from ..vision.capture import to_jpeg_b64

        blocks = []
        for c in self.db.clips(match_id)[-limit:]:
            if c["image"] and Path(c["image"]).exists():
                import cv2

                img = cv2.imread(c["image"])
                if img is not None:
                    blocks.append(image_block(to_jpeg_b64(img, 1024)))
                    blocks.append({"type": "text", "text": f"(Clip R{c['round_no']}: {c['note']})"})
        return blocks

    async def answer(self, question: str, lang: str | None = None) -> str | None:
        self._last_talk = time.time()
        lang = lang or self.lang
        nq = _norm(question)
        caps = bool(CAPS_RE.search(nq))
        app_help = bool(HELP_RE.search(nq))
        live_now = self.tracker.state.active and self.mode == "live"
        explain = bool(EXPLAIN_RE.search(nq)) and not live_now
        tactic = bool(TACTIC_Q_RE.search(nq)) and not live_now
        words = 160 if explain else 140 if caps or app_help else 110 if tactic else self.cfg.get_path("llm.max_answer_words", 35)
        smalltalk = hasattr(self, "game_focus") and not self.game_focus() and not (tactic or caps or explain or app_help)
        extra = ("\n\n" + P.CAPABILITIES.format(name=self.name)) if caps or app_help else ""
        if explain:
            extra += P.EXPLAIN
        if smalltalk:  # niemand spielt: normaler Gesprächspartner, kurzer Prompt = schnellere Antwort
            words = max(int(words), 45)
            system = self._sys(P.SMALLTALK)
        else:
            system = self._sys(P.LIVE_ANSWER, extra=extra, max_words=words)
        long = caps or app_help or explain
        system += ("\n\nThis is read aloud in voice chat: " + ("a complete but natural spoken explanation, 5-10 sentences. "
                   if long else "1-3 short sentences. ") + "Plain words only. "
                   "No emojis, no (haha)/*actions*, no lists, no markdown. Answer in the language of the question.")
        content: list = []
        from ..vision.capture import to_jpeg_b64

        if self.feeds and self.feeds.active and self.tracker.state.active:
            from ..vision.feeds import composite

            grid = composite(self.feeds.frames())
            if grid is not None:
                content.append(image_block(to_jpeg_b64(grid, 1280)))
        elif self.last_frame is not None and self.tracker.state.active:
            content.append(image_block(to_jpeg_b64(self.last_frame, int(self.cfg.get_path("capture.vision_width", 1280)))))
        ctx = self._smalltalk_context(question) if smalltalk else self._live_context()
        content.append({"type": "text", "text": f"{ctx}\n\nANSWER IN: {lang}\nPLAYER SAYS: {question}"})
        log.info("Frage erkannt: „%s“ – frage die KI …", question)
        t0 = time.time()
        spoken: list[str] = []

        async def filler() -> None:  # dauert die KI länger, kurz Bescheid geben (wie ein Mensch: „Okay, …“)
            await asyncio.sleep(float(self.cfg.get_path("llm.filler_after_s", 1.6)))
            if not spoken:
                import random

                self.say(random.choice(["Okay.", "Mhm.", "Gute Frage."] if lang == "de" else ["Okay.", "Right.", "Good question."]),
                         priority=2, max_age=4, lang=lang)

        f = asyncio.create_task(filler())
        reply = None
        try:
            if hasattr(self.llm, "ask_stream"):
                # Satz für Satz vorlesen, sobald er fertig ist – statt auf die ganze Antwort zu warten
                buf = ""
                async for piece in self.llm.ask_stream(system, content):
                    if getattr(getattr(self, "voice", None), "interrupted_at", 0) > t0:
                        log.info("Antwort abgebrochen – wurde unterbrochen")
                        break
                    buf += piece
                    sentences, buf = split_sentences(buf)
                    if not spoken and not sentences:
                        first, buf = split_first_chunk(buf)
                        sentences = [first] if first else []
                    for sent in (clean_for_speech(x) for x in sentences):
                        if not sent:
                            continue
                        if not spoken:
                            log.info("Erster Satz nach %.1fs", time.time() - t0)
                        spoken.append(sent)
                        self.say(sent, priority=2, max_age=60, lang=lang)
                rest = clean_for_speech(buf) if getattr(getattr(self, "voice", None), "interrupted_at", 0) <= t0 else ""
                if rest:
                    spoken.append(rest)
                    self.say(rest, priority=2, max_age=60, lang=lang)
                reply = " ".join(spoken).strip() or None
                if not reply:  # z. B. nur „Nachdenken“ ohne Antwort – dann normal (ohne Stream) fragen
                    log.info("Stream ohne Text – frage nochmal ohne Stream …")
                    reply = clean_for_speech(await self.llm.ask(system, content, max_tokens=1500) or "") or None
                    if not reply:  # letzter Versuch: kurz und ohne Bild
                        reply = clean_for_speech(await self.llm.ask(
                            system + "\nAnswer in 1-2 short sentences.", f"ANSWER IN: {lang}\nPLAYER ASKS: {question}",
                            max_tokens=300) or "") or None
                    if reply:
                        self.say(reply, priority=2, max_age=60, lang=lang)
            else:
                reply = clean_for_speech(await self.llm.ask(system, content) or "") or None
                if reply:
                    self.say(reply, priority=2, max_age=60, lang=lang)
        except Exception as e:
            log.error("KI-Fehler bei der Antwort: %s", e)
            reply = reply or (" ".join(spoken) or None)
        finally:
            f.cancel()
        if not reply:
            log.warning("Keine Antwort von der KI nach %.1fs (läuft Ollama? Modell geladen?)", time.time() - t0)
            self.say(t("thinking_error", lang), priority=2, lang=lang)
            return None
        log.info("Antwort komplett nach %.1fs: %s", time.time() - t0, reply)
        if hasattr(self, "remember_talk"):
            self.remember_talk(question, reply, lang)
        return reply

    async def comms_check(self) -> None:
        comms = self._recent_comms(300)
        if not comms:
            return
        reply = await self.llm.ask(self._sys(P.COMMS_CHECK), f"ANSWER IN: {self.lang}. Max 35 words, spoken.\n\nCOMMS:\n{comms}")
        if reply:
            self.say(reply, priority=2, max_age=20)

    # ----------------------------------------------------------------- events
    def _can_call(self, priority: int) -> bool:
        gap = float(self.cfg.get_path("callouts.min_gap_s", 6))
        if priority >= 3:
            gap /= 2
        return self.cfg.get_path("callouts.enabled", True) and time.time() - self.last_call_ts >= gap

    def _call(self, text: str, priority: int = 3, max_age: float = 5.0) -> None:
        if self.mode in ("observe", "cast"):
            return  # Zuschau-Modus: still, Feedback erst am Ende
        if self._can_call(priority):
            self.last_call_ts = time.time()
            self.say(text, priority=priority, max_age=max_age)

    def _timer_call(self, text: str, priority: int = 3) -> None:
        """Timer-Calls kommen pünktlich (ohne Mindestabstand), außer im Zuschau-Modus."""
        if self.mode in ("observe", "cast") or not self.cfg.get_path("timers.enabled", True):
            return
        self.last_call_ts = time.time()
        self.say(text, priority=priority, max_age=3)

    def _tendency_tick(self, now: float | None = None) -> None:
        now = now or time.time()
        if now - self._last_tend_check < 4 or not self.tracker.state.active:
            return
        self._last_tend_check = now
        if not self.cfg.get_path("callouts.tendencies", True):
            return
        hit = self.tendencies.check(bool(self.tracker.mode_cfg().get("round_based")), now)
        if hit:
            from .tactics import tendency_call

            log.info("Gegner-Tendenz: %s (%s)", *hit)
            self._call(tendency_call(hit[0], hit[1], self.lang), priority=2, max_age=10)

    def _strats_context(self, map_name: str = "", mode: str = "") -> str:
        try:
            from .tactics import strats_context

            return strats_context(self.team.dir / "strats", map_name or "", mode or "", zone_name=self._zone_name)
        except Exception as e:
            log.debug("Strats: %s", e)
            return ""

    def _zone_name(self, z: str) -> str:
        for c in self.callout_index().items:
            if (c.get("zone") or "").upper() == z.upper():
                return c["name"]
        return ""

    def _comms_tactics(self, text: str) -> None:
        st = self.tracker.state
        if not st.active:
            return
        self.timers.on_comms(text, st.mode)
        from .callouts import parse_call

        parsed = parse_call(text, self.callout_index())
        if parsed["where"] and (parsed["count"] or parsed["status"] or parsed["move"]):
            for w in parsed["where"]:
                self.tendencies.add(w.split(" [")[0])
        self._tendency_tick()

    async def handle_events(self, events: list[GameEvent]) -> None:
        if self.mode == "cast":
            if getattr(self, "caster", None) is not None:
                self.caster.on_game_events(events)
            return
        if self.mode == "talk":
            return  # Konversations-Modus: keine Match-Erkennung, keine Calls – erst „Coach, Match-Modus“
        rules = self.cfg.get_path("callouts.rule_calls", True)
        for e in events:
            self.recent_events.append(e)
            self.recent_events = self.recent_events[-50:]
            self.db.add_event(self.match_id, e.kind, e.data)
            lang = self.lang
            if e.kind == "hill_new":
                if self.cfg.get_path("timers.hill_from_hud", True):
                    self.timers.hill_sync(float(e.data["seconds"]))
            elif e.kind == "hill_rotate" and rules:
                self._call(t("hill_rotate", lang, s=e.data["seconds"]))
            elif e.kind == "time_low" and rules:
                self._call(t("time_low", lang, s=e.data["seconds"]))
            elif e.kind == "score_swing" and rules:
                self.swings.append(e.ts)
                self.swings = [x for x in self.swings if e.ts - x < 240]
                if len(self.swings) >= 2 and self.mode == "live":
                    self.swings.clear()
                    self.timeout_reset(lang)
                else:
                    self._call(t("score_swing", lang, n=e.data["points"]))
            elif e.kind == "match_point" and rules:
                self._call(t("match_point_us" if e.data["side"] == "us" else "match_point_them", lang), priority=2)
            elif e.kind == "round_end":
                self.timers.cancel("bomb")
                self.tendencies.new_round(e.data["round"] + 1)
                self.loss_streak = 0 if e.data["won"] else self.loss_streak + 1
                if self.loss_streak >= 3 and self.mode == "live":
                    self.loss_streak = 0
                    self.timeout_reset(lang)
                rid = self.db.add_round(self.match_id, e.data["round"], e.data["won"])
                self._spawn(self.review_round(round_id=rid, won=e.data["won"], round_no=e.data["round"]))
            elif e.kind == "match_end":
                self._spawn(self.end_match(e.data["ours"], e.data["enemy"]))

    # ----------------------------------------------------------------- seeing
    async def hud_loop(self) -> None:
        if not (self.capture and self.hud):
            return
        delay = 1.0 / max(0.2, float(self.cfg.get_path("capture.hud_fps", 2)))
        while True:
            if getattr(self, "gaming", False):  # Gaming-Modus: HUD höchstens 1×/s lesen
                await asyncio.sleep(max(0.0, 1.0 - delay))
            try:
                frame = await asyncio.to_thread(self.capture.grab)
                self.last_frame = frame
                if self.tracker.state.active:
                    if self.minimap.roi:
                        markers = await asyncio.to_thread(self.minimap.update, frame)
                        if markers:
                            from ..vision.minimap import zone

                            zs = [zone(m.x, m.y) for m in markers if m.team == "enemy"]
                            self.tendencies.add_minimap([self._zone_name(z) or z for z in zs])
                        self._tendency_tick()
                    reading = await asyncio.to_thread(self.hud.read, frame)
                    events = self.tracker.update(reading)
                    if events:
                        await self.handle_events(events)
            except Exception as e:
                log.warning("HUD-Loop: %s", e)
            await asyncio.sleep(delay)

    # ------------------------------------------------------------- zuschauen
    WATCH_ON_RE = re.compile(r"\b(schau (mal )?(zu|mit|auf meinen bildschirm|meinen bildschirm an|hin)|guck (mal )?(zu|mit)|"
                             r"watch (my screen|along|this)|look at my screen|zuschauen an)\b")
    WATCH_OFF_RE = re.compile(r"\b(hör auf zu(zu)?schauen|hoer auf zu(zu)?schauen|nicht mehr zuschauen|zuschauen aus|"
                              r"stop watching)\b")

    TWITCH_WATCH_RE = re.compile(r"\b(?:schau|guck|watch)\w*\b.{0,30}?\btwitch\b\s+(?:von\s+|bei\s+|of\s+)?(?P<ch>[a-z0-9_]{3,25})\b"
                                 r"|\b(?:schau|guck|watch)\w*\s+(?:bei\s+)?(?P<ch2>[a-z0-9_]{3,25})\s+(?:auf|on)\s+twitch\b")

    def watch_twitch(self, channel: str, player: str | None = None, announce: bool = True) -> dict:
        """Zuschauen über den Twitch-Stream eines Spielers – geht auch, wenn Yvara AI auf einem anderen Gerät (MacBook)
        läuft. Discord-Bildschirmübertragungen kann ein Bot nicht sehen, Twitch schon (2–5 s Verzögerung)."""
        from ..vision.feeds import FeedManager, FeedSpec

        ch = re.sub(r"[^A-Za-z0-9_]", "", str(channel or ""))[:25]
        if not ch:
            return self.watch
        if self.feeds is None:
            self.feeds = FeedManager()
        specs = [sp for sp in self.feeds.specs if not (sp.kind == "twitch" and str(sp.ref).lower() == ch.lower())]
        specs.append(FeedSpec(player or ch, "twitch", ch))
        try:
            self.feeds.set(specs)
        except Exception as e:
            log.warning("Twitch-Zuschauen: %s", e)
        self.watch.update(on=True, source=f"twitch:{ch}", since=time.time())
        self.watch_log.clear()
        log.info("Zuschauen über Twitch: %s", ch)
        if announce:
            self.say(f"Okay, ich schaue bei {ch} auf Twitch zu. Das hat ein paar Sekunden Verzögerung."
                     if self.lang == "de" else f"Okay, watching {ch} on Twitch. There's a few seconds of delay.",
                     priority=2)
        return self.watch

    def _watch_frame(self):
        """Bild zum Zuschauen: eigener Bildschirm oder (ohne Bildschirm, z. B. MacBook) Twitch-/Stream-Feeds."""
        src = str(self.watch.get("source") or "")
        if self.feeds and self.feeds.active and (src.startswith(("twitch:", "stream:")) or self.last_frame is None):
            from ..vision.feeds import composite

            frames = self.feeds.frames()
            if src.startswith("twitch:"):
                f = self.feeds.frame(next((sp.player for sp in self.feeds.specs if str(sp.ref).lower() == src[7:].lower()), ""))
                if f is not None:
                    return f
            live = {k: v for k, v in frames.items() if v is not None}
            if len(live) == 1:
                return next(iter(live.values()))
            return composite(live) if live else None
        return self.last_frame

    def set_watch(self, on: bool, source: str | None = None, announce: bool = True) -> dict:
        """Bildschirm zeigen: Yvara AI schaut live mit (eigenes Spiel, Discord-Stream eines Mates, Caster-Ansicht)."""
        if source is not None and self.capture is not None:
            kind, _, ref = source.partition(":")
            if kind == "window" and ref:
                self.capture.mode, self.capture.window_title, self.capture.region = "window", ref, None
            elif kind == "monitor":
                self.capture.mode, self.capture.monitor, self.capture.region = "monitor", int(ref or 1), None
            self.watch["source"] = source
        was = self.watch["on"]
        self.watch["on"] = bool(on) and (self.capture is not None or bool(self.feeds and self.feeds.active))
        if self.watch["on"] and not was:
            self.watch["since"] = time.time()
            self.watch_log.clear()
        log.info("Zuschauen %s (%s)", "an" if self.watch["on"] else "aus", self.watch.get("source") or "Bildschirm")
        if announce and on != was:
            lang = self.lang
            if on and not self.watch["on"]:
                self.say("Ich kann gerade keinen Bildschirm sehen." if lang == "de" else "I can't see a screen right now.",
                         priority=2, lang=lang)
            else:
                self.say(("Okay, ich schaue zu." if on else "Okay, ich schaue nicht mehr zu.") if lang == "de"
                         else ("Okay, I'm watching." if on else "Okay, stopped watching."), priority=2, lang=lang)
        return self.watch

    def _watch_context(self) -> str:
        if not self.watch_log:
            return ""
        now = time.time()
        lines = [f"- vor {int((now - ts) // 60)} min: {o}" if now - ts >= 60 else f"- gerade eben: {o}"
                 for ts, o in self.watch_log[-6:]]
        return "WHAT YOU SAW ON THE SHARED SCREEN (latest last):\n" + "\n".join(lines)

    async def spectate_frame(self, frame) -> dict | None:
        from ..vision.capture import to_jpeg_b64

        b64 = await asyncio.to_thread(to_jpeg_b64, frame, int(self.cfg.get_path("capture.vision_width", 1280)))
        text = "\n\n".join(x for x in [self._watch_context(), f"RECENT COMMS:\n{self._recent_comms(60)}",
                                        f"OUR PLAYERS: {', '.join(p['ingame'] for p in self.roster())}",
                                        f"WRITE IN: {self.lang}"] if x)
        res = await self.llm.ask(self._sys(P.SPECTATE), [image_block(b64), {"type": "text", "text": text}],
                                 schema=P.SPECTATE_SCHEMA, max_tokens=300)
        if not isinstance(res, dict):
            return None
        obs = (res.get("observation") or "").strip()
        if obs:
            self.watch_log.append((time.time(), obs))
            del self.watch_log[:-30]
            self.watch["last"] = obs
        comment = (res.get("comment") or "").strip()
        if comment and self.watch.get("comment") and self.mode != "observe":
            self._call(comment, priority=1, max_age=8)
        return res

    async def vision_loop(self) -> None:
        while True:
            await asyncio.sleep(float(self.cfg.get_path("capture.vision_interval_s", 20)))
            frame = self._watch_frame() if self.watch["on"] and not self.tracker.state.active else None
            if frame is not None:
                try:
                    await self.spectate_frame(frame)
                except Exception as e:
                    log.warning("Zuschauen: %s", e)
                continue
            if self.mode in ("talk", "cast") or (not self.tracker.state.active and not (self.mode == "teach" and self.lesson)):
                continue
            if getattr(self, "gaming", False) and not self.cfg.get_path("performance.game_vision", False):
                continue  # Gaming-Modus: keine KI-Bildanalyse, die Grafikkarte gehört dem Spiel
            try:
                if self.feeds and self.feeds.active:
                    await self.analyze_multi()
                elif self.capture and self.last_frame is not None:
                    await self.analyze_frame(self.last_frame)
            except Exception as e:
                log.warning("Vision-Analyse: %s", e)

    async def analyze_multi(self) -> dict | None:
        """Alle Spieler-Feeds als Kachelbild ansehen, selbst entscheiden, auf wen fokussiert wird."""
        from ..vision.capture import to_jpeg_b64
        from ..vision.feeds import composite

        frames = await asyncio.to_thread(self.feeds.frames)
        grid = await asyncio.to_thread(composite, frames)
        if grid is None:
            return None
        images = [image_block(await asyncio.to_thread(to_jpeg_b64, grid, 1280, 80))]
        focus_frame = self.feeds.frame(self.focus_player) if self.focus_player else None
        if focus_frame is not None:
            images.append(image_block(await asyncio.to_thread(to_jpeg_b64, focus_frame, 1280, 80)))
        recent_obs = self.db.observations(self.match_id)[-6:] if self.match_id else []
        text = (f"{self._live_context()}\n\nFEEDS: {', '.join(frames)}\nCURRENT FOCUS: {self.focus_player or 'whole team'}"
                f"\nRECENT OBSERVATIONS: " + "; ".join(o["text"] for o in recent_obs) + f"\nWRITE call/observation IN: {self.lang}")
        system = self._sys(P.VISION) + P.MULTI + self._teach_addendum()
        res = await self.llm.ask(system, images + [{"type": "text", "text": text}], schema=P.MULTI_SCHEMA)
        if not res:
            return None
        st = self.tracker.state
        if res.get("observation"):
            self.db.add_observation(self.match_id, st.round_no, res["observation"], res.get("enemy_tendency", ""),
                                    res.get("player_feedback", []))
        new_focus = (res.get("focus_player") or "").strip()
        if self.cfg.get_path("feeds_auto_focus", True) and new_focus != (self.focus_player or ""):
            if not new_focus or new_focus in frames:
                self.set_focus(new_focus or None)
                log.info("Auto-Fokus: %s (%s)", new_focus or "Team", res.get("focus_reason", ""))
        call = (res.get("call") or "").strip()
        if call and self.cfg.get_path("callouts.vision_calls", True):
            self._call(call, priority=max(1, min(3, int(res.get("priority", 2)))), max_age=8)
        return res

    async def analyze_frame(self, frame) -> dict | None:
        from ..vision.capture import to_jpeg_b64

        st = self.tracker.state
        recent_obs = self.db.observations(self.match_id)[-6:] if self.match_id else []
        text = (
            f"{self._live_context()}\n\nRECENT OBSERVATIONS: "
            + "; ".join(o["text"] for o in recent_obs)
            + f"\n\nTEAM PLAYERS (ours): {', '.join(p['ingame'] for p in self.roster())}"
            + f"\nWRITE call/observation IN: {self.lang}"
        )
        b64 = await asyncio.to_thread(to_jpeg_b64, frame, int(self.cfg.get_path("capture.vision_width", 1280)))
        images = [image_block(b64)]
        if self.minimap.last_crop is not None and self.minimap.roi:
            # Minimap zusätzlich groß -> Positionen/Setups besser lesbar
            import cv2

            mm = cv2.resize(self.minimap.last_crop, (512, 512), interpolation=cv2.INTER_CUBIC)
            images.append(image_block(await asyncio.to_thread(to_jpeg_b64, mm, 512, 90)))
            text += "\n(Second image = enlarged minimap.)"
        system = self._sys(P.VISION) + self._teach_addendum()
        if self.focus_player:
            role = next((p.get("role", "?") for p in self.roster() if p["ingame"].lower() == self.focus_player.lower()), "?")
            system += P.FOCUS.format(player=self.focus_player, role=role)
        res = await self.llm.ask(system, images + [{"type": "text", "text": text}], schema=P.VISION_SCHEMA)
        if not res:
            return None
        if res.get("observation"):
            self.db.add_observation(self.match_id, st.round_no, res["observation"], res.get("enemy_tendency", ""),
                                    res.get("player_feedback", []))
        call = (res.get("call") or "").strip()
        if call and self.cfg.get_path("callouts.vision_calls", True):
            self._call(call, priority=max(1, min(3, int(res.get("priority", 2)))), max_age=6)
        return res

    # ---------------------------------------------------------------- reviews
    def _match_material(self, match_id: int, since: float = 0) -> str:
        m = self.db.match(match_id) or {}
        obs = self.db.observations(match_id)
        obs = [o for o in obs if o["ts"] >= since]
        ev = [e for e in self.db.events(match_id) if e["ts"] >= since]
        comms = self.db.comms(match_id, since=since)
        notes = self.db.notes(match_id)
        rounds = self.db.rounds(match_id)
        return "\n\n".join([
            f"MATCH: {json.dumps({k: m.get(k) for k in ('mode', 'map', 'opponent', 'score_ours', 'score_enemy', 'won')}, ensure_ascii=False)}",
            self._map_context(m.get("mode") or "hardpoint", m.get("map")),
            self._library_context(m.get("mode") or "hardpoint", m.get("map") or ""),
            f"PLAN: {m.get('plan') or '-'}",
            "OBSERVATIONS:\n" + "\n".join(f"- (R{o['round_no']}) {o['text']} | enemy: {o['enemy_tendency']} | feedback: {o['feedback']}" for o in obs[-120:]),
            "EVENTS:\n" + "\n".join(f"- {e['kind']} {e['data']}" for e in ev[-200:]),
            "ROUND REVIEWS:\n" + "\n".join(f"- R{r['round_no']} {'W' if r['won'] else 'L'}: {r['review']}" for r in rounds if r["review"]),
            "COMMS (⟨…⟩ = your automatic reading of the call):\n" + self._comms_lines(comms[-300:], m.get("map")),
            "COACH NOTES:\n" + "\n".join(f"- {n['author']}: {n['text']}" for n in notes),
            "CLIPS (marked moments):\n" + "\n".join(f"- R{c['round_no']} {c['note']}" for c in self.db.clips(match_id)),
            self._profiles_context(),
        ])

    async def review_round(self, round_id: int | None = None, won: bool | None = None,
                           round_no: int | None = None, force: bool = False) -> dict | None:
        if not self.match_id:
            return None
        since = self.round_started
        self.round_started = time.time()
        st = self.tracker.state
        header = ""
        if won is not None:
            header = t("round_won" if won else "round_lost", self.lang, ours=st.score_ours, enemy=st.score_enemy) + " "
        material = self._match_material(self.match_id, since=since) + "\n\n" + self.minimap.round_summary(since)
        res = await self.llm.ask(self._sys(P.ROUND_REVIEW), f"WRITE IN: {self.lang}\nROUND: {round_no or st.round_no}\n\n{material}",
                                 schema=P.ROUND_SCHEMA, max_tokens=6000)
        quiet = self.mode == "observe" and not force  # Zuschau-Modus: sammeln, nicht sprechen
        if not res:
            if header and not quiet:
                self.say(header.strip(), priority=2)
            return None
        if round_id:
            self.db.set_round_review(round_id, res)
        if not quiet:
            self.say(header + res.get("spoken", ""), priority=2, max_age=25)
        return res

    async def review_match(self, match_id: int) -> dict | None:
        material = self._match_material(match_id)
        heat_path = None
        self.team.reports.mkdir(parents=True, exist_ok=True)
        try:
            heat_path = self.minimap.heatmap(self.team.reports / f"match_{match_id}_heatmap.png")
        except Exception as e:
            log.warning("Heatmap fehlgeschlagen: %s", e)
        content = self._clip_images(match_id)
        if heat_path:
            import cv2

            from ..vision.capture import to_jpeg_b64

            content += [image_block(to_jpeg_b64(cv2.imread(heat_path), 500)),
                        {"type": "text", "text": "(Minimap heatmap of the match: blue = us, red = enemy.)"}]
        m = self.db.match(match_id) or {}
        content.append({"type": "text", "text": f"WRITE IN: {self.lang}\n\n{material}\n\n"
                        + self.minimap.round_summary(m.get("started") or 0)[-6000:]})
        res = await self.llm.ask(self._sys(P.MATCH_REVIEW), content, deep=True, schema=P.MATCH_SCHEMA)
        if not res:
            return None
        self.db.add_review(match_id, "match", res)
        for pl in res.get("players", []):
            if pl.get("player") and pl.get("rating") is not None:
                self.db.add_rating(match_id, pl["player"], float(pl["rating"]), pl["player"] == res.get("mvp"))
        self.team.reports.mkdir(parents=True, exist_ok=True)
        path = self.team.reports / f"match_{match_id}_review.md"
        path.write_text(res.get("report_markdown", ""), encoding="utf-8")
        for co in res.get("callouts_learned", []) or []:
            if m.get("map") and co.get("name"):
                self.add_callout(m["map"], co["name"], co.get("zone", ""), note=co.get("note", ""), source="learned")
        self.say(res.get("spoken", ""), priority=2, max_age=60)
        await self.notify(res.get("report_markdown", "")[:1800], path)
        if heat_path:
            await self.notify("🗺️ Minimap-Heatmap (blau = wir, rot = Gegner)", Path(heat_path))
        return res

    # ---------------------------------------------------------------- lernen
    async def _update_profile(self, kind: str, name: str, evidence: dict) -> None:
        old = self.db.get_profile(kind, name)
        system = self._sys(P.PROFILE_UPDATE, kind=kind)
        content = (f"WRITE IN: {self.lang}\n{kind.upper()}: {name}\nEXISTING PROFILE: {json.dumps(old, ensure_ascii=False) or '{}'}\n"
                   f"NEW EVIDENCE FROM LATEST MATCH: {json.dumps(evidence, ensure_ascii=False)}")
        new = await self.llm.ask(system, content, deep=True, schema=P.PROFILE_SCHEMA, max_tokens=8000)
        if new:
            new["matches_seen"] = int(old.get("matches_seen", 0)) + 1
            self.db.set_profile(kind, name, new)

    async def learn_from_match(self, match_id: int) -> None:
        """Nach jedem Match: Spieler-, Team- und Gegnerprofile aktualisieren."""
        reviews = [json.loads(r["data"]) for r in self.db._rows(
            "SELECT data FROM reviews WHERE match_id=? AND kind='match'", (match_id,))]
        if not reviews:
            return
        rev = reviews[-1]
        m = self.db.match(match_id) or {}
        obs = self.db.observations(match_id)
        tasks = []
        for p in self.roster():
            name = p["ingame"]
            fb = [f for o in obs for f in json.loads(o["feedback"] or "[]") if f.get("player", "").lower() == name.lower()]
            player_rev = next((x for x in rev.get("players", []) if x.get("player", "").lower() == name.lower()), {})
            if fb or player_rev:
                tasks.append(self._update_profile("player", name, {"role": p.get("role"), "review": player_rev, "live_feedback": fb[-40:],
                                                                   "match": {k: m.get(k) for k in ("mode", "map", "won")}}))
        tasks.append(self._update_profile("team", self.team.name, {
            "good": rev.get("team_good"), "bad": rev.get("team_bad"), "focus": rev.get("training_focus"),
            "match": {k: m.get(k) for k in ("mode", "map", "opponent", "score_ours", "score_enemy", "won")}}))
        if m.get("opponent"):
            tasks.append(self._update_profile("opponent", m["opponent"], {
                "tendencies": rev.get("enemy_tendencies"),
                "live_reads": [o["enemy_tendency"] for o in obs if o["enemy_tendency"]][-40:],
                "match": {k: m.get(k) for k in ("mode", "map", "score_ours", "score_enemy", "won")}}))
        await asyncio.gather(*tasks)
        log.info("Profile nach Match %s aktualisiert", match_id)

    # ---------------------------------------------------------------- planen
    async def make_plan(self, mode: str, map_name: str, opponent: str = "") -> str | None:
        mode = norm_mode(mode)
        history = self.db.matches_vs(opponent) if opponent else []
        content = "\n\n".join([
            f"WRITE IN: {self.lang}",
            f"UPCOMING: mode={mode}, map={map_name}, opponent={opponent or 'unknown'}",
            self._map_context(mode, map_name),
            self._library_context(mode, map_name, opponent),
            f"ROSTER: {json.dumps(self.roster(), ensure_ascii=False)}",
            self._profiles_context(),
            f"OPPONENT PROFILE: {json.dumps(self.db.get_profile('opponent', opponent), ensure_ascii=False) if opponent else '-'}",
            "HISTORY VS OPPONENT: " + json.dumps([{k: h[k] for k in ('mode', 'map', 'score_ours', 'score_enemy', 'won')} for h in history], ensure_ascii=False),
        ])
        res = await self.llm.ask(self._sys(P.PLAN), content, deep=True, schema=P.PLAN_SCHEMA)
        if not res:
            return None
        self.plan_text = res.get("plan_markdown", "")
        self.team.reports.mkdir(parents=True, exist_ok=True)
        safe = re.sub(r"[^\w-]", "_", f"plan_{mode}_{map_name}_{opponent}")
        path = self.team.reports / f"{safe}.md"
        path.write_text(self.plan_text, encoding="utf-8")
        self.say(res.get("spoken", ""), priority=2, max_age=60)
        await self.notify(self.plan_text[:1800], path)
        return self.plan_text

    # ----------------------------------------------------------------- stats
    def stats_text(self) -> str:
        s = self.db.stats()
        tot = s["total"]
        lines = [f"**Gesamt:** {tot['wins']}/{tot['played']} Siege"]
        for title, key in (("Modus", "by_mode"), ("Map", "by_map"), ("Gegner", "by_opponent")):
            rows = s[key][:8]
            if rows:
                lines.append(f"\n**{title}:**")
                lines += [f"- {r['key'] or '?'}: {r['wins'] or 0}/{r['played']} (Ø {r['avg_ours']}:{r['avg_enemy']})" for r in rows]
        rt = self.ratings_text()
        if rt:
            lines.append("\n" + rt)
        return "\n".join(lines)

    def ratings_text(self) -> str:
        rows = self.db.player_ratings()
        if not rows:
            return ""
        return "**Spieler-Ratings:**\n" + "\n".join(
            f"- {r['player']}: Ø {r['avg_rating']} (letzte 5: {r['last5']}) · {r['mvps'] or 0}× MVP · {r['matches']} Matches" for r in rows)

    async def training_plan(self) -> str | None:
        reviews = [json.loads(r["data"]) for r in self.db._rows("SELECT data FROM reviews WHERE kind='match' ORDER BY id DESC LIMIT 5")]
        pbs = self.library.playbooks() if self.library else {}
        content = "\n\n".join([
            f"WRITE IN: {self.lang}",
            f"ROSTER: {json.dumps(self.roster(), ensure_ascii=False)}",
            self._profiles_context(),
            self.ratings_text(),
            "RECENT REVIEWS: " + json.dumps([{k: r.get(k) for k in ("team_good", "team_bad", "training_focus", "players")} for r in reviews], ensure_ascii=False),
            "LEARNED PLAYBOOKS: " + json.dumps({m: {k: pb.get(k) for k in ("principles", "mistakes_to_avoid", "drills")} for m, pb in pbs.items()}, ensure_ascii=False),
            "MEMORY:\n" + "\n".join(f"- {m}" for m in self.db.relevant_memories("training drill ziel goal", 25)),
        ])
        plan = await self.llm.ask(self._sys(P.TRAINING), content, deep=True, max_tokens=16000)
        if plan:
            self.team.reports.mkdir(parents=True, exist_ok=True)
            path = self.team.reports / f"training_{time.strftime('%Y-%m-%d')}.md"
            path.write_text(plan, encoding="utf-8")
            await self.notify(plan[:1800], path)
        return plan

    def _web_ok(self) -> bool:
        """Websuche nur, wenn privacy.allow_web_search ausdrücklich an ist (sonst gehen Suchbegriffe raus)."""
        from ..safety import allowed

        return allowed(self.cfg, "web_search")

    async def scout(self, opponent: str) -> str | None:
        history = self.db.matches_vs(opponent)
        ctx = "\n".join([
            f"OPPONENT PROFILE: {json.dumps(self.db.get_profile('opponent', opponent), ensure_ascii=False)}",
            "OUR MATCHES VS THEM: " + json.dumps([{k: h[k] for k in ("mode", "map", "score_ours", "score_enemy", "won")} for h in history], ensure_ascii=False),
            "MEMORY:\n" + "\n".join(f"- {m}" for m in self.db.relevant_memories(opponent, 20)),
        ])
        report = await self.llm.chat(self._sys(P.SCOUT, context=ctx),
                                     [{"role": "user", "content": f"(write in {self.lang}) Scouting report: {opponent}"}],
                                     web_search=self._web_ok())
        if report:
            self.team.reports.mkdir(parents=True, exist_ok=True)
            path = self.team.reports / f"scouting_{re.sub(r'[^\w-]', '_', opponent)}.md"
            path.write_text(report, encoding="utf-8")
            self.db.add_memory(f"Scouting-Report zu {opponent} erstellt ({time.strftime('%d.%m.%Y')}), liegt unter reports/{path.name}", "scout")
            await self.notify(report[:1800], path)
        return report

    def profile_text(self, kind: str, name: str) -> str:
        p = self.db.get_profile(kind, name)
        if not p:
            return f"Noch kein Profil für {name}. Nach dem ersten Match lerne ich dazu."
        sec = lambda title, key: f"**{title}:**\n" + "\n".join(f"- {x}" for x in p.get(key, [])) if p.get(key) else ""
        parts = [f"## {name} ({p.get('matches_seen', 0)} Matches)", p.get("summary", ""),
                 sec("Stärken", "strengths"), sec("Schwächen", "weaknesses"), sec("Gewohnheiten", "tendencies"),
                 sec("Fokus", "focus"), sec("Fortschritt", "progress")]
        return "\n\n".join(x for x in parts if x)

    # ------------------------------------------------------------------ chat
    async def chat(self, session_id: int, text: str, web: bool | None = None, source: str = "chat") -> str:
        """Chat in der eigenen Oberfläche: Verlauf + Gedächtnis + optional Online-Recherche."""
        self._last_talk = time.time()
        lang = norm_lang(self._guess_lang(text), self.lang)
        db = self.db
        links = re.findall(r"https?://(?:www\.)?(?:youtube\.com|youtu\.be|twitch\.tv|m\.twitch\.tv)/\S+", text)
        queued = 0
        if links and self.autolearn:
            queued = self.autolearn.add_links(links)
            if not self.autolearn.active and self.cfg.get_path("autolearn.start_on_link", True):
                self.autolearn.start()
        history = db.chat(session_id, limit=30)
        db.add_chat(session_id, "user", text, lang)
        if len(history) == 0 and source == "chat":
            db.rename_session(session_id, text[:60])
        context = "\n\n".join(x for x in [
            f"GAME STATE: {self.tracker.state.summary() if self.tracker.state.active else 'no match running'}",
            f"FOCUS PLAYER: {self.focus_player}" if self.focus_player else "",
            f"ROSTER: {json.dumps(self.roster(), ensure_ascii=False)}",
            self._profiles_context(),
            "RELEVANT MEMORY:\n" + "\n".join(f"- {m}" for m in db.relevant_memories(text, 30)),
            "TEAM STATS:\n" + self.stats_text(),
            self._chat_library(text),
            self._watch_context(),
            self._strats_context(self.tracker.state.map, self.tracker.state.mode),
        ] if x)
        nq = _norm(text)
        extra = ("\n\n" + P.CAPABILITIES.format(name=self.name)) if HELP_RE.search(nq) or CAPS_RE.search(nq) else ""
        if EXPLAIN_RE.search(nq):
            extra += P.EXPLAIN.replace("Spoken style, no lists.", "Here in text chat short headings or a few bullets are fine.")
        system = self._sys(P.CHAT, extra=extra, context=context)
        msgs = [{"role": m["role"], "content": m["text"]} for m in history if m["role"] in ("user", "assistant") and m["text"]]
        # Rollen müssen abwechseln
        merged: list[dict] = []
        note = (f" (System: {queued} video link(s) were added to your study queue; you will watch and learn from them "
                f"in the background. Confirm briefly.)") if queued else ""
        for m in msgs + [{"role": "user", "content": f"(reply in {lang}) {text}{note}"}]:
            if merged and merged[-1]["role"] == m["role"]:
                merged[-1]["content"] += "\n" + m["content"]
            else:
                merged.append(dict(m))
        while merged and merged[0]["role"] != "user":
            merged.pop(0)

        async def handle_tool(name: str, data: dict) -> str:
            if name == "save_memory" and data.get("fact"):
                db.add_memory(data["fact"], source)
                return "saved"
            if name == "save_callout" and data.get("name"):
                ok = self.add_callout(data.get("map", ""), data["name"], data.get("zone", ""),
                                      data.get("aliases") or [], data.get("note", ""), "manual")
                return "saved" if ok else "no map given"
            return "unknown tool"

        # Internet nur mit Erlaubnis: Schalter „Online-Recherche“ oder ausdrücklicher Wunsch in dieser Nachricht
        use_web = bool(self.cfg.get_path("web.research", False)) if web is None else bool(web)
        use_web = use_web and self._web_ok()
        reply = await self.llm.chat(system, merged, web_search=bool(use_web), client_tools=[P.SAVE_MEMORY_TOOL, P.SAVE_CALLOUT_TOOL],
                                    tool_handler=handle_tool)
        if not reply:
            reply = t("thinking_error", lang)
        db.add_chat(session_id, "assistant", reply, lang)
        self.last_lang = lang
        self.remember_talk(text, reply, lang)
        return reply

    # ------------------------------------------------------- aus Gesprächen lernen
    TEACH_RE = re.compile(r"\b(merk dir|merke dir|vergiss nicht|du musst wissen|ab jetzt|in zukunft|wir spielen|unsere? "
                          r"(strat|taktik|setup|rolle)|mein main|ich spiele|ich bin|er ist|sie ist|remember|from now on|"
                          r"we (always|usually) |our (strat|setup)|nenn |heißt bei uns|sagen wir|spielt|spielen|immer|niemals|"
                          r"sollst|musst|lieber|always|never|should)\b", re.I)

    def remember_talk(self, said: str, reply: str, lang: str) -> None:
        """Was im Gespräch gesagt oder beigebracht wurde, dauerhaft merken – ohne dass man „merk dir“ sagen muss."""
        if not self.cfg.get_path("memory.learn_from_talk", True) or not said or len(said.split()) < 3:
            return
        buf = self.__dict__.setdefault("_talk_buf", [])
        buf.append((said, reply or ""))
        del buf[:-12]
        first = self.__dict__.setdefault("_talk_first", time.time())
        if self.TEACH_RE.search(said) or len(buf) >= 6 or time.time() - first > 300:
            items = list(buf)
            buf.clear()
            self._talk_first = time.time()
            self._spawn(self._extract_memories(items, lang))

    async def _extract_memories(self, items: list[tuple[str, str]], lang: str) -> int:
        convo = "\n".join(f"PLAYER: {a}\nCOACH: {b}" for a, b in items)
        known = "\n".join(f"- {m['text']}" for m in self.db.memories(60))
        try:
            res = await self.llm.ask(self._sys(P.MEMORY_EXTRACT),
                                     f"KNOWN ALREADY:\n{known or '-'}\n\nCONVERSATION:\n{convo}\n\nWRITE IN: {lang}",
                                     schema=P.MEMORY_EXTRACT_SCHEMA, max_tokens=500)
        except Exception as e:
            log.debug("Gedächtnis aus Gespräch: %s", e)
            return 0
        n = 0
        for f in (res or {}).get("facts", []) if isinstance(res, dict) else []:
            fact = str(f.get("fact") if isinstance(f, dict) else f).strip()
            if 8 <= len(fact) <= 300:
                self.db.add_memory(fact, "gespräch")
                n += 1
        if n:
            log.info("Aus dem Gespräch gemerkt: %d neue Erinnerung(en)", n)
        return n

    def _chat_library(self, text: str) -> str:
        if not self.library:
            return ""
        from .knowledge import MODE_ALIASES

        norm = _norm(text)
        modes = {v for k, v in MODE_ALIASES.items() if re.search(rf"\b{re.escape(k)}\b", norm)} or {self.tracker.state.mode}
        return "\n".join(self._library_context(m, self.tracker.state.map, text) for m in modes)

    @staticmethod
    def _guess_lang(text: str) -> str:
        de = {"der", "die", "das", "und", "ist", "nicht", "wie", "was", "ich", "wir", "du", "mit", "auf", "für", "warum", "kannst", "bitte"}
        en = {"the", "and", "is", "not", "how", "what", "we", "you", "with", "on", "for", "why", "can", "please", "should"}
        words = set(_norm(text).split())
        d, e = len(words & de), len(words & en)
        if d == e:
            return ""
        return "de" if d > e else "en"
