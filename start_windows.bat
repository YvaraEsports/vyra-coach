@echo off
rem Startet Yvara AI als App (eigenes Fenster, ohne Konsole). Zur Fehlersuche: start_mit_konsole.bat
cd /d "%~dp0"
start "" ".venv\Scripts\pythonw.exe" -m coach desktop %*
