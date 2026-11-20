"""Discord-Bot: Slash-Commands, Voice-Channel beitreten, Reports posten."""
from __future__ import annotations

import asyncio

import logging
from pathlib import Path

import discord
from discord import app_commands
from discord.ext import commands

from .i18n import t

log = logging.getLogger(__name__)

MODE_CHOICES = [
    app_commands.Choice(name="Hardpoint", value="hardpoint"),
    app_commands.Choice(name="Search & Destroy", value="snd"),
    app_commands.Choice(name="Control", value="control"),
]


def check_dave_lib() -> str | None:
    """davey 0.2 braucht prepare/execute_transition, das discord.py 2.7.1 nicht aufruft -> Bot spricht ins Leere."""
    try:
        from importlib.metadata import version

        v = version("davey")
    except Exception:
        return None
    major_minor = tuple(int(x) for x in v.split(".")[:2] if x.isdigit())
    if major_minor >= (0, 2):
        log.error("davey %s passt nicht zu discord.py %s – Yvara AI wäre im Voice-Channel stumm. Beim nächsten Start "
                  "wird automatisch davey 0.1.7 installiert (oder: pip install \"davey<0.2\").", v, discord.__version__)
    return v


class CoachBot(commands.Bot):
    def __init__(self, cfg, player) -> None:
        intents = discord.Intents.default()
        intents.voice_states = True
        intents.guilds = True
        intents.messages = True
        # Für @Yvara AI im Textchat: im Developer-Portal "Message Content Intent" aktivieren
        intents.message_content = bool(cfg.get_path("discord.text_chat", True))
        super().__init__(command_prefix="!coach ", intents=intents)
        self.cfg = cfg
        self.player = player  # DiscordPlayer
        self.coach = None     # wird von app.py gesetzt
        self.report_channel: discord.abc.Messageable | None = None
        self._synced = False

    async def setup_hook(self) -> None:
        for grp in build_groups(self):
            self.tree.add_command(grp)
        if self.coach is not None:
            self.coach.join_voice = self.join_speaker_channel
            self.coach.leave_voice = self.leave_voice

    async def on_ready(self) -> None:
        log.info("Discord verbunden als %s", self.user)
        check_dave_lib()
        if not self._synced:
            self._synced = True
            await self.sync_commands()

    async def on_guild_join(self, guild) -> None:
        await self.sync_guild(guild)

    def invite_url(self) -> str:
        perms = discord.Permissions(view_channel=True, send_messages=True, attach_files=True, read_message_history=True,
                                    connect=True, speak=True, use_application_commands=True)
        return discord.utils.oauth_url(self.application_id or self.user.id, permissions=perms,
                                       scopes=("bot", "applications.commands"))

    async def sync_guild(self, guild) -> int:
        """Slash-Befehle direkt auf einem Server anmelden (sofort sichtbar, global dauert es bis zu 1 h)."""
        try:
            self.tree.copy_global_to(guild=guild)
            cmds = await self.tree.sync(guild=guild)
            log.info("%d Befehle registriert auf Server „%s“ (/coach, /match, /learn)", len(cmds), guild.name)
            return len(cmds)
        except discord.Forbidden:
            log.error("Server „%s“: Yvara AI darf dort keine Slash-Befehle anlegen. Bot mit diesem Link neu einladen "
                      "(Häkchen bei applications.commands ist drin): %s", guild.name, self.invite_url())
        except Exception as e:
            log.error("Server „%s“: Slash-Befehle konnten nicht registriert werden: %s", guild.name, e)
        return 0

    async def sync_commands(self) -> None:
        gid = self.cfg.get_path("discord.guild_id")
        guilds = [g for g in self.guilds if not gid or str(g.id) == str(gid)]
        if not guilds:
            log.warning("Yvara AI ist noch auf keinem Server. Einladen mit: %s", self.invite_url())
            return
        ok = 0
        for g in guilds:
            ok += bool(await self.sync_guild(g))
        if ok:
            try:  # alte globale Befehle entfernen, sonst stehen sie doppelt da
                self.tree.clear_commands(guild=None)
                await self.tree.sync()
            except Exception as e:
                log.debug("Globale Befehle nicht bereinigt: %s", e)
            log.info("Tipp: Tauchen die Befehle nicht auf, Discord einmal mit Strg+R neu laden.")

    def _owner_keys(self) -> list[str]:
        """Wer sitzt am Yvara-AI-PC? discord.follow_user (Name/ID) und die zuletzt per /coach join gemerkte Person."""
        keys = [str(self.cfg.get_path("discord.follow_user") or "").lower().strip(),
                str(self.cfg.get_path("discord.owner_id") or "").strip()]
        return [k for k in keys if k]

    @staticmethod
    def _is_person(m, keys: list[str]) -> bool:
        return any(k in (m.name.lower(), str(m.id), (m.display_name or "").lower()) for k in keys)

    def _speaker_channel(self):
        """Voice-Channel der Person am Yvara-AI-PC – immer LIVE aus dem aktuellen Discord-Status (nie ein alter Channel).
        Reihenfolge: bekannte Person (follow_user / owner_id) -> Server-Besitzer -> einziger/vollster Channel."""
        chans = [c for g in self.guilds for c in g.voice_channels if any(not m.bot for m in c.members)]
        keys = self._owner_keys()
        if keys:
            for c in chans:
                if any(self._is_person(m, keys) for m in c.members if not m.bot):
                    return c
        for c in chans:  # Server-Besitzer (meist die Person am Yvara-AI-PC)
            owner = getattr(getattr(c, "guild", None), "owner_id", None)
            if owner and any(m.id == owner for m in c.members):
                return c
        if not chans:
            return None
        if keys:
            log.info("Deine Person (%s) ist in keinem Voice-Channel – nehme den vollsten", ", ".join(keys))
        return max(chans, key=lambda c: sum(not m.bot for m in c.members))

    def remember_owner(self, member) -> None:
        """Wer /coach join benutzt, ist (meist) die Person am PC – merken, damit „Coach join“ per Sprache ihr folgt."""
        try:
            from .config import save_user_value

            if str(self.cfg.get_path("discord.owner_id") or "") != str(member.id) and not self.cfg.get_path("discord.follow_user"):
                save_user_value(self.cfg, "discord.owner_id", str(member.id))
                log.info("Merke mir %s als Person am Yvara-AI-PC (für „Coach, komm rein“)", member.display_name)
        except Exception as e:
            log.debug("Besitzer nicht gemerkt: %s", e)

    async def join_speaker_channel(self) -> bool:
        ch = self._speaker_channel()
        if not ch:
            log.info("Sprachbefehl „komm rein“: niemand in einem Voice-Channel gefunden")
            return False
        vc = ch.guild.voice_client
        if vc and vc.channel == ch:
            log.info("Sprachbefehl „komm rein“: bin schon in „%s“", ch.name)
            return True
        await self.join_channel(ch)
        log.info("Per Sprachbefehl in „%s“ beigetreten (live ermittelt)", ch.name)
        return True

    async def leave_voice(self) -> bool:
        left = False
        if hasattr(self.player, "rejoin"):
            self.player.rejoin = None  # sonst verbindet sich Yvara AI beim nächsten Satz wieder neu
        for g in self.guilds:
            if g.voice_client:
                await g.voice_client.disconnect()
                left = True
        self.player.vc = None
        return left

    async def join_channel(self, channel: discord.VoiceChannel, greet: bool = True) -> None:
        vc = channel.guild.voice_client
        if vc:
            if vc.channel == channel:
                return
            await vc.move_to(channel)
        else:
            # nicht taub stellen: sonst nimmt Discord den Bot teils nicht richtig in die E2EE-Gruppe auf
            vc = await channel.connect(self_deaf=bool(self.cfg.get_path("discord.self_deaf", False)))
        self.player.vc = vc
        if hasattr(self.player, "rejoin"):
            self.player.rejoin = lambda ch=channel: self._rejoin(ch)
        self._check_voice_rights(channel)
        if greet and self.coach:
            self.coach.say(t("hello", self.coach.lang, name=self.coach.name), priority=2, max_age=15)

    async def _rejoin(self, channel) -> None:
        # Verbindung weg: nicht stur in den alten Channel, sondern dahin, wo die Person JETZT ist
        live = self._speaker_channel() if self._owner_keys() else None
        if live is not None and live.guild == channel.guild:
            channel = live
            if hasattr(self.player, "rejoin"):
                self.player.rejoin = lambda ch=channel: self._rejoin(ch)
        vc = channel.guild.voice_client
        if vc:
            await vc.disconnect(force=True)
        await asyncio.sleep(1.0)
        self.player.vc = await channel.connect(self_deaf=False)

    def _check_voice_rights(self, channel) -> list[str]:
        """Warnen, wenn Yvara AI im Channel nicht sprechen darf – dann hört man sie nicht, obwohl sie „spricht“."""
        problems = []
        try:
            me = channel.guild.me
            perms = channel.permissions_for(me)
            if not perms.speak:
                problems.append("keine Berechtigung „Sprechen“ in diesem Channel")
            vs = me.voice
            if vs and (vs.mute or vs.suppress):
                problems.append("Yvara AI ist auf dem Server stummgeschaltet (rotes Mikro neben ihrem Namen)")
        except Exception as e:
            log.debug("Rechte-Check: %s", e)
        for p in problems:
            log.error("Voice „%s“: %s – Server-Einstellungen → Rollen bzw. Channel-Berechtigungen prüfen.", channel.name, p)
        return problems

    async def on_voice_state_update(self, member: discord.Member, before, after) -> None:
        """Auto-Join: Yvara AI kommt in Team-Channels (discord.auto_join_channels), sobald jemand reingeht,
        und geht, wenn keiner mehr da ist. Oder sie folgt einer Person (discord.follow_user)."""
        if member.bot:
            return
        try:
            if await self._auto_join(member, before, after):
                return
        except Exception as e:
            log.warning("Auto-Join fehlgeschlagen: %s", e)
        follow = str(self.cfg.get_path("discord.follow_user") or "").lower()
        if not follow or member.bot or follow not in (member.name.lower(), str(member.id), member.display_name.lower()):
            return
        try:
            if after.channel and after.channel != before.channel:
                await self.join_channel(after.channel)
            elif before.channel and not after.channel and member.guild.voice_client:
                await member.guild.voice_client.disconnect()
                self.player.vc = None
        except Exception as e:
            log.warning("Auto-Join fehlgeschlagen: %s", e)

    def _is_team_channel(self, ch) -> bool:
        wanted = [str(x).lower() for x in (self.cfg.get_path("discord.auto_join_channels") or [])]
        return bool(ch) and (ch.name.lower() in wanted or str(ch.id) in wanted)

    async def _auto_join(self, member, before, after) -> bool:
        if not self.cfg.get_path("discord.auto_join_channels"):
            return False
        vc = member.guild.voice_client
        if after.channel and after.channel != before.channel and self._is_team_channel(after.channel):
            if not vc or not vc.is_connected():
                await self.join_channel(after.channel)
            return True
        if before.channel and vc and vc.channel == before.channel and self._is_team_channel(before.channel):
            if not any(not m.bot for m in before.channel.members):
                await vc.disconnect()
                self.player.vc = None
            return True
        return False

    async def on_message(self, message: discord.Message) -> None:
        """@Yvara AI im Textchat -> Antwort mit Gedächtnis (pro Kanal ein Chatverlauf)."""
        if message.author.bot or not self.user or self.user not in message.mentions or not self.coach:
            return
        text = message.content.replace(f"<@{self.user.id}>", "").replace(f"<@!{self.user.id}>", "").strip()
        if not text:
            return
        async with message.channel.typing():
            db = self.coach.db
            key = f"discord-{message.channel.id}"
            sess = next((s["id"] for s in db.sessions() if s["title"] == key), None) or db.new_session(key)
            # nur auf ausdrücklichen Wunsch ins Internet („recherchier mal…“, „google mal…“, „search online…“)
            research = any(w in text.lower() for w in ("recherch", "google", "im internet", "online nach", "search online",
                                                       "look it up", "such im netz", "such online"))
            reply = await self.coach.chat(sess, f"[{message.author.display_name}] {text}", web=research, source="discord")
        for i in range(0, len(reply), 1900):
            await message.reply(reply[i : i + 1900], mention_author=False)

    async def notify(self, text: str, path: Path | None = None) -> None:
        if not self.cfg.get_path("privacy.discord_reports", False):
            log.info("🔒 Report bleibt lokal (privacy.discord_reports: false): %s", path or "")
            return
        ch = self.report_channel or self._find_report_channel()
        if not ch:
            log.info("Kein Report-Kanal – Report liegt unter %s", path)
            return
        content = (text or "📄")[:1900]
        if path and Path(path).exists():
            await ch.send(content, file=discord.File(str(path)))
        else:
            await ch.send(content)

    def _find_report_channel(self):
        name = self.cfg.get_path("discord.report_channel", "coach-reports")
        for g in self.guilds:
            for c in g.text_channels:
                if c.name == name:
                    self.report_channel = c
                    return c
        return None


def build_groups(bot: CoachBot) -> list[app_commands.Group]:
    """Discord erlaubt höchstens 25 Unterbefehle pro Gruppe – deshalb drei Gruppen:
    /coach (Allgemein), /match (Match & Review), /learn (Lernen & Fortbildung)."""
    g = app_commands.Group(name="coach", description="Yvara AI – KI-Coach / AI coach")
    m = app_commands.Group(name="match", description="Yvara AI – Match, Score & Review")
    l = app_commands.Group(name="learn", description="Yvara AI – Lernen, Fortbildung & Playbook")  # noqa: E741

    async def _ack(inter: discord.Interaction, text: str, ephemeral: bool = False) -> None:
        if inter.response.is_done():
            await inter.followup.send(text[:1900], ephemeral=ephemeral)
        else:
            await inter.response.send_message(text[:1900], ephemeral=ephemeral)

    @g.command(name="join", description="Coach kommt in deinen Voice-Channel")
    async def join(inter: discord.Interaction):
        member = inter.user if isinstance(inter.user, discord.Member) else None
        if not member or not member.voice or not member.voice.channel:
            return await _ack(inter, "Geh zuerst in einen Voice-Channel. / Join a voice channel first.", True)
        ch = member.voice.channel
        await inter.response.defer()
        bot.remember_owner(member)
        bot.report_channel = inter.channel
        await bot.join_channel(ch)
        await _ack(inter, f"🎧 {bot.coach.name} ist in **{ch.name}**.")

    @g.command(name="watch", description="Yvara AI schaut über Twitch zu (geht auch, wenn Yvara AI auf dem Laptop läuft)")
    @app_commands.describe(kanal="Twitch-Kanal des Spielers, z. B. yvaraesports", spieler="optional: Name des Spielers")
    async def watch(inter: discord.Interaction, kanal: str, spieler: str = ""):
        c = bot.coach
        await inter.response.defer()
        st = c.watch_twitch(kanal, spieler or None, announce=bool(bot.player and bot.player.ready))
        await _ack(inter, f"👀 {c.name} schaut bei **{st.get('source', '')[7:]}** auf Twitch zu "
                          "(ein paar Sekunden Verzögerung). Fragen einfach im Voice stellen – `/coach unwatch` beendet es.")

    @g.command(name="unwatch", description="Yvara AI hört auf zuzuschauen")
    async def unwatch(inter: discord.Interaction):
        bot.coach.set_watch(False, announce=False)
        await _ack(inter, "🙈 Zuschauen beendet.")

    @g.command(name="leave", description="Coach verlässt den Voice-Channel")
    async def leave(inter: discord.Interaction):
        vc = inter.guild.voice_client
        if vc:
            await vc.disconnect()
        bot.player.vc = None
        await _ack(inter, "👋")

    @m.command(name="heatmap", description="Heatmap einer Map posten: blau = wir, rot = Gegner, gelb = Kampfzonen")
    async def heatmap(inter: discord.Interaction, map: str = ""):
        import io

        c = bot.coach
        name = map or c.tracker.state.map or next((h["map"] for h in c.mapheat.maps()), "")
        png = await asyncio.to_thread(c.mapheat.render, name) if name else None
        if not png:
            await _ack(inter, "Für diese Map gibt es noch keine Heatmap (Minimap einrichten und ein Match spielen).", True)
            return
        await inter.response.send_message(f"🔥 Heatmap **{name}** – blau = wir, rot = Gegner, gelb = Kampfzonen",
                                          file=discord.File(io.BytesIO(png), filename="heatmap.png"))

    @m.command(name="strat", description="Gespeicherte Strat vom Strat-Board posten (Name oder Teil davon)")
    async def strat(inter: discord.Interaction, name: str):
        import json

        d = bot.coach.team.dir / "strats"
        found = None
        for f in sorted(d.glob("*.json"), key=lambda p: -p.stat().st_mtime) if d.exists() else []:
            s = json.loads(f.read_text(encoding="utf-8"))
            if name.lower() in s.get("name", "").lower():
                found = (s, f.with_suffix(".png"))
                break
        if not found:
            await _ack(inter, "Keine Strat mit dem Namen gefunden – in der App unter Taktik → Strat-Board zeichnen.", True)
            return
        s, png = found
        text = f"🗺️ **{s['name']}**" + (f" – {s['map']}" if s.get("map") else "") + (f"\n{s['notes']}" if s.get("notes") else "")
        if png.exists():
            await inter.response.send_message(text[:1900], file=discord.File(str(png)))
        else:
            await _ack(inter, text)

    @m.command(name="timer", description="Countdown mit Ansagen, z. B. 30 Sekunden")
    async def timer(inter: discord.Interaction, sekunden: app_commands.Range[int, 3, 1800]):
        bot.coach.timers.custom(float(sekunden))
        await _ack(inter, f"⏱️ Timer {sekunden} s läuft.")

    @m.command(name="start", description="Match starten (Tracking, Calls, Reviews)")
    @app_commands.choices(mode=MODE_CHOICES)
    async def start(inter: discord.Interaction, mode: app_commands.Choice[str], map: str = "", opponent: str = ""):
        await bot.coach.start_match(mode.value, map, opponent)
        await _ack(inter, f"▶️ Match gestartet: **{mode.name}** {map} vs {opponent or '?'}")

    @m.command(name="end", description="Match beenden -> Review + Lernen")
    async def end(inter: discord.Interaction, our_score: int | None = None, enemy_score: int | None = None):
        await inter.response.defer(thinking=True)
        await bot.coach.end_match(our_score, enemy_score)
        await _ack(inter, "✅ Match gespeichert, Review oben im Kanal. Profile wurden aktualisiert.")

    @m.command(name="score", description="Score manuell korrigieren")
    async def score(inter: discord.Interaction, ours: int, enemy: int):
        st = bot.coach.tracker.state
        st.score_ours, st.score_enemy = ours, enemy
        await _ack(inter, f"Score: {ours} : {enemy}", True)

    @m.command(name="plan", description="Strategieplan für ein Match erstellen")
    @app_commands.choices(mode=MODE_CHOICES)
    async def plan(inter: discord.Interaction, mode: app_commands.Choice[str], map: str, opponent: str = ""):
        await inter.response.defer(thinking=True)
        res = await bot.coach.make_plan(mode.value, map, opponent)
        await _ack(inter, "📋 Plan ist im Kanal." if res else "Plan konnte nicht erstellt werden (API-Key?).")

    @m.command(name="review", description="Review der letzten Runde jetzt")
    async def review(inter: discord.Interaction):
        await inter.response.defer(thinking=True)
        res = await bot.coach.review_round(force=True)
        if not res:
            return await _ack(inter, "Kein laufendes Match oder keine Daten.")
        txt = "**Gut:**\n" + "\n".join(f"- {x}" for x in res.get("good", [])) + \
              "\n**Schlecht:**\n" + "\n".join(f"- {x}" for x in res.get("bad", [])) + \
              f"\n**Nächste Runde:** {res.get('next_round', '')}\n**Gegner:** {res.get('enemy_read', '')}"
        await _ack(inter, txt)

    @g.command(name="ask", description="Frag den Coach (Text)")
    async def ask(inter: discord.Interaction, question: str):
        await inter.response.defer(thinking=True)
        reply = await bot.coach.answer(question)
        await _ack(inter, reply or "Keine Antwort (API-Key / Verbindung prüfen).")

    @g.command(name="stats", description="Team-Statistiken")
    async def stats(inter: discord.Interaction):
        await _ack(inter, bot.coach.stats_text())

    @g.command(name="profile", description="Was der Coach über einen Spieler / Gegner gelernt hat")
    @app_commands.choices(kind=[app_commands.Choice(name="Spieler", value="player"),
                                app_commands.Choice(name="Team", value="team"),
                                app_commands.Choice(name="Gegner", value="opponent")])
    async def profile(inter: discord.Interaction, kind: app_commands.Choice[str], name: str):
        await _ack(inter, bot.coach.profile_text(kind.value, name))

    @m.command(name="note", description="Notiz für das Review speichern")
    async def note(inter: discord.Interaction, text: str):
        bot.coach.db.add_note(bot.coach.match_id, inter.user.display_name, text)
        await _ack(inter, "📝 Notiert.", True)

    @g.command(name="callouts", description="Live-Calls an/aus")
    async def callouts(inter: discord.Interaction, on: bool):
        bot.cfg.set_path("callouts.enabled", on)
        bot.coach.voice.enabled = on
        await _ack(inter, f"Calls {'an' if on else 'aus'}.", True)

    @g.command(name="language", description="Sprache: auto / de / en")
    @app_commands.choices(lang=[app_commands.Choice(name=n, value=n) for n in ("auto", "de", "en")])
    async def language(inter: discord.Interaction, lang: app_commands.Choice[str]):
        bot.cfg["language"] = lang.value
        await _ack(inter, f"Sprache: {lang.value}", True)

    @g.command(name="team", description="Welches Team coacht Yvara AI? (z. B. Yvara Main, Yvara Z8)")
    async def team(inter: discord.Interaction, name: str):
        c = bot.coach
        if not c.teams.get(name):
            c.teams.create(name)
        ok = c.set_team(name)
        await _ack(inter, f"🎯 Coache jetzt **{c.team.name}**." if ok else "Während eines Matches kein Teamwechsel.")

    @team.autocomplete("name")
    async def team_auto(_inter: discord.Interaction, current: str):
        return [app_commands.Choice(name=t["name"], value=t["slug"])
                for t in bot.coach.teams.list() if current.lower() in t["name"].lower()][:25]

    @g.command(name="focus", description="Einzelnen Spieler gezielt coachen (leer = ganzes Team)")
    async def focus(inter: discord.Interaction, player: str = ""):
        bot.coach.set_focus(player or None)
        await _ack(inter, f"👁 Fokus auf **{player}**." if player else "Fokus: ganzes Team.")

    @g.command(name="mode", description="Coaching-Modus: live (Calls), observe (still, Feedback am Ende), teach (Training)")
    @app_commands.choices(mode=[app_commands.Choice(name="Konversation (Smalltalk)", value="talk"),
                                app_commands.Choice(name="Live-Coaching (Calls)", value="live"),
                                app_commands.Choice(name="Nur zuschauen, Feedback am Ende", value="observe"),
                                app_commands.Choice(name="Training / Private Match", value="teach")])
    async def mode(inter: discord.Interaction, mode: app_commands.Choice[str]):
        bot.coach.set_mode(mode.value)
        await _ack(inter, f"🎛 Modus: **{mode.name}**")

    @g.command(name="stil", description="Persönlichkeit: normal, toxisch aus Spaß (Roasten) oder charmant/flirty")
    @app_commands.choices(stil=[app_commands.Choice(name="Normal", value="normal"),
                                app_commands.Choice(name="Toxisch aus Spaß (Roast)", value="toxic"),
                                app_commands.Choice(name="Charmant / flirty", value="flirty")])
    async def stil(inter: discord.Interaction, stil: app_commands.Choice[str]):
        bot.coach.set_vibe(stil.value)
        await _ack(inter, f"🎭 Stil: **{stil.name}**")

    @g.command(name="callout", description="Callout merken, z. B. map=Karte name='Top Bridge' zone=C2 aliases='brücke oben'")
    async def callout(inter: discord.Interaction, map: str, name: str, zone: str = "", aliases: str = ""):
        ok = bot.coach.add_callout(map, name, zone, [a.strip() for a in aliases.split(",") if a.strip()])
        await _ack(inter, f"📍 **{name}** auf {map}" + (f" ({zone.upper()})" if zone else "") + " gemerkt." if ok
                   else "Konnte den Callout nicht speichern.")

    @g.command(name="teach", description="Training starten: Yvara AI erklärt ein Thema Schritt für Schritt")
    async def teach(inter: discord.Interaction, topic: str):
        await inter.response.defer(thinking=True)
        res = await bot.coach.start_lesson(topic)
        if not res:
            await _ack(inter, "Konnte keine Lektion erstellen.")
            return
        steps = "\n".join(f"{i + 1}. {s['say']}" for i, s in enumerate(res["steps"]))
        await _ack(inter, f"🏋️ **{res.get('title', topic)}**\n{steps}\n\nWeiter mit `/coach next` oder sagt „Yvara AI, weiter“.")

    @g.command(name="next", description="Training: nächster Schritt")
    async def next_step(inter: discord.Interaction):
        bot.coach.lesson_next()
        await _ack(inter, "➡️ Nächster Schritt." if bot.coach.lesson else "Training beendet.")

    @g.command(name="chat", description="Längere Frage an Yvara AI (mit Gedächtnis & Recherche)")
    async def chat(inter: discord.Interaction, message: str, research: bool = False):
        await inter.response.defer(thinking=True)
        db = bot.coach.db
        key = f"discord-{inter.channel_id}"
        sess = next((s["id"] for s in db.sessions() if s["title"] == key), None) or db.new_session(key)
        reply = await bot.coach.chat(sess, message, web=research, source="discord")
        await _ack(inter, reply)

    @l.command(name="video", description="Lern-Modus: Video-Link, Datei oder 'screen' (Stream auf dem Bildschirm)")
    @app_commands.choices(mode=MODE_CHOICES + [app_commands.Choice(name="Hijack", value="hijack")])
    async def study(inter: discord.Interaction, source: str, mode: app_commands.Choice[str] | None = None,
                    title: str = "", own_team: bool = False):
        try:
            bot.coach.studier.start(source, title, mode.value if mode else "", own_team=own_team)
        except RuntimeError as e:
            return await _ack(inter, f"⚠️ {e}", True)
        await _ack(inter, f"📚 {bot.coach.name} lernt jetzt von: {title or source}"
                          + (" (Analyse unseres Teams)" if own_team else ""))

    @l.command(name="start", description="Fortbildungsmodus: Yvara AI lernt selbstständig, bis du stoppst")
    async def learn(inter: discord.Interaction, topics: str = "", links: str = ""):
        al = bot.coach.autolearn
        al.start(topics, links.split())
        await _ack(inter, f"🎓 Fortbildung läuft{' – Fokus: ' + topics if topics else ''}. Stoppen mit /learn stop.")

    @l.command(name="stop", description="Fortbildungsmodus stoppen")
    async def learn_stop(inter: discord.Interaction):
        bot.coach.autolearn.stop()
        await _ack(inter, "⏹ Fortbildung wird gestoppt.")

    @l.command(name="queue", description="Video-/Stream-Links zum Lernen in die Warteschlange")
    async def queue(inter: discord.Interaction, links: str):
        n = bot.coach.autolearn.add_links(links.split())
        await _ack(inter, f"📥 {n} Link(s) in der Warteschlange. Läuft die Fortbildung, schaut Yvara AI sie als Nächstes.")

    @l.command(name="ideas", description="Eigene Taktiken, die Yvara AI für euer Team entwickelt hat")
    async def ideas(inter: discord.Interaction):
        rows = bot.coach.library.ideas(bot.coach.team.name, 8)
        if not rows:
            return await _ack(inter, "Noch keine – starte /learn start.")
        await _ack(inter, "\n".join(f"**{r['name']}** ({r['mode']}, {r['map']}): {r['data'].get('concept', '')[:180]}" for r in rows))

    @l.command(name="video_stop", description="Lern-Modus beenden")
    async def study_stop(inter: discord.Interaction):
        bot.coach.studier.stop()
        await _ack(inter, "⏹ Lern-Session wird beendet, Playbook wird aktualisiert.")

    @l.command(name="playbook", description="Was Yvara AI aus Pro-Matches gelernt hat (pro Modus)")
    @app_commands.choices(mode=MODE_CHOICES + [app_commands.Choice(name="Hijack", value="hijack")])
    async def playbook(inter: discord.Interaction, mode: app_commands.Choice[str]):
        pb = bot.coach.library.playbook(mode.value)
        if not pb:
            return await _ack(inter, "Noch kein Playbook – starte /learn video mit Pro-Matches.")
        parts = [f"## Playbook {mode.name}", pb.get("summary", "")]
        for key, title in (("principles", "Prinzipien"), ("setups", "Setups"), ("rotations", "Rotationen"),
                           ("mistakes_to_avoid", "Fehler vermeiden"), ("drills", "Trainings-Drills")):
            if pb.get(key):
                parts.append(f"**{title}:**\n" + "\n".join(f"- {x}" for x in pb[key][:6]))
        await _ack(inter, "\n\n".join(parts))

    @g.command(name="training", description="Wochen-Trainingsplan aus den Schwächen des Teams")
    async def training(inter: discord.Interaction):
        await inter.response.defer(thinking=True)
        plan = await bot.coach.training_plan()
        await _ack(inter, "🏋️ Trainingsplan ist im Kanal." if plan else "Konnte keinen Plan erstellen.")

    @g.command(name="scout", description="Scouting-Report über ein Gegnerteam (mit Online-Recherche)")
    async def scout(inter: discord.Interaction, opponent: str):
        await inter.response.defer(thinking=True)
        rep = await bot.coach.scout(opponent)
        await _ack(inter, "🔎 Scouting-Report ist im Kanal." if rep else "Scouting fehlgeschlagen.")

    @m.command(name="clip", description="Aktuellen Moment für das Review markieren")
    async def clip(inter: discord.Interaction, note: str = ""):
        bot.coach.save_clip(note or f"von {inter.user.display_name}")
        await _ack(inter, "🎬 Geclippt.", True)

    @m.command(name="timeout", description="Reset-Call / Timeout-Ansage")
    async def timeout(inter: discord.Interaction):
        bot.coach.timeout_reset()
        await _ack(inter, "⏸", True)

    @g.command(name="update", description="Yvara AI auf die neueste Version updaten (startet neu)")
    async def update(inter: discord.Interaction):
        import asyncio as _a

        from . import updater

        if bot.coach.tracker.state.active:
            return await _ack(inter, "Erst das Match beenden.", True)
        await inter.response.defer(thinking=True)
        try:
            res = await _a.to_thread(updater.update, bot.cfg)
        except Exception as e:
            return await _ack(inter, f"⚠️ Update fehlgeschlagen: {e}")
        if not res.get("updated"):
            return await _ack(inter, "✅ Schon auf dem neuesten Stand.")
        await _ack(inter, f"⬆️ Update installiert: {res.get('message', '')} – ich bin gleich wieder da.")
        _a.get_running_loop().call_later(2, updater.restart_now)

    @g.command(name="say", description="Coach sagt einen Text im Voice")
    async def say(inter: discord.Interaction, text: str):
        bot.coach.say(text, priority=2, max_age=20)
        await _ack(inter, "🔊", True)

    return [g, m, l]
