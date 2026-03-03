@echo off
echo ==========================================
echo Arabic Subtitle AI - ENHANCED VERSION
echo ==========================================
echo.
echo Features:
echo   - Detects Stremio video automatically
echo   - Upload: .srt / .vtt / .sub / .ass / .ssa
echo   - Search SubDL + OpenSubtitles
echo   - Auto-translate to Arabic if needed
echo   - Auto-inject AWD_AR into Stremio
echo   - Clear Cache and Open Folders
echo.

python --version >nul 2>&1
if errorlevel 1 (
    echo ERROR: Python not found. Install from python.org
    pause
    exit /b 1
)

echo Checking dependencies...
pip install fastapi uvicorn requests pysrt --quiet --break-system-packages 2>nul

curl -s http://localhost:11434/api/tags >nul 2>&1
if errorlevel 1 (
    echo.
    echo WARNING: Ollama is NOT running!
    echo Translation will fail. Start Ollama and run: ollama pull llama3.1:8b
    echo.
)

echo.
echo Starting server at http://localhost:8000
echo.

python arabic_subtitle_SIMPLE.py

pause
