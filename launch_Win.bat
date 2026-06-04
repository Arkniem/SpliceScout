@echo off
setlocal enableextensions
title GEO RNA-seq Pipeline
cd /d "%~dp0"

echo ============================================================
echo   GEO RNA-seq Pipeline - launcher (Windows)
echo   First run installs Python + packages automatically.
echo ============================================================
echo.

REM ---- 1) Find a working Python (prefer the 'py' launcher; never a Store stub) ----
set "PY="
where py >nul 2>&1 && set "PY=py"
if not defined PY ( where python >nul 2>&1 && set "PY=python" )
if defined PY (
  %PY% -c "import sys" >nul 2>&1
  if errorlevel 1 set "PY="
)
if defined PY goto have_python

REM ---- Python missing: install the LATEST version available via winget ----
echo Python 3 was not found. Installing the latest version via winget...
where winget >nul 2>&1
if errorlevel 1 goto no_winget
powershell -NoProfile -ExecutionPolicy Bypass -Command "$out=(winget search --id Python.Python --source winget --accept-source-agreements | Out-String); $m=[regex]::Matches($out,'Python\.Python\.(\d+\.\d+)') | Sort-Object { [version]$_.Groups[1].Value } | Select-Object -Last 1; $id=if($m){$m.Value}else{'Python.Python.3.13'}; Write-Host ('Installing latest Python: ' + $id); winget install -e --id $id --accept-source-agreements --accept-package-agreements"
echo.
echo Python was installed. Please CLOSE this window and double-click launch_Win.bat again
echo so Windows can pick up the updated PATH.
echo.
pause
exit /b 0

:no_winget
echo.
echo winget is unavailable. Please install the latest Python 3 from:
echo     https://www.python.org/downloads/
echo IMPORTANT: tick "Add python.exe to PATH" during setup, then run launch_Win.bat again.
echo.
pause
exit /b 1

:have_python
echo Using Python: %PY%
echo.

REM ---- 2) Ensure required packages (installs only when something is missing) ----
%PY% -c "import anthropic, openai, openpyxl" >nul 2>&1
if errorlevel 1 (
  echo Installing required packages ^(first run only, needs internet^)...
  %PY% -m ensurepip >nul 2>&1
  %PY% -m pip install --upgrade pip >nul 2>&1
  %PY% -m pip install -r requirements.txt
  if errorlevel 1 (
    echo.
    echo Package install failed - check your internet connection and try again.
    echo.
    pause
    exit /b 1
  )
  echo Done installing.
  echo.
)

REM ---- 3) Optional: paramiko enables SSH *password* auth for the cluster upload ----
REM      (SSH key / agent auth works fine without it, so this never blocks launch)
%PY% -c "import paramiko" >nul 2>&1
if errorlevel 1 (
  echo Installing optional package 'paramiko' ^(SSH password auth^)...
  %PY% -m pip install paramiko >nul 2>&1
)

REM ---- 4) Launch the web UI (opens your browser) ----
echo Starting the web UI - a browser window will open shortly.
echo Keep this window open while you work; close it (or press Ctrl+C) to stop.
echo.
%PY% server.py

if errorlevel 1 (
  echo.
  echo The server exited with an error - see the messages above.
)
echo.
pause
