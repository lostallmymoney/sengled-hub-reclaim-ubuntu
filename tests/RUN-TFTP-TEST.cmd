@echo off
setlocal
cd /d "%~dp0\.."
title Sengled Reclaim - Production TFTP Integration Test

echo.
echo Sengled Reclaim - Production TFTP Integration Test
echo Tests the output tree, permissions, firewall rule, UDP/6969 server,
echo and production-sized transfers without contacting or modifying a hub.
echo.

powershell.exe -NoProfile -Command "$p=New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent()); if($p.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)){exit 0}else{exit 1}"
if errorlevel 1 (
    echo Requesting Administrator permission for the temporary UDP/TFTP firewall rule...
    powershell.exe -NoProfile -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
    exit /b
)

powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0Test-TftpServer.ps1" -Mode Test -KeepWork %*
set "RC=%ERRORLEVEL%"

echo.
if "%RC%"=="0" (
    echo Production TFTP integration test passed.
) else (
    echo Production TFTP integration test failed with exit code %RC%.
)
echo.
pause
exit /b %RC%
