@echo off
chcp 65001 > nul
setlocal enabledelayedexpansion

REM --- Auto-detection de QGIS ---
set "QGIS_ROOT="

REM 1. Variable d'environnement QGIS_ROOT
if defined QGIS_ROOT (
    if exist "%QGIS_ROOT%\bin\o4w_env.bat" (
        echo [i] QGIS detecte via QGIS_ROOT : %QGIS_ROOT%
        goto :launch
    )
    echo [!] QGIS_ROOT defini mais o4w_env.bat introuvable, on cherche ailleurs...
)

REM 2. Chemins les plus frequents
for %%d in (
    "C:\Program Files\QGIS 4.0.1"
    "C:\Program Files\QGIS 4.0.0"
    "C:\OSGeo4W"
    "D:\OSGeo4W"
) do (
    if exist "%%~d\bin\o4w_env.bat" (
        echo [i] QGIS detecte : %%~d
        set "QGIS_ROOT=%%~d"
        goto :launch
    )
)

REM 3. Scan C:\Program Files\QGIS*
for /d %%d in ("C:\Program Files\QGIS*") do (
    if exist "%%d\bin\o4w_env.bat" (
        echo [i] QGIS detecte : %%d
        set "QGIS_ROOT=%%d"
        goto :launch
    )
)

REM 4. Scan D:\Program Files\QGIS*
for /d %%d in ("D:\Program Files\QGIS*") do (
    if exist "%%d\bin\o4w_env.bat" (
        echo [i] QGIS detecte : %%d
        set "QGIS_ROOT=%%d"
        goto :launch
    )
)

echo [X] QGIS introuvable.
echo     Aucun o4w_env.bat trouve dans les chemins habituels.
echo     Definis QGIS_ROOT avant de lancer :
echo       set QGIS_ROOT=D:\chemin\vers\QGIS
echo       start.bat
pause
exit /b 1

:launch
REM Charger l'environnement QGIS (Python, GeoPandas, etc.)
call "%QGIS_ROOT%\bin\o4w_env.bat"

REM Ajouter les repertoires Qt au PATH pour que PyQt6 trouve ses DLLs
REM PyQt6 est livre avec QGIS mais ses DLLs Qt sont dans apps\Qt6\bin
if exist "%QGIS_ROOT%\apps\Qt6\bin" (
    set "PATH=%QGIS_ROOT%\apps\Qt6\bin;%QGIS_ROOT%\bin;%PATH%"
) else if exist "%QGIS_ROOT%\apps\Qt5\bin" (
    set "PATH=%QGIS_ROOT%\apps\Qt5\bin;%QGIS_ROOT%\bin;%PATH%"
) else (
    set "PATH=%QGIS_ROOT%\bin;%PATH%"
)

REM Lancer le launcher
cd /d "%~dp0"
python -m auvergne_pipeline.launcher
if errorlevel 1 (
    echo.
    echo [X] Erreur lors du lancement du launcher
    echo     Si l'erreur mentionne QtCore, verifie que le dossier apps\Qt6\bin existe.
    echo     Chemin QGIS : %QGIS_ROOT%
    pause
)
