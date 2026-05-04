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
REM  Output: stdout + tee'd to logs\run_<timestamp>.log
REM  Override the QGIS install root by setting QGIS_ROOT before calling.
REM =====================================================================

if not defined QGIS_ROOT set "QGIS_ROOT=C:\Program Files\QGIS 4.0.1"

set "OSGEO4W_ENV=%QGIS_ROOT%\bin\o4w_env.bat"
if not exist "%OSGEO4W_ENV%" (
    echo [X] o4w_env.bat introuvable: "%OSGEO4W_ENV%"
    echo [!] Definir QGIS_ROOT vers le dossier d installation QGIS et relancer.
    pause
    exit /b 2
)

call "%OSGEO4W_ENV%"
if errorlevel 1 (
    echo [X] Echec de o4w_env.bat
    pause
    exit /b 1
)

set "SCRIPT_DIR=%~dp0"
pushd "%SCRIPT_DIR%" >nul

REM --- Prepare logs dir + timestamped log file --------------------------
if not exist "logs" mkdir "logs"

set "TS="
for /f "usebackq tokens=*" %%I in (`powershell -NoProfile -Command "(Get-Date).ToString('yyyyMMdd_HHmmss')"`) do set "TS=%%I"
if not defined TS set "TS=%RANDOM%"
set "LOG_FILE=logs\run_%TS%.log"

echo [OK] QGIS env charge: %QGIS_ROOT%
echo [OK] Log file: %LOG_FILE%
echo [OK] Lancement: python -m auvergne_pipeline.main %*
echo.

REM --- Run + tee to log via PowerShell ---------------------------------
REM PowerShell Tee-Object writes to the log AND streams to the host console,
REM so the user sees progress live and the log is preserved.
powershell -NoProfile -ExecutionPolicy Bypass -Command "& { python -u -m auvergne_pipeline.main %* 2>&1 | Tee-Object -FilePath '%LOG_FILE%' }"
set "RC=%ERRORLEVEL%"

popd >nul

echo.
if not "%RC%"=="0" (
    echo [!] Pipeline termine avec code %RC% - voir %LOG_FILE%
) else (
    echo [OK] Pipeline OK - log: %LOG_FILE%
)

pause
exit /b %RC%
