@echo off
chcp 65001 >nul
title Yvara AI – Grafikkarte einrichten
cd /d "%~dp0"
echo Installiere die NVIDIA-Bibliotheken fuer schnelle Spracherkennung (ca. 1,2 GB) ...
echo Bitte Yvara AI vorher schliessen.
echo.
call .venv\Scripts\activate.bat
python -m pip install --disable-pip-version-check nvidia-cublas-cu12 "nvidia-cudnn-cu12==9.*"
echo.
if %errorlevel%==0 (echo Fertig! Yvara AI jetzt neu starten.) else (echo Fehler - bitte Screenshot an den Support schicken.)
pause
