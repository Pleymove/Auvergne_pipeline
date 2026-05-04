@echo off
chcp 65001 > nul
cd /d "%~dp0"
call "C:\Program Files\QGIS 4.0.1\bin\o4w_env.bat"
python -m auvergne_pipeline.launcher
if errorlevel 1 (
    echo.
    echo [X] Erreur lors du lancement du launcher
    pause
)
