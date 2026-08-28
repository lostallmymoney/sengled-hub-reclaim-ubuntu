@echo off
setlocal
cd /d "%~dp0"
title Sengled Element Hub Reclaim - CHECK ONLY

echo.
echo Sengled Element Hub Reclaim - SAFE CHECK ONLY
echo This opens the stock backdoor, validates the hub layout, and saves metadata.
echo It does NOT flash the EM357 or RTL system flash.
echo.

powershell.exe -NoProfile -Command "$p=New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent()); if($p.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)){exit 0}else{exit 1}"
if errorlevel 1 (
    powershell.exe -NoProfile -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
    exit /b
)

powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0Reclaim-SengledHub.ps1" -DryRun
set "RC=%ERRORLEVEL%"

echo.
if "%RC%"=="0" (
    echo Safe check finished.
) else (
    echo Safe check stopped with exit code %RC%.
)
echo.
pause
exit /b %RC%
