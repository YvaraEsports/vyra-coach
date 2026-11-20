@echo off
chcp 65001 >nul
title Yvara AI - Installation
echo === Yvara AI - Installation (Windows) ===

where py >nul 2>nul
if errorlevel 1 (
  echo Python fehlt - installiere Python 3.12 ueber winget ...
  winget install -e --id Python.Python.3.12 --scope user --accept-source-agreements --accept-package-agreements
  set "PATH=%LOCALAPPDATA%\Programs\Python\Launcher;%LOCALAPPDATA%\Programs\Python\Python312;%PATH%"
)
where py >nul 2>nul
if errorlevel 1 (
  echo Python konnte nicht automatisch installiert werden. Bitte Python 3.12 von https://www.python.org/downloads/ installieren ^(Haken bei "Add to PATH"^) und dieses Skript erneut starten.
  pause & exit /b 1
)

where ffmpeg >nul 2>nul
if errorlevel 1 (
  echo Installiere ffmpeg ueber winget ...
  winget install -e --id Gyan.FFmpeg --accept-source-agreements --accept-package-agreements
  echo Falls ffmpeg danach nicht gefunden wird: Fenster schliessen und neu starten.
)

where deno >nul 2>nul
if errorlevel 1 (
  echo Installiere Deno ^(braucht yt-dlp fuer YouTube-Videos^) ...
  winget install -e --id DenoLand.Deno --accept-source-agreements --accept-package-agreements
)

if not exist .venv (
  py -3.12 -m venv .venv 2>nul || py -3 -m venv .venv
)
call .venv\Scripts\activate.bat
python -m pip install --upgrade pip
pip install -r requirements.txt
if errorlevel 1 ( echo Installation fehlgeschlagen. & pause & exit /b 1 )

if not exist config.yaml copy config.example.yaml config.yaml >nul
if not exist .env copy .env.example .env >nul
python -m coach shortcut

echo.
echo Fertig! Naechste Schritte:
echo  1. .env oeffnen und ANTHROPIC_API_KEY + DISCORD_TOKEN eintragen
echo  2. Auf dem Desktop "Yvara AI" doppelklicken (oder start_windows.bat)
pause
