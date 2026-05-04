@echo off
chcp 65001 > nul
setlocal enabledelayedexpansion

REM --- Auto-detection de QGIS ---

REM 1. Variable d'environnement QGIS_ROOT
if defined QGIS_ROOT (
    if exist "%QGIS_ROOT%\bin\o4w_env.bat" (
        echo [i] QGIS detecte via QGIS_ROOT : %QGIS_ROOT%
        call "%QGIS_ROOT%\bin\o4w_env.bat"
        goto :launch
    )
)

REM 2. Chemins les plus frequents
set "TRIED="
for %%d in (
    "C:\Program Files\QGIS 4.0.1"
    "C:\Program Files\QGIS 4.0.0"
    "C:\OSGeo4W"
    "D:\OSGeo4W"
) do (
    if exist "%%~d\bin\o4w_env.bat" (
        echo [i] QGIS detecte : %%~d
        set "TRIED=%%~d"
        call "%%~d\bin\o4w_env.bat"
        goto :launch
    )
    set "TRIED=!TRIED!, %%~d"
)

REM 3. Scan C:\Program Files\QGIS*
for /d %%d in ("C:\Program Files\QGIS*") do (
    if exist "%%d\bin\o4w_env.bat" (
        echo [i] QGIS detecte : %%d
        call "%%d\bin\o4w_env.bat"
        goto :launch
    )
)

REM 4. Scan D:\Program Files\QGIS*
for /d %%d in ("D:\Program Files\QGIS*") do (
    if exist "%%d\bin\o4w_env.bat" (
        echo [i] QGIS detecte : %%d
        call "%%d\bin\o4w_env.bat"
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
REM o4w_env.bat charge Python + GeoPandas mais PAS les DLLs Qt.
REM PyQt6 (utilise par launcher.py) a besoin de apps\Qt6\bin dans le PATH.
REM On essaie le chemin detecte (TRIED ou QGIS_ROOT) + les chemins frequents.
for %%p in ("%TRIED%" "%QGIS_ROOT%"
            "C:\Program Files\QGIS 4.0.1"
            "C:\Program Files\QGIS 4.0.0"
            "C:\OSGeo4W"
            "D:\OSGeo4W") do (
    if exist "%%~p\apps\Qt6\bin" (
        set "PATH=%%~p\apps\Qt6\bin;%%~p\bin;%PATH%"
    )
)
REM Scan C:\Program Files\QGIS* pour Qt6 (cas rares)
for /d %%d in ("C:\Program Files\QGIS*") do (
    if exist "%%d\apps\Qt6\bin" set "PATH=%%d\apps\Qt6\bin;%%d\bin;%PATH%"
)

cd /d "%~dp0"
python -m auvergne_pipeline.launcher
if errorlevel 1 (
    echo.
    echo [X] Erreur lors du lancement du launcher
    pause
)
