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

REM ---- 3b) Vendored Plotly for the Plots tab (download once if missing) ----
if not exist "%~dp0vendor\plotly.min.js" (
  echo Downloading the Plotly charting library ^(first run only, for the Plots tab^)...
  if not exist "%~dp0vendor" mkdir "%~dp0vendor"
  powershell -NoProfile -ExecutionPolicy Bypass -Command "try{ [Net.ServicePointManager]::SecurityProtocol=[Net.SecurityProtocolType]::Tls12; Invoke-WebRequest -Uri 'https://cdn.plot.ly/plotly-2.35.2.min.js' -OutFile '%~dp0vendor\plotly.min.js' }catch{ Write-Host 'Plotly download failed - the Plots tab will need internet once.' }"
)

REM ---- 3c) Optional: tray libraries (pystray + Pillow) for the system-tray launcher ----
%PY% -c "import pystray, PIL" >nul 2>&1
if errorlevel 1 (
  echo Installing tray support ^(pystray, Pillow^) for the system-tray icon...
  %PY% -m pip install pystray Pillow >nul 2>&1
)

REM ---- 4) Launch: prefer the system tray (no console window); fall back to a console window ----
set "PYW=pythonw"
if /I "%PY%"=="py" set "PYW=pyw"
%PY% -c "import pystray, PIL" >nul 2>&1
if errorlevel 1 (
  echo Starting the web UI in this window ^(tray libraries unavailable^).
  echo Keep this window open while you work; close it ^(or press Ctrl+C^) to stop.
  echo TIP: run launch_Win.bat again for ANOTHER instance ^(its own port + JOB_TAG sra1/sra2/...^).
  echo.
  %PY% server.py
  if errorlevel 1 (
    echo.
    echo The server exited with an error - see the messages above.
  )
  echo.
  pause
) else (
  echo Starting SpliceScout in the system tray - look for the blue "S" icon near the clock.
  echo Right-click it for Open / Quit. You can close this window. Run launch_Win.bat again for
  echo another concurrent instance ^(its own port + JOB_TAG sra1/sra2/...^).
  start "SpliceScout" %PYW% "%~dp0tray.py"
  timeout /t 4 >nul
)
