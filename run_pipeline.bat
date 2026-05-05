@echo off
setlocal enableextensions enabledelayedexpansion

REM =====================================================================
REM  Auvergne avant-vente pipeline - Windows launcher
REM
REM  Important: this script must NOT call powershell. After o4w_env.bat,
REM  the PATH is rewritten and no longer contains
REM  C:\Windows\System32\WindowsPowerShell\v1.0\, so 'powershell' is not
REM  found and the run fails with code 9009 before python is launched.
REM
REM  Usage:
REM    run_pipeline.bat --sro 63149/M06/PMZ/42478
REM    run_pipeline.bat --all-pilots
REM    run_pipeline.bat --list-sros
REM
REM  Output: redirected to logs\run_<YYYYMMDD_HHMMSS>.log, then dumped to
REM  the console with 'type' so the user can read it post-run.
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

set "SCRIPT_DIR=%~dp0"
pushd "%SCRIPT_DIR%" >nul

REM --- Output GPKG path (overridable via --output passed in %*) -----------
set "OUTPUT=!SCRIPT_DIR!output\auvergne_outputs.gpkg"

REM --- Charger l'env QGIS (modifie le PATH) ----------------------------
call "%OSGEO4W_ENV%"
if errorlevel 1 (
    echo [X] Echec chargement env QGIS
    popd >nul
    pause
    exit /b 1
)
echo [OK] QGIS env charge: %QGIS_ROOT%

REM --- Timestamp natif cmd via wmic ------------------------------------
REM Format: YYYYMMDDHHMMSS.xxxxxx+ZZZ -> on garde YYYYMMDD_HHMMSS.
REM Pas de delims== sur wmic (le double = peut planter).
REM skip=1 tokens=1 capture la 1ere ligne non vide (la valeur).
set "LDT="
for /f "skip=1 tokens=1" %%I in ('wmic os get localdatetime') do (
    if not defined LDT set "LDT=%%I"
)
if not defined LDT (
    REM Filet de securite si wmic est absent (Win11 Home par exemple).
    set "LDT=00000000000000"
)
set "TS=!LDT:~0,8!_!LDT:~8,6!"

REM --- Preparer logs/ --------------------------------------------------
if not exist logs mkdir logs

set "LOGFILE=logs\run_!TS!.log"
echo [OK] Log file: !LOGFILE!
echo [OK] Lancement: python -m auvergne_pipeline.main %*

REM --- Lancer python avec redirection complete -------------------------
REM %* passe les arguments (--all-pilots, --gpkg, --sro, ...) tels quels.
REM --output est ajoute a la fin (ecrase par l'utilisateur si deja dans %*).
python -m auvergne_pipeline.main %* --output "!OUTPUT!" > "!LOGFILE!" 2>&1
set "RC=!errorlevel!"

REM --- Reafficher le log dans la console -------------------------------
echo.
echo ===== Sortie pipeline =====
type "!LOGFILE!"
echo ===========================

popd >nul

if !RC! equ 0 (
    echo [OK] Pipeline OK - voir !LOGFILE!
) else (
    echo [X] Pipeline termine avec code !RC! - voir !LOGFILE!
)

pause
exit /b !RC!
