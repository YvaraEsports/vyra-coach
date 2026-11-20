#!/bin/bash
# Yvara AI als Caster auf dem MacBook: ohne Discord, ohne Mikrofon, startet direkt im Caster-Modus.
cd "$(dirname "$0")"
source .venv/bin/activate
(pgrep -x ollama >/dev/null || (brew services start ollama >/dev/null 2>&1 || (ollama serve >/dev/null 2>&1 &)))
while true; do
  python -m coach local --no-audio --caster "$@"
  [ $? -eq 42 ] || break   # 42 = Update installiert -> neu starten
done
