#!/bin/bash
cd "$(dirname "$0")"
source .venv/bin/activate
while true; do
  python -m coach local "$@"
  [ $? -eq 42 ] || break   # 42 = Update installiert -> neu starten
done
