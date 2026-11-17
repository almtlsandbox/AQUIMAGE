@echo off
rem ==================================================================
rem  AquImage - lanceur Windows
rem  1er lancement : trouve Python 3.9+ (ou l'installe sans question,
rem  pour cet utilisateur seulement), cree l'environnement (hors
rem  OneDrive) et installe les dependances.  Ensuite : ouvre l'interface.
rem  Arguments transmis tels quels, ex. :
rem     AquImage.bat lot "D:\Plongees\Brut" "D:\Plongees\Corrigees"
rem ==================================================================
setlocal
chcp 65001 >nul
cd /d "%~dp0"
set "HOMEAQ=%LOCALAPPDATA%\AquImage"
set "VENV=%HOMEAQ%\venv"
set "VPY=%VENV%\Scripts\python.exe"

rem Python installe par AquImage lui-meme (si aucun n'etait present)
set "PYDIR=%HOMEAQ%\python"
set "PYVER=3.12.10"
set "PYURL=https://www.python.org/ftp/python/%PYVER%/python-%PYVER%-amd64.exe"
set "PYMD5=5eddb0b6f12c852725de071ae681dde4"

if exist "%VPY%" goto run

echo Premiere installation d'AquImage (quelques minutes)...
if not exist "%HOMEAQ%" mkdir "%HOMEAQ%"
call :findpy
if not defined PY call :installpy
if not defined PY (
  echo.
  echo Python n'a pas pu etre installe automatiquement.
  echo Installez Python 3.9 ou plus recent depuis
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

rem ------------------------------------------------------------------
rem  Cherche un Python 3.9+ qui fonctionne vraiment.  Le faux
rem  "python" du Microsoft Store (qui ouvre le Store) est ecarte :
rem  on verifie que l'interpreteur repond et donne sa version.
rem ------------------------------------------------------------------
:findpy
set "PY="
call :trypy "%PYDIR%\python.exe"
if defined PY exit /b
call :trypy "C:\Python\Python39\python.exe"
if defined PY exit /b
where py >nul 2>nul
if not errorlevel 1 (
  py -3 -c "import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)" >nul 2>nul
  if not errorlevel 1 set "PY=py -3"
)
if defined PY exit /b
where python >nul 2>nul
if not errorlevel 1 (
  python -c "import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)" >nul 2>nul
  if not errorlevel 1 set "PY=python"
)
exit /b

:trypy
if not exist "%~1" exit /b
"%~1" -c "import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)" >nul 2>nul
if not errorlevel 1 set PY="%~1"
exit /b

rem ------------------------------------------------------------------
rem  Installe Python sans question ni droits administrateur, pour cet
rem  utilisateur seulement, dans %LOCALAPPDATA%\AquImage\python.  Rien
rem  n'est ajoute au PATH : les autres programmes ne sont pas touches.
rem  Desinstallable depuis Parametres > Applications.
rem ------------------------------------------------------------------
:installpy
set "PYEXE=%HOMEAQ%\python-%PYVER%-amd64.exe"
echo Python est introuvable : telechargement de Python %PYVER% (25 Mo)...
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$ProgressPreference='SilentlyContinue'; [Net.ServicePointManager]::SecurityProtocol=[Net.SecurityProtocolType]::Tls12;" ^
  "try { Invoke-WebRequest -UseBasicParsing -Uri '%PYURL%' -OutFile '%PYEXE%' } catch { exit 1 };" ^
  "if ((Get-FileHash -Algorithm MD5 '%PYEXE%').Hash -ne '%PYMD5%') { Remove-Item '%PYEXE%'; exit 2 }"
if errorlevel 2 (
  echo Le fichier telecharge est incorrect ^(somme de controle^).
  exit /b
)
if errorlevel 1 (
  echo Le telechargement a echoue. Verifiez la connexion Internet.
  exit /b
)
echo Installation de Python %PYVER% pour cet utilisateur...
"%PYEXE%" /quiet InstallAllUsers=0 "TargetDir=%PYDIR%" PrependPath=0 ^
  AssociateFiles=0 Shortcuts=0 Include_launcher=0 InstallLauncherAllUsers=0 ^
  Include_doc=0 Include_test=0 Include_tcltk=0 Include_dev=0 Include_pip=1 ^
  /log "%HOMEAQ%\python-install.log"
del "%PYEXE%" >nul 2>nul
call :trypy "%PYDIR%\python.exe"
if not defined PY echo L'installation de Python a echoue. Journal : %HOMEAQ%\python-install.log
exit /b

:err
echo.
echo L'installation a echoue. Verifiez la connexion Internet puis relancez.
echo Pour repartir de zero, supprimez le dossier : %VENV%
pause
exit /b 1
