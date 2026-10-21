@echo off
REM Start at1 and expose it through a Cloudflare quick tunnel, so a phone on
REM mobile data or another Wi-Fi can open it. Prints an https://*.trycloudflare.com
REM link - open that on the phone. The link changes on every run.
REM
REM WARNING: at1 has no login. Anyone who has the link can see the dashboard
REM while this window is open. Close it when you are done.
REM
REM Needs cloudflared once:  winget install --id Cloudflare.cloudflared
setlocal
if not defined AT1_PORT set "AT1_PORT=8777"

where cloudflared >nul 2>&1
if errorlevel 1 (
  echo cloudflared is not installed. Install it once with:
  echo     winget install --id Cloudflare.cloudflared
  echo then close this window and run run_remote.bat again.
  echo.
  pause
  exit /b 1
)

REM The server runs in its own window so its log and the tunnel log stay apart.
start "at1 server" cmd /c ""%~dp0run.bat""

echo Waiting for at1 to come up on port %AT1_PORT%...
powershell -NoProfile -Command "for($i=0;$i -lt 120;$i++){try{$null=Invoke-WebRequest 'http://127.0.0.1:%AT1_PORT%' -UseBasicParsing -TimeoutSec 2;exit 0}catch{Start-Sleep -Milliseconds 500}};exit 1"
if errorlevel 1 (
  echo at1 did not start - check the "at1 server" window for the error.
  echo.
  pause
  exit /b 1
)

echo.
echo Look below for a line like  https://something-random.trycloudflare.com
echo Open that link on your phone. Ctrl+C here closes the tunnel.
echo.
cloudflared tunnel --url http://127.0.0.1:%AT1_PORT%
endlocal
