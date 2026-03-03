@echo off
echo ==========================================
echo Arabic Subtitle AI - DEBUG VERSION
echo ==========================================
echo.
echo This version has EXTENSIVE LOGGING
echo to troubleshoot detection issues
echo.
echo 1. Run this batch file
echo 2. Web UI opens automatically  
echo 3. Click "🚨 Force Detect Now" button
echo 4. Check the console log for details
echo.
pause

python --version >nul 2>&1
if errorlevel 1 (
    echo ERROR: Python not found in PATH
    pause
    exit /b 1
)

echo.
echo Starting DEBUG server...
echo.

python arabic_subtitle_DEBUG.py

pause
