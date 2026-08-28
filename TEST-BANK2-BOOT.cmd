@echo off
setlocal
cd /d "%~dp0"
title Sengled Reclaimed Bank2 Boot Test

echo.
echo Sengled reclaimed Bank2 boot and TCP/6638 health test
echo This mode does not back up, build, flash, or reboot anything.
echo.

powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0Reclaim-SengledHub.ps1" -BootTestOnly
set "RC=%ERRORLEVEL%"

echo.
if "%RC%"=="0" (
    echo Bank2 boot test finished successfully.
) else (
    echo Bank2 boot test stopped with exit code %RC%.
)
echo.
pause
exit /b %RC%
