@echo off
title AutoClip-AI
cd /d "%~dp0"

:: Ativar virtualenv se existir
if exist ".venv\Scripts\activate.bat" (
    call .venv\Scripts\activate.bat
) else if exist "venv\Scripts\activate.bat" (
    call venv\Scripts\activate.bat
)

python run.py
if errorlevel 1 (
    echo.
    echo [ERRO] Algo falhou. Verifique as dependencias com: pip install -r requirements.txt
    pause
)
