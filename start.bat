@echo off
chcp 65001 > nul
setlocal enabledelayedexpansion

REM =====================================================================
REM  Phase 1 : Trouver QGIS
REM =====================================================================
set "QGIS_PATH="

REM 1a. Variable d'environnement QGIS_ROOT (prioritaire)
if defined QGIS_ROOT (
    if exist "%QGIS_ROOT%\bin\o4w_env.bat" (
        set "QGIS_PATH=%QGIS_ROOT%"
    )
)

REM 1b. Chemins standards
if not defined QGIS_PATH (
    for %%d in (
        "C:\Program Files\QGIS 4.0.1"
        "C:\Program Files\QGIS 4.0.0"
        "C:\OSGeo4W"
        "D:\OSGeo4W"
    ) do if not defined QGIS_PATH (
        if exist "%%~d\bin\o4w_env.bat" set "QGIS_PATH=%%~d"
    )
)

REM 1c. Scan QGIS* (autres versions)
if not defined QGIS_PATH (
    for /d %%d in ("C:\Program Files\QGIS*") do if not defined QGIS_PATH (
        if exist "%%d\bin\o4w_env.bat" set "QGIS_PATH=%%d"
    )
)
if not defined QGIS_PATH (
    for /d %%d in ("D:\Program Files\QGIS*") do if not defined QGIS_PATH (
        if exist "%%d\bin\o4w_env.bat" set "QGIS_PATH=%%d"
    )
)

REM 1d. Echec detection
if not defined QGIS_PATH (
    echo [X] QGIS introuvable.
    echo     Chemins essayes : C:\Program Files\QGIS*, C:\OSGeo4W, D:\OSGeo4W
    echo     Definis QGIS_ROOT avant de lancer :
    echo       set QGIS_ROOT=D:\chemin\vers\QGIS
    echo       start.bat
    pause
    exit /b 1
)

echo [i] QGIS detecte : %QGIS_PATH%

REM =====================================================================
REM  Phase 2 : Initialiser l'environnement QGIS + Qt
REM =====================================================================

call "%QGIS_PATH%\bin\o4w_env.bat"

REM === Fix qgis.core access (PR #21) ===
set "PYTHONPATH=%QGIS_PATH%\apps\qgis\python;%QGIS_PATH%\apps\qgis\python\plugins;%PYTHONPATH%"
REM ====================================

REM Qt6 (prioritaire) ou Qt5 fallback
if exist "%QGIS_PATH%\apps\Qt6\bin" (
    set "PATH=%QGIS_PATH%\apps\Qt6\bin;%QGIS_PATH%\bin;%PATH%"
    set "QT_PLUGIN_PATH=%QGIS_PATH%\apps\Qt6\plugins"
) else if exist "%QGIS_PATH%\apps\Qt5\bin" (
    set "PATH=%QGIS_PATH%\apps\Qt5\bin;%QGIS_PATH%\bin;%PATH%"
    set "QT_PLUGIN_PATH=%QGIS_PATH%\apps\Qt5\plugins"
) else (
    set "PATH=%QGIS_PATH%\bin;%PATH%"
)

REM =====================================================================
REM  Phase 3 : Lancer le launcher
REM =====================================================================

cd /d "%~dp0"
echo [i] Lancement du launcher...
echo.
python -m auvergne_pipeline.launcher

REM Toujours faire pause, que le lancement reussisse ou echoue
echo.
if errorlevel 1 (
    echo [X] Erreur lors du lancement du launcher ^(code %errorlevel%^)
    echo     QGIS : %QGIS_PATH%
    dir "%QGIS_PATH%\apps\Qt6\plugins\platforms\qwindows*" 2>nul || (
        echo     [!] qwindows.dll introuvable dans plugins\platforms
    )
) else (
    echo [OK] Launcher termine.
)
pause
