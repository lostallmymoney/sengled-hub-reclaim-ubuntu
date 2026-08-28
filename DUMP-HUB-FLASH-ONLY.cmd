@echo off
setlocal
cd /d "%~dp0"
title Sengled Element Hub Reclaim - READ-ONLY FLASH BACKUP

echo.
echo Sengled Element Hub - READ-ONLY FLASH BACKUP
echo This validates the hub and copies all four RTL flash partitions to this PC.
echo It does NOT probe or flash the coordinator, build images, write RTL flash, or reboot.
echo.

powershell.exe -NoProfile -Command "$p=New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent()); if($p.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)){exit 0}else{exit 1}"
if errorlevel 1 (
    echo Requesting Administrator permission for the temporary UDP/TFTP firewall rule...
    powershell.exe -NoProfile -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
    exit /b
)

powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0Reclaim-SengledHub.ps1" -BackupOnly
set "RC=%ERRORLEVEL%"

echo.
if "%RC%"=="0" (
    echo Read-only flash backup finished.
) else (
    echo Flash backup stopped with exit code %RC%.
)
echo.
pause
exit /b %RC%
