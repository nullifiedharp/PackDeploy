@echo off
setlocal EnableDelayedExpansion
rem ============================================================
rem  PackDeploy build script
rem  Builds dist\PackDeploy.exe with PyInstaller, then compiles
rem  installer\PackDeploy-<version>-Setup.exe with Inno Setup 6.
rem  Usage:  build.bat            (version 1.1.0)
rem          build.bat 1.2.0      (custom version)
rem ============================================================
cd /d "%~dp0"

set "VERSION=%~1"
if "%VERSION%"=="" set "VERSION=1.1.0"

echo.
echo [1/3] Installing / updating PyInstaller...
python -m pip install --upgrade pyinstaller || goto :fail

echo.
echo [2/3] Building PackDeploy.exe...
python -m PyInstaller --noconfirm --clean --onefile --windowed ^
  --name PackDeploy ^
  --icon assets\icon.ico ^
  --add-data "assets\icon.ico;assets" ^
  PackDeploy.py || goto :fail

echo.
echo [3/3] Building installer...
call :find_iscc
if not defined ISCC (
  echo Inno Setup 6 not found - installing it with winget...
  winget install --id JRSoftware.InnoSetup -e --accept-source-agreements --accept-package-agreements
  call :find_iscc
)
if not defined ISCC (
  echo.
  echo Couldn't find Inno Setup. Install it from https://jrsoftware.org/isdl.php and run this again.
  goto :fail
)

"!ISCC!" /DAppVersion=%VERSION% installer.iss || goto :fail

echo.
echo ============================================================
echo  Done!
echo  Portable exe: dist\PackDeploy.exe
echo  Installer:    installer\PackDeploy-%VERSION%-Setup.exe
echo ============================================================
pause
exit /b 0

:find_iscc
set "ISCC="
for %%P in (
  "%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe"
  "%ProgramFiles%\Inno Setup 6\ISCC.exe"
  "%LOCALAPPDATA%\Programs\Inno Setup 6\ISCC.exe"
) do if exist %%P if not defined ISCC set "ISCC=%%~P"
if not defined ISCC for /f "delims=" %%I in ('where ISCC.exe 2^>nul') do if not defined ISCC set "ISCC=%%I"
exit /b 0

:fail
echo.
echo BUILD FAILED - see the error above.
pause
exit /b 1
