@echo off
REM Start the at1 dashboard. Uses the dedicated venv outside OneDrive, because
REM syncing site-packages is slow and corruption-prone.
REM
REM Safe to double-click every morning: any server left running from yesterday
REM is stopped first, missing packages are installed, and the browser opens only
REM once the port actually answers. Anything that goes wrong stays on screen
REM instead of flashing past as the window closes.
REM
REM Override the port for a one-off run with:  set AT1_PORT=8778 && run.bat
REM To open it from a phone on the same Wi-Fi use run_mobile.bat, which sets
REM AT1_HOST=0.0.0.0 so the server listens on the LAN as well as localhost.
setlocal EnableDelayedExpansion

set "PY=%USERPROFILE%\.venvs\at1\Scripts\python.exe"
if not defined AT1_PORT set "AT1_PORT=8777"
if not defined AT1_HOST set "AT1_HOST=127.0.0.1"
set "URL=http://127.0.0.1:%AT1_PORT%"

cd /d "%~dp0"

if not exist "%PY%" (
  echo Could not find the at1 virtualenv at %PY%
  echo Create it with:  uv venv %USERPROFILE%\.venvs\at1 --python 3.14
  echo Then:            "%PY%" -m pip install -r requirements.txt
  goto :fail
)

REM --- Stop a server left over from a previous session ----------------------
REM Without this, uvicorn dies on bind with WinError 10048 and the window shuts
REM before the message is readable, which looks like the script doing nothing.
call :stop_listeners
if defined FOREIGN goto :foreign
for /l %%i in (1,1,20) do (
  call :first_listener
  if not defined LPID goto :port_free
  ping -n 2 127.0.0.1 >nul
  call :stop_listeners
  if defined FOREIGN goto :foreign
)
call :first_listener
if defined LPID (
  echo Port %AT1_PORT% is still busy after 20 attempts - PID !LPID! will not die.
  echo Reboot, or run on another port:  set AT1_PORT=8778 ^&^& run.bat
  goto :fail
)
:port_free

REM --- Check the app imports before handing it to uvicorn -------------------
REM Worth the few seconds: with --reload, an import error makes the reloader
REM retry in a loop forever rather than exiting, so the real traceback scrolls
REM away and the dashboard just never comes up.
"%PY%" -c "import app.main" 2>nul
if errorlevel 1 (
  echo Import failed on the first try - checking packages...
  "%PY%" -m pip install -q -r requirements.txt
  if errorlevel 1 (
    echo pip install failed - fix the error above, then run this again.
    goto :fail
  )
  "%PY%" -c "import app.main"
  if errorlevel 1 (
    echo.
    echo app\main.py still fails to import - see the traceback above.
    goto :fail
  )
)

REM --- Open the browser only after the server answers -----------------------
REM The old script opened it before uvicorn had bound, so a cold start showed a
REM connection error on a server that was about to come up fine.
start "" /b powershell -NoProfile -Command "for($i=0;$i -lt 90;$i++){try{$null=Invoke-WebRequest '%URL%' -UseBasicParsing -TimeoutSec 2;Start-Process '%URL%';break}catch{Start-Sleep -Milliseconds 500}}"

echo Starting at1 on %URL%  (Ctrl+C to stop)
if "%AT1_HOST%"=="0.0.0.0" (
  echo On your phone ^(same Wi-Fi^), open one of:
  for /f "tokens=2 delims=:" %%A in ('ipconfig ^| findstr /c:"IPv4"') do (
    for /f "tokens=*" %%B in ("%%A") do echo     http://%%B:%AT1_PORT%
  )
)
"%PY%" -m uvicorn app.main:app --host %AT1_HOST% --port %AT1_PORT% --reload --reload-dir app
set "RC=%ERRORLEVEL%"
if not "%RC%"=="0" (
  echo.
  echo at1 exited with code %RC%.
  goto :fail
)
endlocal
exit /b 0

:foreign
echo Port %AT1_PORT% is held by !FOREIGN!, which is not the at1 server.
echo Close that program, or run at1 on another port:
echo     set AT1_PORT=8778 ^&^& run.bat
goto :fail

:stop_listeners
REM Kills every process listening on AT1_PORT that looks like at1. --reload runs
REM a supervisor plus a worker and BOTH show as LISTENING, so killing just one
REM leaves the other holding the socket - or respawning the one we killed.
set "FOREIGN="
for /f "tokens=5" %%P in ('netstat -ano ^| findstr /r /c:":%AT1_PORT% .*LISTENING" 2^>nul') do call :stop_one %%P
exit /b 0

:stop_one
set "IMG="
for /f "tokens=1 delims=," %%I in ('tasklist /fi "PID eq %~1" /nh /fo csv 2^>nul') do set "IMG=%%~I"
if not defined IMG exit /b 0
REM tasklist puts "INFO: No tasks..." on stdout, not stderr, when the PID has
REM already exited - without this it gets mistaken for a process name.
if /i "!IMG:~0,5!"=="INFO:" exit /b 0
if /i "!IMG!"=="python.exe" (
  echo   stopping leftover at1 process, PID %~1
  taskkill /f /t /pid %~1 >nul 2>&1
  exit /b 0
)
set "FOREIGN=!IMG! PID %~1"
exit /b 0

:first_listener
set "LPID="
for /f "tokens=5" %%P in ('netstat -ano ^| findstr /r /c:":%AT1_PORT% .*LISTENING" 2^>nul') do set "LPID=%%P"
exit /b 0

:fail
echo.
pause
exit /b 1
