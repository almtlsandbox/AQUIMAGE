@echo off
rem ==================================================================
rem  AquImage - lanceur Windows
rem  1er lancement : cree l'environnement Python (hors OneDrive) et
rem  installe les dependances.  Ensuite : ouvre l'interface.
rem  Arguments transmis tels quels, ex. :
rem     AquImage.bat lot "D:\Plongees\Brut" "D:\Plongees\Corrigees"
rem ==================================================================
setlocal
chcp 65001 >nul
cd /d "%~dp0"
set "VENV=%LOCALAPPDATA%\AquImage\venv"
set "VPY=%VENV%\Scripts\python.exe"

if exist "%VPY%" goto run

echo Premiere installation d'AquImage (quelques minutes)...
call :findpy
if not defined PY (
  echo.
  echo Python est introuvable. Installez Python 3.9 ou plus recent depuis
  echo https://www.python.org/downloads/ puis relancez ce fichier.
  pause
  exit /b 1
)
echo Python utilise : %PY%
%PY% -m venv "%VENV%"
if errorlevel 1 goto err
"%VPY%" -m pip install --upgrade pip
"%VPY%" -m pip install -r requirements.txt
if errorlevel 1 goto err

:run
"%VPY%" -m aquimage %*
if errorlevel 1 pause
exit /b

:findpy
set "PY="
if exist "C:\Python\Python39\python.exe" set "PY="C:\Python\Python39\python.exe"" & exit /b
where py >nul 2>nul && set "PY=py -3" && exit /b
where python >nul 2>nul && set "PY=python" && exit /b
exit /b

:err
echo.
echo L'installation a echoue. Verifiez la connexion Internet puis relancez.
echo Pour repartir de zero, supprimez le dossier : %VENV%
pause
exit /b 1
