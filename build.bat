@echo off
rem Build MyBeatSaberStats by calling build.ps1.
rem Usage:
rem   build.bat                       build with the current version.json
rem   build.bat -Version 1.0.1        update version.json and build
rem   build.bat -Version 1.0.1 -Clean clean build (deletes build, dist and release)
setlocal

rem PyInstaller picks up Git's libssl/libcrypto if Git's mingw64\bin is in PATH.
rem Only the default install location (C:\Program Files\Git) is removed.
set "PATH=%PATH%;"
set "PATH=%PATH:C:\Program Files\Git\mingw64\bin;=%"
set "PATH=%PATH:C:\Program Files\Git\mingw64\bin\;=%"
set "PATH=%PATH:C:\Program Files\Git\usr\bin;=%"
set "PATH=%PATH:C:\Program Files\Git\usr\bin\;=%"

cd /d "%~dp0"
"%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe" -NoProfile -ExecutionPolicy Bypass -File "%~dp0build.ps1" %*
set "RC=%ERRORLEVEL%"

if not "%RC%"=="0" echo.
if not "%RC%"=="0" echo Build failed. exit code %RC%

rem Keep the window open only when started by double-click.
rem Explorer runs: cmd.exe /c ""<path>\build.bat" "
rem After removing quotes it ends with "build.bat " (name, space, end of line).
rem Terminal runs do not end that way, so they do not pause.
set "DOPAUSE="
setlocal EnableDelayedExpansion
set "CL=!CMDCMDLINE:"=!#"
set "HIT="
if /i not "!CL:\%~nx0 #=!"=="!CL!" set "HIT=1"
endlocal & set "DOPAUSE=%HIT%"
if defined DOPAUSE pause

endlocal & exit /b %RC%
