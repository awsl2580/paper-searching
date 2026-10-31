@echo off
rem ===================================================================
rem  One-click launcher (Windows). Pure ASCII on purpose:
rem  cmd.exe parses .bat using the current codepage and requires CRLF,
rem  so non-ASCII text here would break into "not recognized as a
rem  command" errors. All Chinese UI lives in launch.py instead.
rem
rem  What this file does: find a Python 3.10+, then let bootstrap_env.py
rem  create or re-point .venv at that Python (moved folder / new host),
rem  then hand over to launch.py (deps install + env check + start).
rem ===================================================================
setlocal
cd /d "%~dp0"
title AI4S Science Agent

echo ==================================================================
echo   AI4S Science Agent - launcher
echo   Folder: %CD%
echo ==================================================================
echo.

set "PYEXE="
if not defined PYEXE ( py -3.13 -V >nul 2>&1 && set "PYEXE=py -3.13" )
if not defined PYEXE ( py -3.12 -V >nul 2>&1 && set "PYEXE=py -3.12" )
if not defined PYEXE ( py -3.11 -V >nul 2>&1 && set "PYEXE=py -3.11" )
if not defined PYEXE ( python    -V >nul 2>&1 && set "PYEXE=python" )
if not defined PYEXE ( py -3     -V >nul 2>&1 && set "PYEXE=py -3" )
if not defined PYEXE ( py -3.10  -V >nul 2>&1 && set "PYEXE=py -3.10" )
if not defined PYEXE (
    echo [ERROR] Python not found.
    echo.
    echo   Install Python 3.12 or 3.13: https://www.python.org/downloads/
    echo   Tick "Add python.exe to PATH" during setup, then run this file again.
    echo.
    pause
    exit /b 1
)
for /f "delims=" %%v in ('%PYEXE% -V 2^>^&1') do echo Using %%v  ^(%PYEXE%^)

rem  Let bootstrap_env.py create/repair .venv against the Python found above.
rem  It rewrites pyvenv.cfg when the project was moved, and rebuilds only
rem  when the host Python's major.minor changed.
%PYEXE% "scripts\bootstrap_env.py"
if errorlevel 1 (
    echo [ERROR] Could not prepare .venv.
    echo         Close any running server, delete the .venv folder, and retry.
    pause
    exit /b 1
)

set "VPY=%CD%\.venv\Scripts\python.exe"
if not exist "%VPY%" (
    echo [ERROR] .venv is broken. Delete the .venv folder and run this file again.
    pause
    exit /b 1
)

"%VPY%" "scripts\launch.py"
set "RC=%ERRORLEVEL%"
echo.
if not "%RC%"=="0" echo Exit code: %RC%  - see messages above or ????.txt
pause
exit /b %RC%
