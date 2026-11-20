@echo off
chcp 65001 >nul
title Yvara AI – eine Stimme fuer Deutsch und Englisch einrichten
cd /d "%~dp0"
echo Richtet die XTTS-Stimme ein: EINE Stimme fuer Deutsch und Englisch, laeuft auf deiner Grafikkarte,
echo und du kannst eigene Stimmen aufnehmen. Download ca. 4-5 GB, dauert ein paar Minuten.
echo Bitte Yvara AI vorher schliessen.
echo.
call .venv\Scripts\activate.bat
echo [1/3] PyTorch fuer die Grafikkarte ...
python -m pip install --disable-pip-version-check "torch==2.8.0" "torchaudio==2.8.0" --index-url https://download.pytorch.org/whl/cu128
if errorlevel 1 goto fehler
echo [2/3] Sprachmodell-Paket ...
python -m pip install --disable-pip-version-check "coqui-tts>=0.27,<0.29"
if errorlevel 1 goto fehler
echo.
echo [3/3] Stimmenmodell herunterladen (ca. 1,8 GB).
echo Das Modell steht unter der nicht-kommerziellen Coqui-Lizenz (CPML). Gleich wirst du gefragt,
echo ob du zustimmst - lies den Text und tippe selbst y und Enter, wenn du einverstanden bist.
echo.
set "TTS_HOME=%~dp0data\voices\xtts"
python -c "from TTS.api import TTS; TTS('tts_models/multilingual/multi-dataset/xtts_v2', progress_bar=True); print('OK')"
if errorlevel 1 goto fehler
echo.
echo Fertig! Yvara AI neu starten, dann in den Einstellungen bei Stimme "Eine Stimme (XTTS)" waehlen.
pause
exit /b 0
:fehler
echo.
echo Fehler - bitte Screenshot schicken. Yvara AI spricht solange weiter mit Piper.
pause
