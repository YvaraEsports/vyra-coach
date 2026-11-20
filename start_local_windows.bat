@echo off
rem Yvara AI als App ohne Discord (eigenes Fenster, ohne Konsole)
cd /d "%~dp0"
start "" ".venv\Scripts\pythonw.exe" -m coach desktop --local %*
