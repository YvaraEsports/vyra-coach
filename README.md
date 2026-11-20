# Yvara AI – KI-Coach für competitive Call of Duty

Yvara AI läuft nativ auf deinem PC oder Mac, coacht euer Team live über Discord, schaut dem Spiel zu, liest HUD und Minimap,
hört die Comms, gibt Calls, macht Reviews, lernt jeden Spieler kennen und wird mit jedem Match, Scrim, Stream und VOD besser.
Sie spricht Deutsch und Englisch, ist locker drauf, wenn Zeit dafür ist, und absolut ernst, wenn's zählt.

> Yvara AI analysiert nur Bild und Ton und gibt Ratschläge. Kein Speicherzugriff, keine Eingriffe ins Spiel, keine Aim-Hilfen.

---

## Was Yvara AI kann

| Bereich | Funktionen |
|---|---|
| **Live-Coaching** | Hört auf „Yvara AI“ / „Coach“ (oder Push-to-Talk), beantwortet Fragen im Voice, gibt kurze Live-Calls (Hill-Rotation, Zeit, Score-Swing, Matchpoint), Timeout-/Reset-Calls bei Tilt, ruhigere Comms bei Hektik |
| **Spiel sehen** | Bildschirm- oder Fensteraufnahme, liest Score/Timer per OCR, schaut regelmäßig per KI aufs Bild, **Minimap-Tracking** (eigene Sicht oder Caster-Ansicht mit allen 8 Spielern: Positionen, Bewegung, Setups, Heatmaps) |
| **Fokus-Modus** | Schaut gezielt einem Spieler zu und coacht ihn einzeln („Yvara AI, schau auf Player2“) |
| **Multi-View** | Mehrere Bildquellen gleichzeitig (Monitore, Discord-Übertragung, Twitch-Livestreams der Spieler), Auto-Fokus auf den Spieler, der gerade Hilfe braucht |
| **Reviews** | Kurz-Review nach jeder Runde, großes Match-Review mit Gut/Schlecht/Besser, Note 1–10 pro Spieler, MVP, Gegner-Analyse, Clips |
| **Lernen** | Profile pro Spieler, Team und Gegner, die nach jedem Match aktualisiert werden (Stärken, Schwächen, Gewohnheiten, Fortschritt) |
| **Fortbildung** | Lernt stundenlang selbstständig, bis du stoppst: sucht Videos auf YouTube und CDL-VODs auf Twitch, schaut deine geschickten Links zuerst und entwickelt daraus **eigene Taktiken** für euer Team |
| **Lern-Modus** | Schaut CDL-Matches, Twitch-/Discord-Streams, YouTube-VODs, Highlights und eure Scrims, schreibt Lektionen in eine **Taktik-Bibliothek** und baut daraus **Playbooks** pro Modus. Eigene Liga-Streams analysiert sie als „unser Team“ und speichert die Fehler im Team-Gedächtnis |
| **Planen** | Gameplan pro Map/Modus/Gegner, Wochen-Trainingsplan mit Drills, Scouting-Report über Gegner (mit Online-Recherche) |
| **Chat-App** | Eigene Oberfläche im Browser: Chatten (Text oder Stimme), Chatverläufe, Langzeitgedächtnis, Team-Projekte, Lern-Modus, Einstellungen |
| **Teams** | Getrennte Projekte, z. B. „Yvara Main“ und „Yvara Z8“, jeweils mit eigenem Gedächtnis, Chats, Profilen, Stats und Reports |
| **Auto-Update** | Holt neue Versionen selbst von GitHub, eure Daten bleiben erhalten |
| **Stimme** | Lokale Stimme (Piper, offline, eigene Stimme möglich) oder Online-Stimme (Edge), Ausgabe im Discord-Voice oder lokal |

---

## So funktioniert es (wichtig zu wissen)

```
  Dein PC / Mac
  ┌──────────────────────────────────────────────────────────────┐
  │  Bildschirm (Spiel, Twitch, Discord-Übertragung) ─► Yvara AI sieht │
  │  Mikrofon + PC-Ton (Discord-Comms)                ─► Yvara AI hört │
  │  Yvaras Gehirn (Claude) + lokale Daten (data/)                 │
  │  Chat-App: http://127.0.0.1:8765                              │
  └───────────────┬──────────────────────────────────────────────┘
                  │ Discord-Bot spricht im Voice-Channel
                  ▼
           Euer Discord-Voice-Channel
```

- **Warum hört Yvara AI über den PC und nicht direkt über Discord?** Discord verschlüsselt Sprache inzwischen Ende-zu-Ende (DAVE-Protokoll).
  Bots können deshalb aktuell nicht zuverlässig mithören (alle Bibliotheken melden das als kaputt). Dein Discord-Client spielt die
  Comms aber ohnehin auf deinem PC ab, und genau diesen Ton nimmt Yvara AI ab („Loopback“). **Sprechen** kann der Bot im Voice-Channel ganz normal.
- **Warum schaut Yvara AI über den Bildschirm?** Ein Discord-Bot kann keine Go-Live-Übertragung anschauen. Deshalb nimmt Yvara AI den Bildschirm
  des PCs auf, auf dem sie läuft. Du hast drei Möglichkeiten:
  1. Yvara AI läuft auf dem PC eines Spielers und schaut sein Spiel.
  2. Yvara AI läuft auf einem Coach-PC oder Laptop, dort öffnest du die **Discord-Übertragung** eines Spielers, und Yvara AI nimmt das Discord-Fenster auf (`capture.mode: window`, `window_title: "Discord"`).
  3. Yvara AI schaut einen **Twitch-/CDL-Stream** oder ein Video im Browser (Lern-Modus mit „Bildschirm“).

### Mehrere Spieler gleichzeitig (Monitore, Discord, Twitch)

Im Tab **Match → Bildquellen** (oder `feeds:` in `config.yaml`) legst du pro Spieler einen Feed an:

| Art | ref | Beispiel |
|---|---|---|
| `monitor` | Bildschirm-Nummer | dein eigener Bildschirm |
| `window` | Fenstertitel (Windows) | Discord-Übertragung eines Mates |
| `region` | `links oben breite höhe` | Teil eines Bildschirms (z. B. mehrere Streams nebeneinander im Browser) |
| `twitch` | Twitch-Kanal | Livestream eines Spielers, wird lokal gelesen |

Gemischt geht auch: einer streamt auf Discord, drei auf Twitch. Yvara AI sieht alle als Kachelbild, entscheidet mit **Auto-Fokus**
selbst, wem sie genauer zuschaut (z. B. wer gerade Probleme hat oder denselben Fehler wiederholt), und spricht die Spieler mit Namen an.
Twitch hat einige Sekunden Verzögerung, das passt für Coaching und Feedback, aber nicht für Calls auf die Sekunde.

---

## Installation

Du brauchst: **Python 3.12**, **ffmpeg**, einen **Anthropic-API-Key** (Yvaras Gehirn) und für Discord einen **Bot-Token**.

### Windows 10/11
1. Python 3.12 von [python.org](https://www.python.org/downloads/) installieren (Haken bei „Add to PATH“).
2. Ordner entpacken, `install_windows.bat` doppelklicken (installiert auch ffmpeg über winget).
3. `.env` öffnen und Keys eintragen (siehe unten).
4. `start_windows.bat` starten. Ohne Discord: `start_local_windows.bat`.

### macOS 13+
1. [Homebrew](https://brew.sh) installieren.
2. Im Terminal: `cd` in den Ordner, dann `./install_mac.sh`.
3. `.env` öffnen und Keys eintragen.
4. **Systemeinstellungen > Datenschutz & Sicherheit:** Terminal Zugriff auf *Bildschirmaufnahme*, *Mikrofon* und (für Push-to-Talk) *Bedienungshilfen* geben.
5. `start_mac.command` doppelklicken. Ohne Discord: `start_local_mac.command`.
6. **Discord-Comms auf dem Mac mithören:** macOS kann PC-Ton nicht direkt abgreifen. Installiere `brew install blackhole-2ch`,
   lege in „Audio-MIDI-Setup“ ein *Gerät mit mehreren Ausgängen* (Kopfhörer + BlackHole) an, wähle es in Discord als Ausgabe und trage in
   `config.yaml` bei der Loopback-Quelle `name: "BlackHole"` ein. Fenster-Aufnahme gibt es auf dem Mac nicht, nutze `monitor` oder `region`.

### Keys eintragen (`.env`)
```
ANTHROPIC_API_KEY=...   # https://console.anthropic.com -> API Keys (nicht nötig mit Ollama, siehe unten)
DISCORD_TOKEN=...       # siehe nächster Abschnitt
```

### Gehirn wählen: Claude oder kostenlos lokal (Ollama)
| | Claude (Anthropic-API) | Ollama / LM Studio (lokal) |
|---|---|---|
| Kosten | pro Nutzung (siehe *Kosten*) | **0 €**, läuft auf deinem PC |
| Qualität | beste Analyse, Taktik, Bildverständnis | gut, aber deutlich schwächer bei Taktik und schnellen Bildern |
| Internet | nötig | nicht nötig (außer Recherche) |
| Hardware | egal | Grafikkarte mit 8 GB+ VRAM oder Mac mit Apple-Chip und 16 GB+ empfohlen |

Hinweis: Ein Claude.ai-Abo (Pro/Max) kann für eigene Programme wie Yvara AI **nicht** genutzt werden; die API ist ein eigenes, getrenntes Konto.

**Ollama einrichten (kostenlos):**
1. [ollama.com](https://ollama.com) installieren.
2. Ein Modell laden, das Bilder versteht: empfohlen `ollama pull gemma3:12b` (schnell, gutes Deutsch und Englisch, denkt nicht lange nach). Trägst du es in den Einstellungen ein, lädt Yvara AI es auch selbst herunter.
3. In Yvara AI: *Einstellungen → KI-Modell → Ollama*, Modellname eintragen, speichern. Wechsel geht ohne Neustart.
   (Oder in `config.yaml`: `llm.provider: ollama` und `llm.local_model: gemma3:12b`.)

LM Studio geht genauso (*Provider: LM Studio / OpenAI-kompatibel*, Server in LM Studio starten). Online-Recherche läuft lokal
über eine kostenlose DuckDuckGo-Suche. Tipp: Für den Fortbildungsmodus über Nacht lohnt sich lokal besonders, weil es nichts kostet.

### Discord-Bot anlegen (einmalig, ca. 5 Minuten)
1. [discord.com/developers/applications](https://discord.com/developers/applications) → *New Application* → Name „Yvara AI“.
2. *Bot* → *Reset Token* → Token kopieren und in `.env` eintragen.
3. *OAuth2 → URL Generator*: Scopes `bot` + `applications.commands`, Rechte *Connect*, *Speak*, *Send Messages*, *Attach Files*, *Use Slash Commands*.
4. Erzeugten Link öffnen und den Bot auf euren Server einladen.
5. Unter *Bot* den **Message Content Intent** einschalten (für @Yvara AI im Textchat).
6. Textkanal `coach-reports` anlegen (dort landen Pläne, Reviews, Heatmaps).
7. Optional in `config.yaml` → `discord.guild_id` eure Server-ID eintragen, dann erscheinen die Slash-Commands sofort.

---

## Erste Schritte

1. **Starten:** Auf dem Desktop **„Yvara AI“** doppelklicken (legt der Installer an, sonst `python -m coach shortcut`). Yvara AI öffnet sich als eigenes Programmfenster, ohne Browser-Tabs. Wer lieber den Browser will: `web.app_window: false`.

   **Desktop-App ohne Konsolenfenster:** Die Verknüpfung startet `pythonw -m coach desktop`. Yvara AI läuft dann unsichtbar im Hintergrund, und es öffnet sich ein echtes App-Fenster (WebView2, Paket `pywebview`). Fenster schließen = Yvara AI beendet. Updates starten Yvara AI im Hintergrund selbst neu. Die Ausgaben stehen in `data/logs/vyra.log` (und `console.log`). Nach dem ersten Start mit der neuen Version wird eine alte Verknüpfung automatisch umgestellt. Die Konsole steckt jetzt direkt in der App (Menü „Konsole“). Beim Schließen wird alles beendet: KI-Modelle werden aus dem Grafikspeicher entladen, Bot und Stimmen gestoppt. Nur zur Fehlersuche: `start_mit_konsole.bat`.
2. **Team anlegen:** Links „+ Neues Team“, z. B. *Yvara Main* und *Yvara Z8*. Im Tab *Team* Spieler (In-Game-Name + Rolle) eintragen.
3. **HUD kalibrieren** (einmal pro Spiel/Auflösung): Match starten, dann `python -m coach calibrate` (bzw. `.venv\Scripts\python -m coach calibrate`).
   Nach 5 Sekunden kommt ein Screenshot, darauf ziehst du Rahmen um Score, Timer, Hill-Timer, Killfeed und **Minimap**. Testen mit `python -m coach test-capture`.
4. **Im Discord:** in den Voice-Channel gehen, `/coach join`. Yvara AI begrüßt euch.
5. **Match:** `/match start` (Modus, Map, Gegner) → spielen → `/match end` (Score). Danach kommt das Review in den Report-Kanal, und die Profile werden aktualisiert.

### Mit Yvara AI sprechen
„**Yvara AI**, …“ oder „**Coach**, …“ am Satzanfang, oder Push-to-Talk-Taste (Einstellungen). Beispiele:

| Sag | Passiert |
|---|---|
| „Yvara AI, was machen die gerade auf B?“ | Antwort mit Blick aufs aktuelle Bild und die Minimap |
| „Yvara AI, clip das!“ | Moment + Screenshot werden fürs Review gespeichert |
| „Yvara AI, wie war die Runde?“ | Sofort-Review |
| „Yvara AI, schau auf Player2“ / „ganzes Team“ | Fokus-Modus an/aus |
| „Yvara AI, wechsel zu Yvara Z8“ | Team-Projekt wechseln (nicht während eines Matches) |
| „Yvara AI, Timeout“ | Reset-Call |
| „Yvara AI, sei ruhig“ / „Calls an“ | Live-Calls stumm / wieder an |
| „Yvara AI, wie sind unsere Comms?“ | Comms-Analyse |
| „Yvara AI, Live-Modus“ / „Zuschau-Modus“ / „Training-Modus“ | Coaching-Modus wechseln |
| „Yvara AI, zeig uns den B-Retake“ (im Training-Modus) | Schritt-für-Schritt-Training |
| „Yvara AI, weiter“ / „nochmal“ | Nächster Schritt / Schritt wiederholen |

### Coaching-Modi
- **🎙 Live-Coaching:** Yvara AI macht Calls im Spiel (Rotationen, Trades, Timeout bei Lauf-Serien).
- **👀 Nur zuschauen:** Yvara AI ist komplett still, schaut zu (auch über Twitch) und gibt am Ende der Map / des Matches Feedback:
  was gut war, was schlecht war, was man besser machen sollte. Gut für Ligaspiele.
- **🏋️ Training / Private Match:** Ihr geht ins Private Match, Yvara AI erklärt Schritt für Schritt („Jungs, so geht ihr hin,
  das könnt ihr so abaimen, diese Taktik spielen wir“), schaut dabei zu und korrigiert („Player2, weiter links“).

Umschalten in der Web-App (Tab *Match*), per Stimme oder mit `/coach mode`, Training mit `/coach teach Thema` und `/coach next`.

### Calls verstehen und Positionen beim Namen nennen
Yvara AI hört eure Comms und zerlegt jeden Call: wie viele, wo, Status, was passiert („zwei Top Bridge, einer lit, die pushen“
→ 2 Gegner · Top Bridge · angeschossen · Push). Sie vergleicht die Calls mit dem, was sie sieht (Bild, Minimap, Killfeed),
und sagt im Feedback konkret: „Auf *Map* bei Top Bridge hättest du nicht pushen sollen, sondern …“, statt allgemein zu bleiben.
Und sie bewertet die Calls selbst: richtig, rechtzeitig, klar?

Dafür muss sie die **Callouts eurer Maps** kennen:
- Im Tab *Team → Map-Callouts* eintragen (Name, Minimap-Feld wie `C2`, andere Namen wie „Brücke oben“), oder `/coach callout`,
  oder einfach im Chat sagen: „Merk dir: auf Munition heißt die obere Brücke Top Bridge.“
- Sie lernt sie auch selbst: aus euren Comms + Minimap nach jedem Match und von Castern beim Fortbilden (markiert als „gelernt“).
  Eure eigenen Einträge haben immer Vorrang.
- Die Callout-Namen bekommt auch die Spracherkennung, damit „Top Bridge“ richtig verstanden wird.

### Discord-Befehle
Discord erlaubt höchstens 25 Unterbefehle pro Befehl, deshalb gibt es drei:

- `/coach` join · leave · ask · chat · say · mode · teach · next · callout · callouts · language · team · focus · stats · profile · scout · training · update
- `/match` start · end · score · plan · review · note · clip · timeout
- `/learn` start · stop · queue · video · video_stop · ideas · playbook

Die Befehle werden beim Start direkt auf jedem Server angemeldet, auf dem Yvara AI ist (Konsole: „… Befehle registriert auf Server …“).
Fehlen sie trotzdem: Discord mit Strg+R neu laden. Steht in der Konsole „neu einladen“, den angezeigten Link öffnen.

**Gespräch in Echtzeit:** Yvara AI antwortet Satz für Satz, sobald der erste Satz fertig ist. Redest du dazwischen, hört sie sofort auf.
Deine eigenen Fragen am Mikro beantwortet sie immer, auch ohne „Coach“ (außer sie gehen an einen Mate, z. B. „Max, wo bist du?“).
Fragen der Mates in den Comms beantwortet sie, wenn sie helfen kann (höchstens alle 8 s, `audio.proactive`).
Deutsch und Englisch erkennt sie pro Satz und antwortet in derselben Sprache. „Was kannst du?“ erklärt alles.
Mit NVIDIA-Grafikkarte nutzt die Spracherkennung automatisch das große Modell `large-v3-turbo`.

**Per Sprache:** „Coach, komm rein“ → Yvara AI kommt in deinen Voice-Channel, „Coach, geh raus“ → sie geht.
Trag unter `discord.follow_user` deinen Discord-Namen ein, dann findet sie dich sicher.

---

## Lern-Modus: Yvara AI wird mit jedem Video besser

Yvara AI lernt **automatisch im Hintergrund**, sobald sie läuft – nur das eingestellte Spiel (Standard: Modern Warfare 4,
Modi Hardpoint, SnD, Hijack; andere CoD-Teile werden aussortiert). Läuft ein CDL-Stream auf Twitch, schaut sie ihn live
mit. Während eines Matches, beim Zuschauen oder wenn ihr mit ihr redet, pausiert das Lernen, damit sie schnell antwortet.

In der Chat-App, Tab **Lernen** (oder `/learn video`). Ganz oben zeigt **📈 Lernstand**, wie viele Stunden Yvara AI
schon geschaut hat, wie viele Lektionen, eigene Taktiken und Playbooks es gibt, was gerade läuft (mit Fortschrittsbalken)
und den **Lernverlauf**: jedes Video mit Datum, geschauter Zeit und den gelernten Lektionen (antippen zum Aufklappen).
Alles liegt in `data/library` und bleibt bei Updates erhalten.

- **Live-Stream** (Twitch, CDL, Discord-Übertragung, YouTube live): Stream öffnen, Quelle leer lassen bzw. `screen`. Den Caster-Ton hört Yvara AI über den PC-Ton mit.
  In der Caster-Ansicht trägst du den Minimap-Bereich unter `study.minimap_roi` ein (oder kalibrierst ihn), dann liest sie alle 8 Spieler.
- **Video-Link** (YouTube, Twitch-VOD): Link einfügen. Yvara AI lädt das Video mit yt-dlp, transkribiert den Kommentar und analysiert es minutenweise.
  Nutzt nur Videos, die ihr für eure eigene Analyse verwenden dürft.
- **Eure Scrims als Datei:** Pfad zur Videodatei eintragen.
- **Euer eigener Liga-Stream:** Haken bei „Das ist UNSER Team“. Dann landen eure Fehler und Stärken im Team-Gedächtnis statt in der Pro-Bibliothek.

Aus allen Lektionen baut Yvara AI pro Modus ein **Playbook** (Prinzipien, Setups, Rotationen, Fehler, Drills). Das fließt automatisch in Live-Calls,
Pläne, Reviews, Trainingspläne und den Chat ein. Neue Modi wie **Hijack** lernt sie so von Grund auf; trage die Grundregeln in
`data/knowledge/modes.yaml` ein oder lass Yvara AI im Chat mit Online-Recherche nachschauen.

### 🎓 Fortbildungsmodus: lernen, während du arbeitest

Tab **Lernen → Fortbildung starten** (oder `/learn start`). Yvara AI lernt dann selbstständig, bis du **Stopp** drückst (`/learn stop`):

1. Erst schaut sie die Links, die du ihr geschickt hast (einfach in den Chat posten: „Schau dir das an: https://youtube.com/…“, oder `/learn queue`).
2. Ist die Warteschlange leer, überlegt sie selbst, wo ihr Wissen noch dünn ist (z. B. Hijack), sucht passende Videos auf YouTube und
   holt die neuesten VODs der Call of Duty League von Twitch (`autolearn.twitch_channels`).
3. Jedes Video wird angeschaut, Kommentar und Minimap ausgewertet, Lektionen landen in der Bibliothek, Playbooks werden aktualisiert.
4. Alle paar Videos entwickelt sie **eigene Taktiken für euer Team**: nicht kopiert, sondern passend zu euren Spielern, mit Setup,
   Rollen, Timings, Konter und Risiko. Die landen im Tab *Lernen*, im Team-Gedächtnis, unter `reports/` und im Discord-Report-Kanal (`/learn ideas`).

Tipp: Laptop oder PC einfach laufen lassen. Mit `autolearn.max_hours` / `max_videos` kannst du ein Limit setzen.
**Kosten beachten:** Jedes Video kostet je nach Länge grob 0,50 bis 2 € an API-Gebühren (lange VODs werden auf
`max_minutes_per_video` Minuten verteilt angeschaut). Günstiger: `study.effort: low`, weniger `frames_per_window`, größeres `window_s`.

---

## Bildschirm zeigen: Yvara AI schaut zu

Tab **Match** → „👀 Bildschirm zeigen“: Quelle wählen (Bildschirm, Call-of-Duty-Fenster oder die Discord-Übertragung
eines Mates) und „▶ Zuschauen“. Du siehst eine Live-Vorschau, was Yvara AI sieht, und darunter ihre Einschätzung.
Sie versteht so das Spiel und den Team-Zusammenhang und du kannst sie direkt dazu fragen. Per Stimme:
„Coach, schau zu“ / „Coach, hör auf zuzuschauen“.

## Gedächtnis: Yvara AI lernt aus jedem Gespräch

Was ihr Yvara AI erzählt oder beibringt (Rollen, Strats, Callouts, wie ihr gecoacht werden wollt), merkt sie sich von
selbst – ohne „merk dir“. Alles steht im Tab **Gedächtnis** (mit Herkunft) und kann dort gelöscht werden.

## Taktik: Strat-Board, Heatmaps, Timer, Gegner-Muster

- **Strat-Board** (App → Tab **Taktik** → „Neue Strat zeichnen“): Pfeile, Freihand, Bereiche, Gegner (✕), Spieler
  und Text auf die Map zeichnen. Als Hintergrund dient die Minimap aus euren Matches oder ein eigenes Kartenbild
  („Kartenbild…“). Speichern und mit einem Klick in Discord posten (oder `/match strat name:`).
- **Heatmaps pro Map**: Yvara AI sammelt über alle Matches, wo ihr steht (blau), wo die Gegner sind (rot) und wo
  gekämpft wird (gelb). Im Tab Taktik ansehen und posten, oder `/match heatmap`. Braucht eine eingerichtete Minimap.
- **Timer-Calls**: „Coach, Timer 30 Sekunden“, „Coach, Hill ist neu“ (Hill-Uhr mit „Zwei Mann vorrotieren“),
  „Coach, Timer aus“, `/match timer`. In SnD startet der Bomben-Timer automatisch, wenn jemand „geplantet“ oder
  „Bombe liegt“ callt („Bombe 30 … Bombe 15 … Letzte Chance“). „Gegner haben UAV“ → Yvara AI sagt, wann sie weg sein müsste.
  Zeiten unter `timers:` in der config.yaml anpassbar.
- **Gegner-Tendenzen**: Aus euren Calls („zwei Top Bridge“) und der Minimap erkennt Yvara AI wiederkehrende Wege und sagt
  sie live an („Die kommen immer wieder über Top Bridge“). Pro Gegner und Map gemerkt – beim nächsten Match gegen
  dasselbe Team sagt sie am Anfang, wo die letztes Mal oft waren.
- **Stil**: „Coach, sei toxisch“ (roastet euch aus Spaß, ohne echte Beleidigungen, pausiert bei Tilt),
  „Coach, Flirt-Modus“ (charmant, jugendfrei), „Coach, wieder normal“ – oder `/coach stil` bzw. in den Einstellungen.
- **Raus aus dem Channel**: „Coach, geh raus“, „Coach, tschüss“, „Coach, hau ab“ – Yvara AI verabschiedet sich und geht.

## Discord: Yvara AI im Team

- Yvara AI ist ein eigener **Bot-Account** (eigener Name + Profilbild im Developer-Portal).
- **Von selbst in den Channel:** `discord.auto_join_channels: ["Scrims", "Liga"]`. Sobald irgendwer aus dem Team reingeht,
  kommt Yvara AI dazu, und wenn alle weg sind, geht sie. Du musst dafür selbst nicht im Channel sein.
- **Oder dir folgen:** `discord.follow_user: "deinname"`.
- **Ohne dich (Stand-alone):** Das Programm muss nur auf einem Rechner laufen, der an ist (dein PC, ein alter Laptop, ein Mini-PC).
  Sprechen, Slash-Befehle und `@Yvara AI` im Textchat gehen dann immer. Zum **Hören** braucht dieser Rechner Discord mit einem
  zweiten, normalen Account (z. B. „Yvara AI Ohr“, stummgeschaltet) im selben Channel, weil Bots Sprache nicht empfangen können.
  Zum **Sehen** nutzt Yvara AI die Twitch-Streams der Spieler (Feeds), dafür muss niemand am Rechner sitzen.
- **Alle Mates können mit ihr reden:** Sie hört die Comms über deinen PC mit, also kann jeder „Yvara AI, …“ sagen.
- **Textchat:** `@Yvara AI wie spielen wir morgen Hardpoint?` → Antwort mit Gedächtnis (ein Verlauf pro Kanal).
  Dafür im Developer-Portal unter *Bot* den **Message Content Intent** einschalten.
- **Übertragungen anschauen:** Bots dürfen Discord-Übertragungen nicht beitreten, und ein gesteuerter User-Account verstößt gegen die
  Discord-Regeln (Sperr-Risiko). Deshalb öffnest du die Übertragung auf dem Rechner, auf dem Yvara AI läuft, und stellst
  `capture.mode: window` + `window_title: "Discord"` ein. Dann sieht Yvara AI genau diese Übertragung.

---

## Stimme

- **Eine Stimme für Deutsch und Englisch (XTTS, empfohlen mit NVIDIA-Grafikkarte):** Yvara AI schließen,
  `stimme_einrichten.bat` doppelklicken (einmalig, ca. 4–5 GB; dabei fragt das Programm, ob du der nicht-kommerziellen
  Coqui-Lizenz zustimmst – das bestätigst du selbst). Danach in **Einstellungen → Stimme** „Eine Stimme für Deutsch + Englisch“
  wählen, Sprecher aussuchen (optional eine andere Stimme nur fürs Englische), mit **▶ Vorhören** testen, Tempo per Schieberegler, **Speichern**.
- **Eigene Stimme:** In den Einstellungen „🎙️ Eigene Stimme“ aufklappen, 15–30 Sekunden den Beispieltext vorlesen
  (oder Dateien hochladen, auch WhatsApp-Sprachnachrichten .opus/.ogg, mp3, m4a – mehrere kurze werden zusammengesetzt), bestätigen, dass es deine Stimme ist oder die Person zugestimmt hat, speichern, vorhören.
  Die Aufnahmen liegen in `data/voices/custom` und bleiben bei Updates erhalten. Bitte nur mit Einverständnis der Person.
- **Lokal (Piper, offline, sehr schnell):** Piper. Die Stimmen `de_DE-thorsten-high` und `en_US-ryan-high` werden beim ersten Sprechen automatisch geladen.
  Andere Stimmen: [Piper-Stimmen](https://huggingface.co/rhasspy/piper-voices). **Eigene Stimme:** eine eigene Piper-Stimme trainieren
  (oder trainieren lassen) und den Pfad zur `.onnx`-Datei in den Einstellungen eintragen.
- **Online:** Edge-Stimmen (sehr natürlich), `tts.engine: edge`.
- **Ausgabe:** Discord-Voice (`tts.output: discord`) oder deine Lautsprecher (`local`). In der Chat-App liest Yvara AI Antworten vor (🔊).

---

## Updates (automatisch)

Yvara AI holt sich Updates selbst von GitHub (`update.repo` in `config.yaml`, Standard `YvaraEsports/vyra-coach`):
- **Beim Start** prüft sie auf eine neue Version, installiert sie und startet neu. Neue Pakete werden automatisch nachinstalliert.
- **Manuell:** Chat-App → *Einstellungen → Nach Updates suchen*, Discord `/coach update` oder `python -m coach update`.
- **Eure Daten bleiben immer erhalten:** `config.yaml`, `.env`, alle Teams, Gedächtnis, Chats, Bibliothek und Reports werden nie überschrieben.
- Ist das Repo **privat**, trage in `.env` einen GitHub-Token mit Lesezugriff ein (`GITHUB_TOKEN=`). Bei einem öffentlichen Repo ist nichts nötig.
- Ohne Update beim Start: `--no-update` anhängen.

---

## Daten & Datenschutz

Alles liegt lokal im Ordner `data/`:
- `data/teams/<team>/coach.db`: Matches, Runden, Comms, Reviews, Profile, Gedächtnis, Chats, Clips, Ratings
- `data/teams/<team>/reports/`: Reviews, Pläne, Trainingspläne, Scouting-Reports, Heatmaps
- `data/library/library.db`: Taktik-Bibliothek und Playbooks (teamübergreifend)

Zur KI (Anthropic) gehen nur die Inhalte der jeweiligen Anfrage (Bild-Ausschnitte, Text, Kontext). Die Chat-App ist nur auf deinem Rechner erreichbar.

**Kosten:** Mit Ollama/LM Studio kostet nichts davon Geld. Mit Claude ist Yvaras Gehirn *Claude Opus 5.5* über die Anthropic-API (Abrechnung pro Nutzung). Die größten Posten sind die
Bildanalyse im Match (`capture.vision_interval_s`, Standard 20 s) und der Lern-Modus (`study.window_s`, `study.frames_per_window`).
Größere Abstände bzw. weniger Bilder sparen Geld. Spracherkennung, Stimme und OCR laufen kostenlos lokal.

---

## Fehlerbehebung

| Problem | Lösung |
|---|---|
| Yvara AI versteht nichts | `python -m coach devices` → richtige Geräte in `config.yaml` (`audio.sources`) eintragen, `python -m coach test-mic` |
| Spricht nicht im Discord | Bot im Channel? (`/coach join`), ffmpeg installiert? Rechte *Connect/Speak*? |
| Score/Timer falsch | neu kalibrieren, HUD-Skalierung im Spiel nicht ändern, `test-capture` prüfen; Score per `/match score` korrigieren |
| Minimap-Marker fehlen | Bereich neu kalibrieren, Teamfarben unter `minimap.team_colors` anpassen (Caster-Ansicht hat oft eigene Farben) |
| „Keine Verbindung zum Gehirn“ | `ANTHROPIC_API_KEY` in `.env` prüfen, Internet |
| Langsame Spracherkennung | `audio.stt_model: base` oder NVIDIA-GPU nutzen (`stt_device: cuda`) |

---

## Für Entwickler

```
coach/
  app.py            Start & Verdrahtung          brain/coach.py   Coach-Gehirn (Calls, Reviews, Lernen, Chat)
  discord_bot.py    Slash-Commands, Voice         brain/prompts.py Persönlichkeit, Prompts, JSON-Schemas
  teams.py          Team-Projekte                 brain/llm.py     Claude-Anbindung (Tools, Websuche, Fallbacks)
  web/              Chat-App (aiohttp + HTML/JS)  study/           Lern-Modus, Taktik-Bibliothek, Playbooks
  audio/            Mikro/Loopback, VAD, Whisper, Piper/Edge-TTS, Sprach-Warteschlange
  vision/           Capture, HUD-OCR, Spielzustand & Events, Minimap-Tracking
data/knowledge/     modes.yaml, maps.yaml (Map-Callouts selbst pflegen)
```
Tests: `pip install pytest pytest-asyncio && python -m pytest`


## Gaming-Modus (CoD flüssig halten)

Sobald Call of Duty läuft (cod.exe u. ä.), schaltet Yvara AI automatisch in den Gaming-Modus:
kleines KI-Modell (`gemma3:4b`, das große wird aus dem Grafikspeicher entladen), keine KI-Bildanalyse,
Hintergrund-Lernen pausiert, niedrigere Prozess-Priorität. Spiel zu = volle KI wieder an.
Einstellungen → „Gaming-Modus“: Automatisch / Immer an / Aus. Das kleine Modell lädt Yvara AI vorab einmalig herunter.
Tipp: In CoD unter Grafik „VRAM-Skalierungsziel“ auf 70–80 % stellen, damit Spiel und Yvara AI zusammen in die 16 GB passen.


## Sicherheit

- **Netzwerk-Sperre** (technisch, nicht nur per Prompt): Yvara AI verbindet sich nur mit Discord, YouTube/Twitch (Video-Lernen),
  Hugging Face (Modelle), dem eigenen GitHub-Repo (Updates), der Claude-API, Edge-Stimmen und – nur wenn erlaubt – der
  Websuche. Alles andere wird blockiert und im Log als „🔒 Sicherheit: … blockiert“ vermerkt.
- **Internet nur mit Erlaubnis:** Websuche nur mit dem Schalter „Online-Recherche“ oder wenn ihr ausdrücklich darum bittet
  („recherchier mal …“, „google mal …“).
- **Die KI kann nichts ausführen:** keine Befehle, kein Code, kein Dateizugriff, kein Hochladen. KI-Antworten werden nur
  gesprochen/angezeigt. Updates kommen ausschließlich aus dem eingestellten Repo.

## Smalltalk und Stimmen per Sprachbefehl

Läuft kein Match (und Yvara AI schaut nicht zu), ist Yvara AI ein normaler Gesprächspartner über alles – ohne ständig auf
Hardpoint zu lenken oder Matches zu erfinden. Im Match oder beim Zuschauen ist der Fokus wieder voll aufs Spiel.
Stimme wechseln per Sprache: „Coach, nimm die Stimme von Shotzy“, „Coach, sprich auf Englisch mit Sinans Stimme“,
„Coach, nimm wieder die Standardstimme“.

## 🔒 Privatsphäre: alles bleibt auf deinem PC

Standardmäßig geht **nichts** von deinen Daten ins Internet: kein Gedächtnis, keine Dateien, keine Stimmen, keine Bilder.
- KI: nur lokal (Ollama). Die Claude-API ist gesperrt (`privacy.allow_cloud_ai: false`).
- Stimmen: nur lokal (XTTS/Piper). Edge-Stimmen sind gesperrt (`privacy.allow_cloud_voice: false`).
- Websuche: aus (`privacy.allow_web_search: false`).
- Match-Reports werden nicht automatisch in Discord gepostet (`privacy.discord_reports: false`).

Nach außen gehen nur Downloads (Modelle, Lern-Videos von YouTube/Twitch, Updates) und Discord selbst
(der Bot, Antworten im Chat und der Sprachkanal). Alles andere blockiert die Netzwerk-Sperre.

## 📞 Sprach-Call in der App, Parakeet und Supertonic (ab 0.16)

- **Call ohne Discord:** Oben auf „📞 Call“ klicken und einfach reden. Yvara AI hört über dein Mikrofon zu, antwortet über die Lautsprecher und merkt sich wie im Discord, was ihr besprecht. „Auflegen“ beendet den Call.
- **Spracherkennung Parakeet v3** (`audio.stt_engine: parakeet`, Standard): Deutsch und Englisch, läuft auf der CPU und braucht keinen Grafikspeicher. OpenVINO wird benutzt, wenn `onnxruntime-openvino` installiert ist (bringt vor allem auf Intel-Hardware etwas). Für lange Lern-Videos lädt Yvara AI bei Bedarf weiterhin Whisper. Zurück zu Whisper: `audio.stt_engine: whisper`.
- **Sprachausgabe Supertonic** (`tts.engine: supertonic`): sehr schnell und ohne Grafikkarte, Deutsch und Englisch, 10 fertige Stimmen (M1–M5, F1–F5). Eigene Stimmen klonen kann Supertonic nicht, dafür bleibt XTTS (Damien Black, Yvara Girl, eure eigenen Stimmen). Fällt XTTS aus, springt Supertonic ein.

## 📣 Caster-Modus (ab 0.17)

Yvara AI kann statt Coach auch **Caster** sein: Sie kommentiert Liga-Matches, Turniere und Scrims live für deinen Stream, auf Deutsch oder Englisch, mit Emotionen. Umschalten geht in der App (📣 Caster → „Coach | Caster“) oder per Stimme („Yvara AI, Caster-Modus“ bzw. „Yvara AI, Konversations-Modus“).

- **Sofort-Reaktion:** Bei Doppel- und Multikills ruft sie in unter einer Sekunde („Ohhh, Doppelkill!“). Das klappt ohne KI, weil sie den Killfeed-Bereich im Bild vergleicht und die Rufe vorab erzeugt hat. 1 bis 3 Sekunden später sagt die KI, **wer** es war und was es für die Runde bedeutet. Zusätzlich gibt es Hype-Knöpfe in der App (gehen auch über ein Stream Deck per `POST http://127.0.0.1:8765/api/caster/moment {"kind":"multi"}`).
- **Twitch-Chat:** Yvara AI liest den Chat anonym mit (kein Login, kein Token), begrüßt neue Zuschauer und beantwortet Fragen zum Match. Chat-Nachrichten sind für sie nur Daten: Befehle aus dem Chat befolgt sie nicht, und Beleidigungen oder Links wiederholt sie nicht. Sie schreibt nie selbst in den Chat.
- **Stimme im Stream:** Mit Ausgabe „OBS-Overlay“ öffnest du in OBS eine Browserquelle auf `http://IP-DES-CASTER-GERÄTS:8765/overlay` (in OBS „Audio über OBS steuern“ anhaken). Dann laufen Untertitel und Stimme direkt im Stream. Im Heimnetz ist **nur** das Overlay erreichbar, alles andere (Gedächtnis, Einstellungen, Chat) bleibt gesperrt.
- **Bildquelle mit wenig Delay:** OBS auf dem Gaming-PC schickt das Spielbild per SRT übers Heimnetz an das Caster-Gerät (`stream:9000`), meist unter 1 Sekunde Verzögerung. Alternativ geht der Twitch-Stream (`twitch:kanal`, 2 bis 5 Sekunden), ein Monitor oder Fenster oder eine Capture-Karte.
- **Von Casts lernen:** In der App unter „Von Casts lernen“ eine Videodatei, einen YouTube- oder Twitch-Link oder ein Transkript eingeben. Yvara AI hört lokal zu und übernimmt typische Sprüche und Techniken in ihren Stil.
- **MacBook als Caster:** Mit `./install_mac.sh` installieren, danach `./start_caster_mac.command` starten. Die ausführliche Anleitung für das Setup mit zwei Geräten liegt im Projektordner.

**Coach über Twitch zuschauen lassen** (z. B. wenn Yvara AI auf dem Laptop läuft): In Discord `/coach watch kanal:<twitch-name>` eingeben oder „Yvara AI, schau bei Twitch von <name> zu“ sagen. Beenden geht mit `/coach unwatch`. Ein Discord-Bot kann Bildschirmübertragungen in Discord nicht sehen, deshalb läuft das über Twitch.

## 🔗 Sync, Trash Talk und Selbsterklärung (ab 0.17)

- **Sync für zwei Geräte:** Die KI läuft auf dem Laptop oder MacBook, der Gaming-PC schickt nur Bild, PC-Sound und auf Wunsch das Mikrofon (alles einzeln schaltbar). Einrichten in der App unter 🔗 Sync: Auf dem Laptop „KI-Gerät (empfängt)“ wählen, auf dem PC „Gaming-PC (sendet)“ und dort Adresse und Kopplungs-Code vom Laptop eintragen. Der PC lädt dann keine KI und startet als schlankes Sender-Fenster. Die Übertragung bleibt im Heimnetz und braucht den Code.
- **Trash Talk:** Unter Einstellungen gibt es den Regler von 0 bis 3. Yvara AI spiegelt deinen Ton: Eine kleine Spitze bekommt eine kleine Spitze zurück, eine heftige Beleidigung eine heftige. Feste Grenzen gelten auf jeder Stufe: nichts gegen Herkunft, Religion, Geschlecht, Sexualität oder Behinderung, keine Slurs, keine Drohungen, und kein Roast gegen Leute, die nicht mitmachen.
- **Erklärt sich selbst:** Frag einfach „Coach, wie funktioniert der Caster?“, „wo finde ich …?“ oder „ich check das mit den Spawns nicht“. Sie erklärt dann Schritt für Schritt mit Beispiel, ihre eigenen Funktionen genauso wie Spielwissen.
- **Name:** Sie heißt jetzt Yvara AI. „Coach“ und „Vyra“ funktionieren als Ansprache weiter.
- **Auto-Update:** Beim Start sucht die App im GitHub-Repo (`update.repo` in der Konfiguration) nach Updates und installiert sie automatisch. Deine Daten, `config.yaml` und `.env` bleiben dabei unangetastet.
