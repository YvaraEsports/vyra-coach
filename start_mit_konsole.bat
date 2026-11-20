@echo off
chcp 65001 >nul
title Yvara AI (Konsole, zur Fehlersuche)
call .venv\Scripts\activate.bat
:loop
python -m coach run %*
if %errorlevel%==42 goto loop
pause
