@echo off
title Arabic Sub AI - System Check
color 0B
cls

:: Set Python path explicitly
set PYTHON_EXE=C:\Users\AWD\AppData\Local\Programs\Python\Python310\python.exe
set PYTHON_PIP=C:\Users\AWD\AppData\Local\Programs\Python\Python310\Scripts\pip.exe

echo ==========================================
echo   ARABIC SUBTITLE AI - SYSTEM CHECK
echo ==========================================
echo.

:: Verify Python 3.10 exists at specific path
echo [1/5] Checking Python 3.10 at:
echo      %PYTHON_EXE%
if exist "%PYTHON_EXE%" (
    color 0A
    echo      ✓ Python 3.10 found
    "%PYTHON_EXE%" --version
    echo.
    echo      Verifying it's NOT Windows Store version...
    "%PYTHON_EXE%" -c "import sys; print('Executable:', sys.executable)"
) else (
    color 0C
    echo      ✗ Python 3.10 NOT FOUND at that path!
    echo      Please verify the installation path.
    pause
    exit /b 1
)

echo.

:: Check packages
echo [2/5] Checking Python packages...
"%PYTHON_EXE%" -c "import fastapi, uvicorn, pysrt, psutil, requests, win32file" 2>nul
if %errorlevel% == 0 (
    echo      ✓ All packages installed
) else (
    echo      ⚠ Installing missing packages...
    echo      This may take a few minutes...
    "%PYTHON_PIP%" install fastapi==0.104.1 uvicorn==0.24.0 pysrt==1.1.2 psutil==5.9.6 requests==2.31.0 pywin32==306 pydantic==2.5.0
    if %errorlevel% neq 0 (
        echo      ✗ Installation failed! Trying alternative...
        "%PYTHON_EXE%" -m pip install fastapi==0.104.1 uvicorn==0.24.0 pysrt==1.1.2 psutil==5.9.6 requests==2.31.0 pywin32==306 pydantic==2.5.0
    )
)

echo.

:: Check FFmpeg
echo [3/5] Checking FFmpeg...
ffmpeg -version >nul 2>&1
if %errorlevel% == 0 (
    for /f "tokens=3" %%a in ('ffmpeg -version ^| findstr "ffmpeg version"') do (
        echo      ✓ FFmpeg found: %%a
    )
) else (
    color 0C
    echo      ✗ FFmpeg NOT FOUND in PATH
    echo      Install from: https://www.gyan.dev/ffmpeg/builds/
    pause
    exit /b 1
)

echo.

:: Check Ollama
echo [4/5] Checking Ollama AI...
curl -s http://localhost:11434/api/tags >nul 2>&1
if %errorlevel% == 0 (
    echo      ✓ Ollama is running
    curl -s http://localhost:11434/api/tags | findstr "llama3.1" >nul && (
        echo      ✓ Model llama3.1:8b found
    ) || (
        color 0E
        echo      ⚠ Model not found! Downloading...
        start cmd /k "ollama pull llama3.1:8b"
    )
) else (
    color 0E
    echo      ⚠ Ollama not running! Will auto-start.
)

echo.

:: Check Stremio Cache
echo [5/5] Checking Stremio Cache...
if exist "C:\Users\AWD\AppData\Roaming\stremio\stremio-server\stremio-cache" (
    echo      ✓ Cache directory accessible
    dir "C:\Users\AWD\AppData\Roaming\stremio\stremio-server\stremio-cache" 2>nul | find "File(s)" >nul && (
        echo      ✓ Cache contains files
    ) || (
        echo      ⚠ Cache empty (Start Stremio first)
    )
) else (
    color 0E
    echo      ⚠ Cache not found! Start Stremio to create it.
)

echo.
echo ==========================================
color 0A
echo   ALL CHECKS PASSED!
echo   Python: %PYTHON_EXE%
echo ==========================================
echo.
pause