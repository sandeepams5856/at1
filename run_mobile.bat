@echo off
REM Start at1 so a phone on the same Wi-Fi can open it. The first run may show a
REM Windows Firewall prompt for python.exe - allow it on Private networks.
set "AT1_HOST=0.0.0.0"
call "%~dp0run.bat"
