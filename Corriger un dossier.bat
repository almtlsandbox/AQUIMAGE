@echo off
rem Corrige tout un dossier sans ouvrir l'interface.
rem Glissez-deposez un dossier sur ce fichier : les fichiers corriges vont
rem dans un sous-dossier "AquImage" du dossier depose.
setlocal
cd /d "%~dp0"
if "%~1"=="" (
  echo Glissez un dossier de photos sur ce fichier.
  pause
  exit /b 1
)
call "%~dp0AquImage.bat" lot "%~1" "%~1\AquImage"
pause
