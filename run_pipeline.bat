@echo off
setlocal ENABLEEXTENSIONS ENABLEDELAYEDEXPANSION

REM =====================================================================
REM  Auvergne avant-vente pipeline - Windows launcher
REM  Runs the embedded QGIS Python (no system Python required).
REM
REM  Usage:
REM    run_pipeline.bat --sro 63149/M06/PMZ/42478
REM    run_pipeline.bat --all-pilots
REM    run_pipeline.bat --list-sros
REM
REM  Override the QGIS install root by setting QGIS_ROOT before calling.
REM =====================================================================

if not defined QGIS_ROOT set "QGIS_ROOT=C:\Program Files\QGIS 4.0.1"

set "OSGEO4W_ENV=%QGIS_ROOT%\bin\o4w_env.bat"
if not exist "%OSGEO4W_ENV%" (
    echo [X] o4w_env.bat introuvable: "%OSGEO4W_ENV%"
    echo [!] Definir QGIS_ROOT vers le dossier d installation QGIS et relancer.
    exit /b 2
)

call "%OSGEO4W_ENV%"
if errorlevel 1 (
    echo [X] Echec de o4w_env.bat
    exit /b 1
)

set "SCRIPT_DIR=%~dp0"
pushd "%SCRIPT_DIR%" >nul

echo [OK] QGIS env charge: %QGIS_ROOT%
echo [OK] Lancement: python -m auvergne_pipeline.main %*

python -m auvergne_pipeline.main %*
set "RC=%ERRORLEVEL%"

popd >nul

if not "%RC%"=="0" (
    echo [!] Pipeline termine avec code %RC%
) else (
    echo [OK] Pipeline OK
)
exit /b %RC%
