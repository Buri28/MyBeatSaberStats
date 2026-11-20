@echo off
rem Run MyBeatSaberStats from source (src\) with the .venv Python.
rem Usage: run.bat
setlocal

cd /d "%~dp0"
set "PY=%~dp0.venv\Scripts\python.exe"
if not exist "%PY%" (
    echo .venv not found. Create it and install requirements.txt first.
    set "RC=1"
    goto :done
)

"%PY%" "%~dp0player_main.py" %*
set "RC=%ERRORLEVEL%"
if not "%RC%"=="0" echo.
if not "%RC%"=="0" echo App exited with code %RC%

:done
rem Pause only when started by double-click and the app failed.
rem Explorer runs: cmd.exe /c ""<path>\run.bat" "
set "DOPAUSE="
setlocal EnableDelayedExpansion
set "CL=!CMDCMDLINE:"=!#"
set "HIT="
if /i not "!CL:\%~nx0 #=!"=="!CL!" set "HIT=1"
endlocal & set "DOPAUSE=%HIT%"
if defined DOPAUSE if not "%RC%"=="0" pause

endlocal & exit /b %RC%
