#!/bin/bash
# Yvara AI – Installation (macOS 13+)
set -e
cd "$(dirname "$0")"
echo "=== Yvara AI – Installation (macOS) ==="
if ! command -v brew >/dev/null; then
  echo "Homebrew fehlt: https://brew.sh installieren und dieses Skript erneut starten."; exit 1
fi
brew list python@3.12 >/dev/null 2>&1 || brew install python@3.12 python-tk@3.12
command -v ffmpeg >/dev/null || brew install ffmpeg
command -v deno >/dev/null || brew install deno   # yt-dlp braucht eine JS-Laufzeit für YouTube
echo "Optional für Discord-Comms mithören: brew install blackhole-2ch (siehe README)"
[ -d .venv ] || "$(brew --prefix python@3.12)/bin/python3.12" -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
pip install -r requirements.txt
if [ ! -f config.yaml ]; then
  cp config.example.yaml config.yaml
  # MacBook (Apple Silicon): kleines, schnelles Bild-Modell + Supertonic-Stimme (läuft flott ohne Grafikkarte)
  python - <<'PY'
import re
p = "config.yaml"; s = open(p, encoding="utf-8").read()
s = re.sub(r"(?m)^(  local_model:\s*)\S+", r"\1gemma3:4b", s, count=1)
s = re.sub(r"(?m)^(  engine:\s*)\S+", r"\1supertonic", s, count=1)
open(p, "w", encoding="utf-8").write(s)
PY
fi
if ! command -v ollama >/dev/null; then
  echo "Ollama fehlt – installiere es (lokale KI) …"; brew install ollama
fi
(brew services start ollama >/dev/null 2>&1 || true)
[ -f .env ] || cp .env.example .env
# macOS: Loopback braucht BlackHole -> Loopback-Quelle standardmäßig nur mit Namen
echo
echo "Fertig! Nächste Schritte:"
echo " 1. Optional für Discord: .env öffnen und DISCORD_TOKEN selbst eintragen (niemandem schicken!)"
echo " 2. Systemeinstellungen > Datenschutz: Terminal Bildschirmaufnahme + Mikrofon erlauben"
echo " 3. Caster: ./start_caster_mac.command  ·  Coach mit Discord: ./start_mac.command"
echo "    Beim ersten Start lädt Yvara AI das KI-Modell (gemma3:4b, ca. 3 GB) – einmalig."
