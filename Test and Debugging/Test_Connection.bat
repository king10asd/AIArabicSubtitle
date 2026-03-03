@echo off
title Test Arabic Sub Server
color 0E
cls

echo Testing server at localhost:8000...
echo.

curl -s http://localhost:8000/health >nul 2>&1
if %errorlevel% == 0 (
    color 0A
    echo ✓ SERVER IS RUNNING!
    echo.
    echo Health check:
    curl -s http://localhost:8000/health
    echo.
    echo.
    echo Available subtitles:
    curl -s http://localhost:8000/subtitles
    echo.
) else (
    color 0C
    echo ✗ SERVER NOT RESPONDING at port 8000
    echo.
    echo Possible fixes:
    echo 1. Run START_SERVER.bat first
    echo 2. Check if port 8000 is blocked by firewall
    echo 3. Check Windows Defender (add exclusion)
    echo.
    echo Trying to restart...
    timeout /t 3 >nul
    start START_SERVER.bat
)

echo.
pause