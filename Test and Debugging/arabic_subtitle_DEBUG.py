#!/usr/bin/env python3
"""
Arabic Subtitle AI for Stremio - DEBUG VERSION
Extensive logging to troubleshoot detection issues
"""

import os
import sys
import json
import time
import shutil
import hashlib
import zipfile
import requests
import subprocess
import webbrowser
from pathlib import Path
from datetime import datetime
from typing import Optional, List, Dict, Tuple
from dataclasses import dataclass, field
from threading import Thread, Lock
import logging
import re

try:
    import win32file
    import win32con
    import msvcrt
    WINDOWS = True
except ImportError:
    WINDOWS = False

import pysrt
from fastapi import FastAPI, File, UploadFile, HTTPException, BackgroundTasks, Query
from fastapi.responses import FileResponse, JSONResponse, HTMLResponse
import uvicorn

# DEBUG LOGGING
logging.basicConfig(
    level=logging.DEBUG,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("arabic_subtitle_debug.log", encoding='utf-8')
    ]
)
logger = logging.getLogger(__name__)

@dataclass
class Config:
    STREMIO_CACHE_PATHS: List[Path] = field(default_factory=lambda: [
        Path.home() / "AppData/Roaming/stremio/stremio-server/stremio-cache",
        Path.home() / ".stremio-server/stremio-cache",
        Path("C:/stremio-cache"),
        Path("D:/stremio-cache"),
        Path("E:/stremio-cache"),
    ])
    TEMP_DIR: Path = Path("C:/temp/arabic_subs")
    OLLAMA_URL: str = "http://localhost:11434/api/generate"
    MODEL: str = "llama3.1:8b"
    PORT: int = 8000

    SUBDL_API_KEY: str = "sZZkTyFFMdUSPfjBb_s_5dNlfS_O3PzT"
    OPENSUBTITLES_API_KEY: str = "f22fr0hgPZzYk7DfZfg1MfIoVFRJvSqY"
    OPENSUBTITLES_JWT: str = "eyJ0eXAiOiJKV1QiLCJhbGciOiJIUzI1NiJ9.eyJpc3MiOiJmMjJmcjBoZ1BaellrN0RmWmZnMU1mSW9WRlJKdlNxWSIsImV4cCI6MTc3MjU4MDU0OX0.1-Cp7ejxgvmxdmZo0eYWCVu5NJdcv_NzTDbTbPb_zMU"
    SUBDL_BASE_URL: str = "https://api.subdl.com/api/v1"
    OPENSUBTITLES_BASE_URL: str = "https://api.opensubtitles.com/api/v1"

CONFIG = Config()

class TranslationState:
    def __init__(self):
        self.lock = Lock()
        self.progress: Dict = {}
        self.active_video_hash: Optional[str] = None
        self.active_cache_folder: Optional[Path] = None
        self.stremio_cache_path: Optional[Path] = None

    def update_progress(self, job_id: str, stage: str, percent: int, message: str, details: dict = None):
        with self.lock:
            self.progress[job_id] = {
                "stage": stage,
                "percent": min(100, max(0, percent)),
                "message": message,
                "details": details or {},
                "timestamp": datetime.now().isoformat()
            }
            logger.info(f"[{job_id}] {stage}: {percent}% - {message}")

    def get_progress(self, job_id: str) -> Optional[Dict]:
        with self.lock:
            return self.progress.get(job_id)

    def set_active_video(self, video_hash: str, cache_folder: Path):
        with self.lock:
            self.active_video_hash = video_hash
            self.active_cache_folder = cache_folder

    def get_active_video(self) -> Tuple[Optional[str], Optional[Path]]:
        with self.lock:
            return self.active_video_hash, self.active_cache_folder

state = TranslationState()
app = FastAPI(title="Arabic Subtitle AI - DEBUG")
CONFIG.TEMP_DIR.mkdir(parents=True, exist_ok=True)

def is_stremio_video_folder(folder: Path) -> bool:
    """Detect Stremio cache folder with logging"""
    try:
        if not folder.is_dir():
            logger.debug(f"  {folder.name}: Not a directory")
            return False

        files = list(folder.iterdir())
        file_names = [f.name for f in files if f.is_file()]

        logger.debug(f"  Checking folder: {folder.name}")
        logger.debug(f"    Files found: {file_names}")

        numbered_files = [name for name in file_names if name.isdigit()]
        has_numbered = len(numbered_files) > 0
        has_cache = "cache" in file_names
        has_bitfield = "bitfield" in file_names

        logger.debug(f"    Numbered files ({len(numbered_files)}): {numbered_files[:5]}")
        logger.debug(f"    Has cache: {has_cache}")
        logger.debug(f"    Has bitfield: {has_bitfield}")

        is_valid = has_numbered and has_cache

        if is_valid:
            logger.info(f"  ✅ VALID Stremio folder: {folder.name}")
        else:
            logger.debug(f"  ❌ Invalid: numbered={has_numbered}, cache={has_cache}")

        return is_valid

    except Exception as e:
        logger.error(f"Error checking folder {folder}: {e}")
        return False

def find_stremio_cache() -> Optional[Path]:
    """Find cache with logging"""
    logger.debug("Searching for Stremio cache...")

    config_paths = [
        Path.home() / "AppData/Roaming/stremio/stremio-server/server-settings.json",
        Path.home() / ".stremio-server/server-settings.json",
    ]

    for config_path in config_paths:
        logger.debug(f"  Checking config: {config_path}")
        if config_path.exists():
            try:
                with open(config_path, 'r') as f:
                    settings = json.load(f)
                    cache_root = settings.get("cacheRoot")
                    logger.debug(f"    Config found, cacheRoot: {cache_root}")
                    if cache_root:
                        cache_path = Path(cache_root)
                        if cache_path.exists():
                            logger.info(f"✅ Cache from config: {cache_path}")
                            return cache_path
            except Exception as e:
                logger.error(f"    Config error: {e}")

    for cache_path in CONFIG.STREMIO_CACHE_PATHS:
        logger.debug(f"  Checking path: {cache_path}")
        if cache_path.exists():
            logger.info(f"✅ Cache at default: {cache_path}")
            return cache_path
        else:
            logger.debug(f"    Path does not exist")

    logger.warning("❌ No cache found!")
    return None

def get_folder_size(folder: Path) -> int:
    try:
        total = 0
        for f in folder.iterdir():
            if f.is_file():
                total += f.stat().st_size
        return total
    except:
        return 0

def manual_check_cache() -> Dict:
    """Manual check for debug"""
    logger.info("=" * 60)
    logger.info("MANUAL CACHE CHECK")
    logger.info("=" * 60)

    result = {
        "cache_found": False,
        "cache_path": None,
        "folders_found": 0,
        "video_folders": [],
        "errors": []
    }

    try:
        cache_path = find_stremio_cache()

        if not cache_path:
            result["errors"].append("Cache path not found")
            return result

        result["cache_found"] = True
        result["cache_path"] = str(cache_path)
        state.stremio_cache_path = cache_path

        folders = [f for f in cache_path.iterdir() if f.is_dir()]
        result["folders_found"] = len(folders)
        logger.info(f"Total folders in cache: {len(folders)}")

        video_folders = []
        for folder in folders:
            if is_stremio_video_folder(folder):
                try:
                    mtime = folder.stat().st_mtime
                    size = get_folder_size(folder)
                    video_folders.append({
                        "name": folder.name,
                        "path": str(folder),
                        "mtime": mtime,
                        "size_mb": round(size / 1024 / 1024, 2),
                        "age_seconds": int(time.time() - mtime)
                    })
                except Exception as e:
                    logger.error(f"Error stat folder {folder}: {e}")

        video_folders.sort(key=lambda x: x["mtime"], reverse=True)
        result["video_folders"] = video_folders

        logger.info(f"Video folders detected: {len(video_folders)}")
        for vf in video_folders[:3]:
            logger.info(f"  - {vf['name'][:30]}... ({vf['size_mb']} MB, {vf['age_seconds']}s ago)")

        if video_folders:
            newest = video_folders[0]
            state.set_active_video(newest["name"], Path(newest["path"]))
            logger.info(f"✅ ACTIVE VIDEO SET: {newest['name']}")
        else:
            logger.warning("No video folders found!")

    except Exception as e:
        logger.error(f"Manual check error: {e}")
        result["errors"].append(str(e))

    logger.info("=" * 60)
    return result

def watch_stremio_cache():
    """Background watcher"""
    last_folder = None

    logger.info("🔍 Watcher started")

    while True:
        try:
            cache_path = find_stremio_cache()

            if cache_path and cache_path.exists():
                state.stremio_cache_path = cache_path
                folders = [f for f in cache_path.iterdir() if f.is_dir()]

                if folders:
                    video_folders = []
                    for folder in folders:
                        if is_stremio_video_folder(folder):
                            try:
                                mtime = folder.stat().st_mtime
                                size = get_folder_size(folder)
                                video_folders.append((folder, mtime, size))
                            except:
                                pass

                    if video_folders:
                        video_folders.sort(key=lambda x: x[1], reverse=True)
                        newest_folder, mtime, size = video_folders[0]

                        if newest_folder != last_folder:
                            size_mb = size / 1024 / 1024
                            logger.info(f"🎬 NEW VIDEO: {newest_folder.name} ({size_mb:.1f} MB)")
                            state.set_active_video(newest_folder.name, newest_folder)
                            last_folder = newest_folder
            else:
                if state.stremio_cache_path:
                    logger.warning("Cache path lost")
                    state.stremio_cache_path = None

        except Exception as e:
            logger.error(f"Watcher error: {e}")

        time.sleep(5)

HTML_TEMPLATE = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Arabic Subtitle AI - DEBUG MODE</title>
    <style>
        * { box-sizing: border-box; margin: 0; padding: 0; }
        body {
            font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif;
            background: linear-gradient(135deg, #1e3c72 0%, #2a5298 100%);
            min-height: 100vh;
            color: #fff;
            padding: 20px;
        }
        .container { max-width: 1200px; margin: 0 auto; }
        h1 { text-align: center; margin-bottom: 10px; font-size: 2.5em; text-shadow: 2px 2px 4px rgba(0,0,0,0.3); }
        .subtitle { text-align: center; margin-bottom: 30px; opacity: 0.9; color: #ff9800; font-weight: bold; }
        .status-box {
            background: rgba(255,255,255,0.1);
            backdrop-filter: blur(10px);
            border-radius: 15px;
            padding: 20px;
            margin-bottom: 20px;
            border: 1px solid rgba(255,255,255,0.2);
        }
        .status-header {
            display: flex;
            justify-content: space-between;
            align-items: center;
            margin-bottom: 15px;
        }
        .status-indicator {
            display: inline-flex;
            align-items: center;
            gap: 8px;
            padding: 8px 16px;
            border-radius: 20px;
            font-weight: bold;
            font-size: 0.9em;
        }
        .status-active { background: #4CAF50; color: white; }
        .status-inactive { background: #f44336; color: white; }
        .status-waiting { background: #ff9800; color: white; }
        .pulse {
            width: 10px;
            height: 10px;
            background: white;
            border-radius: 50%;
            animation: pulse 2s infinite;
        }
        @keyframes pulse {
            0% { opacity: 1; }
            50% { opacity: 0.5; }
            100% { opacity: 1; }
        }
        .video-info {
            background: rgba(0,0,0,0.2);
            padding: 15px;
            border-radius: 10px;
            font-family: monospace;
            font-size: 0.9em;
            line-height: 1.6;
        }
        .debug-info {
            background: rgba(255,0,0,0.1);
            border: 1px solid rgba(255,0,0,0.3);
            padding: 15px;
            border-radius: 10px;
            margin-top: 15px;
            font-family: monospace;
            font-size: 0.8em;
            max-height: 300px;
            overflow-y: auto;
        }
        .btn {
            background: #4CAF50;
            color: white;
            border: none;
            padding: 12px 30px;
            border-radius: 25px;
            cursor: pointer;
            font-size: 1em;
            font-weight: bold;
            transition: all 0.3s;
            margin: 5px;
        }
        .btn:hover { background: #45a049; transform: translateY(-2px); box-shadow: 0 5px 15px rgba(0,0,0,0.3); }
        .btn-secondary { background: #2196F3; }
        .btn-secondary:hover { background: #0b7dda; }
        .btn-warning { background: #ff9800; }
        .btn:disabled { background: #666; cursor: not-allowed; }
        .btn-danger { background: #f44336; }
        .upload-area {
            background: rgba(255,255,255,0.1);
            backdrop-filter: blur(10px);
            border-radius: 15px;
            padding: 40px;
            margin-bottom: 20px;
            border: 2px dashed rgba(255,255,255,0.3);
            text-align: center;
        }
        .hidden-file-input { display: none; }
        .progress-container {
            background: rgba(255,255,255,0.1);
            backdrop-filter: blur(10px);
            border-radius: 15px;
            padding: 20px;
            margin-bottom: 20px;
            display: none;
        }
        .progress-bar {
            width: 100%;
            height: 30px;
            background: rgba(0,0,0,0.3);
            border-radius: 15px;
            overflow: hidden;
            margin: 10px 0;
        }
        .progress-fill {
            height: 100%;
            background: linear-gradient(90deg, #4CAF50, #8BC34A);
            transition: width 0.5s;
            display: flex;
            align-items: center;
            justify-content: center;
            font-weight: bold;
        }
        .log-box {
            background: rgba(0,0,0,0.5);
            border-radius: 10px;
            padding: 15px;
            height: 200px;
            overflow-y: auto;
            font-family: monospace;
            font-size: 0.85em;
            margin-top: 20px;
        }
        .log-entry { margin-bottom: 5px; padding: 3px 0; border-bottom: 1px solid rgba(255,255,255,0.1); }
        .success-msg { color: #4CAF50; font-weight: bold; }
        .error-msg { color: #f44336; font-weight: bold; }
    </style>
</head>
<body>
    <div class="container">
        <h1>🎬 Arabic Subtitle AI</h1>
        <p class="subtitle">⚠️ DEBUG MODE - Verbose Logging Enabled</p>

        <div class="status-box">
            <div class="status-header">
                <h2>📺 Stremio Status</h2>
                <span id="status-badge" class="status-indicator status-waiting">
                    <span class="pulse" style="display:none"></span>
                    <span id="status-text">Checking...</span>
                </span>
            </div>
            <div class="video-info" id="video-info">
                <p>Initializing...</p>
            </div>
            <div class="debug-info" id="debug-info" style="display:none;">
                <strong>Debug Info:</strong>
                <pre id="debug-content"></pre>
            </div>
        </div>

        <div class="status-box">
            <h3>🔧 Debug Controls</h3>
            <button class="btn btn-danger" onclick="forceDetect()">🚨 Force Detect Now</button>
            <button class="btn btn-secondary" onclick="showDebugInfo()">📋 Show Debug Info</button>
            <button class="btn btn-warning" onclick="refreshStatus()">🔄 Refresh Status</button>
            <button class="btn btn-secondary" onclick="openFolder()">📂 Open Temp</button>
        </div>

        <div class="upload-area" id="upload-area">
            <h3>📁 Upload Subtitle File</h3>
            <p style="margin:15px 0; opacity:0.8;">Drag & drop your .srt file here</p>
            <input type="file" id="file-input" class="hidden-file-input" accept=".srt,.ass,.ssa,.vtt">
            <button class="btn" id="upload-btn" onclick="document.getElementById('file-input').click()" disabled>
                Waiting for video...
            </button>
        </div>

        <div class="progress-container" id="progress-container">
            <h3>🔄 Processing</h3>
            <div class="progress-bar">
                <div class="progress-fill" id="progress-fill" style="width:0%">0%</div>
            </div>
            <p id="progress-message">Starting...</p>
        </div>

        <div class="log-box" id="log-box">
            <div class="log-entry">System initialized in DEBUG mode...</div>
        </div>
    </div>

    <script>
        let currentJobId = null;

        window.onload = function() {
            refreshStatus();
            setInterval(refreshStatus, 3000);
            log("DEBUG mode active - Check console for verbose logs");
        };

        function log(msg) {
            const box = document.getElementById("log-box");
            const entry = document.createElement("div");
            entry.className = "log-entry";
            entry.textContent = "[" + new Date().toLocaleTimeString() + "] " + msg;
            box.insertBefore(entry, box.firstChild);
        }

        async function refreshStatus() {
            try {
                const res = await fetch("/health");
                const data = await res.json();

                const badge = document.getElementById("status-badge");
                const text = document.getElementById("status-text");
                const pulse = badge.querySelector(".pulse");
                const info = document.getElementById("video-info");
                const uploadBtn = document.getElementById("upload-btn");

                if (data.active_video_hash) {
                    badge.className = "status-indicator status-active";
                    text.textContent = "Video Active";
                    pulse.style.display = "inline-block";
                    uploadBtn.disabled = false;
                    uploadBtn.textContent = "Choose File";
                    info.innerHTML = "<p class=\"success-msg\">✅ VIDEO DETECTED</p>" +
                        "<p><strong>Hash:</strong> " + data.active_video_hash + "</p>" +
                        "<p><strong>Folder:</strong> " + data.active_cache_folder + "</p>" +
                        "<p><strong>Cache:</strong> " + (data.cache_path || "Unknown") + "</p>";
                } else {
                    badge.className = "status-indicator status-inactive";
                    text.textContent = "No Video Detected";
                    pulse.style.display = "none";
                    uploadBtn.disabled = true;
                    uploadBtn.textContent = "Waiting for video...";
                    info.innerHTML = "<p class=\"error-msg\">❌ No video detected</p>" +
                        "<p>Cache: " + (data.cache_path || "Unknown") + "</p>" +
                        "<p style=\"margin-top:10px; font-size:0.8em;\">" +
                        "Click <strong>🚨 Force Detect Now</strong> to manually scan</p>";
                }
            } catch (e) {
                console.error(e);
                log("Status check failed: " + e.message);
            }
        }

        async function forceDetect() {
            log("🚨 FORCE DETECT triggered...");
            try {
                const res = await fetch("/force-detect");
                const data = await res.json();

                log("Force detect: cache=" + data.cache_found + ", videos=" + data.video_folders.length);

                if (data.cache_found) {
                    log("✅ Cache: " + data.cache_path);
                    log("📁 Folders: " + data.folders_found);
                    if (data.video_folders.length > 0) {
                        log("🎬 First: " + data.video_folders[0].name.substring(0, 30));
                    }
                } else {
                    log("❌ Cache not found");
                }

                const debugDiv = document.getElementById("debug-info");
                const debugContent = document.getElementById("debug-content");
                debugContent.textContent = JSON.stringify(data, null, 2);
                debugDiv.style.display = "block";

                refreshStatus();
            } catch (e) {
                log("Force detect failed: " + e.message);
            }
        }

        function showDebugInfo() {
            forceDetect();
        }

        async function uploadFile(file) {
            if (!file.name.match(/\.(srt|ass|ssa|vtt)$/i)) {
                alert("Invalid file type");
                return;
            }
            const formData = new FormData();
            formData.append("file", file);
            try {
                log("Uploading: " + file.name);
                const res = await fetch("/upload?auto_translate=true", {
                    method: "POST",
                    body: formData
                });
                const data = await res.json();
                if (data.job_id) {
                    currentJobId = data.job_id;
                    showProgress();
                }
            } catch (e) {
                log("Upload failed");
            }
        }

        function showProgress() {
            document.getElementById("progress-container").style.display = "block";
            const interval = setInterval(async () => {
                if (!currentJobId) return;
                try {
                    const res = await fetch("/progress/" + currentJobId);
                    const data = await res.json();
                    document.getElementById("progress-fill").style.width = data.percent + "%";
                    document.getElementById("progress-fill").textContent = data.percent + "%";
                    document.getElementById("progress-message").textContent = data.message;
                    if (data.percent >= 100) {
                        clearInterval(interval);
                        log("✅ Complete!");
                    }
                } catch (e) {
                    console.error(e);
                }
            }, 1000);
        }

        function openFolder() {
            fetch("/open");
            log("Opening temp folder...");
        }

        const uploadArea = document.getElementById("upload-area");
        const fileInput = document.getElementById("file-input");

        uploadArea.addEventListener("dragover", e => {
            e.preventDefault();
            uploadArea.style.borderColor = "#4CAF50";
        });
        uploadArea.addEventListener("dragleave", () => {
            uploadArea.style.borderColor = "rgba(255,255,255,0.3)";
        });
        uploadArea.addEventListener("drop", e => {
            e.preventDefault();
            uploadArea.style.borderColor = "rgba(255,255,255,0.3)";
            if (e.dataTransfer.files.length) uploadFile(e.dataTransfer.files[0]);
        });
        fileInput.addEventListener("change", e => {
            if (e.target.files.length) uploadFile(e.target.files[0]);
        });
    </script>
</body>
</html>
"""

import io

class OpenSubtitlesAPI:
    HEADERS = {
        "Api-Key": CONFIG.OPENSUBTITLES_API_KEY,
        "Authorization": f"Bearer {CONFIG.OPENSUBTITLES_JWT}",
        "Content-Type": "application/json",
        "User-Agent": "ArabicSubtitleAI/1.0"
    }

    @classmethod
    def search(cls, query: str = None, language: str = "ar") -> List[Dict]:
        try:
            url = f"{CONFIG.OPENSUBTITLES_BASE_URL}/subtitles"
            params = {"languages": language}
            if query:
                params["query"] = query
            res = requests.get(url, headers=cls.HEADERS, params=params, timeout=10)
            res.raise_for_status()
            data = res.json()
            results = []
            for item in data.get("data", []):
                attr = item.get("attributes", {})
                files = attr.get("files", [])
                if files:
                    results.append({
                        "id": item.get("id"),
                        "language": attr.get("language"),
                        "title": attr.get("feature_details", {}).get("title", "Unknown"),
                        "file_id": files[0].get("file_id"),
                        "source": "opensubtitles"
                    })
            return results
        except Exception as e:
            logger.error(f"OS search error: {e}")
            return []

class SubDLAPI:
    @classmethod
    def search(cls, film_name: str = None, languages: str = "AR,EN") -> List[Dict]:
        try:
            url = f"{CONFIG.SUBDL_BASE_URL}/subtitles"
            params = {"api_key": CONFIG.SUBDL_API_KEY, "languages": languages, "subs_per_page": 30}
            if film_name:
                params["film_name"] = film_name
            res = requests.get(url, params=params, timeout=10)
            res.raise_for_status()
            data = res.json()
            results = []
            if data.get("status") and "subtitles" in data:
                for sub in data["subtitles"]:
                    results.append({
                        "id": sub.get("url", "").split("/")[-1].replace(".zip", ""),
                        "language": sub.get("lang", "unknown"),
                        "release_name": sub.get("release_name", "Unknown"),
                        "download_link": sub.get("download_link"),
                        "source": "subdl"
                    })
            return results
        except Exception as e:
            logger.error(f"SUBDL search error: {e}")
            return []

def translate_subtitles(srt_path: Path, job_id: str) -> Optional[Path]:
    try:
        subs = pysrt.open(str(srt_path), encoding='utf-8', errors='ignore')
        total = len(subs)
        if total == 0:
            return None

        arabic_path = CONFIG.TEMP_DIR / f"{job_id}.ar.srt"
        chunk_size = 30

        for i in range(0, total, chunk_size):
            chunk = subs[i:i + chunk_size]
            texts = [f"{j+1}. {sub.text}" for j, sub in enumerate(chunk)]

            prompt = "Translate to Modern Standard Arabic. Subtitles: " + " | ".join(texts) + " Return JSON array."

            try:
                res = requests.post(CONFIG.OLLAMA_URL, json={
                    "model": CONFIG.MODEL,
                    "prompt": prompt,
                    "stream": False,
                    "options": {"temperature": 0.3}
                }, timeout=120)
                res.raise_for_status()
                text = res.json().get('response', '').strip()

                arabic_re = re.compile(r'[\u0600-\u06FF]')
                lines = text.split('\n')
                translations = []
                for line in lines:
                    if arabic_re.search(line):
                        clean = line.strip().strip('"[],')
                        if clean:
                            translations.append(clean)

                for j, txt in enumerate(translations):
                    if i + j < len(subs):
                        subs[i + j].text = txt

            except Exception as e:
                logger.error(f"Translation chunk error: {e}")

            pct = min(100, int((i + chunk_size) / total * 100))
            state.update_progress(job_id, "translate", pct, f"Translated {min(i+chunk_size, total)}/{total}")

        subs.save(str(arabic_path), encoding='utf-8')
        state.update_progress(job_id, "translate", 100, "Complete!")
        return arabic_path
    except Exception as e:
        logger.error(f"Translation error: {e}")
        return None

def inject_subtitle(arabic_path: Path, cache_folder: Path, job_id: str) -> bool:
    try:
        dest_path = cache_folder / "AWD-AR.srt"
        shutil.copy2(arabic_path, dest_path)
        backup = CONFIG.TEMP_DIR / f"{job_id}_AWD-AR.srt"
        shutil.copy2(arabic_path, backup)

        if dest_path.exists():
            logger.info(f"✅ INJECTED: {dest_path}")
            state.update_progress(job_id, "inject", 100, f"✅ Success! {dest_path}")
            return True
        return False
    except Exception as e:
        logger.error(f"Injection failed: {e}")
        state.update_progress(job_id, "inject", 0, f"❌ Error: {e}")
        return False

def process_upload(file_content: bytes, filename: str, video_hash: str, 
                  cache_folder: Path, job_id: str):
    try:
        state.update_progress(job_id, "upload", 10, f"Received {filename}")

        upload_path = CONFIG.TEMP_DIR / f"{job_id}_upload.srt"
        with open(upload_path, 'wb') as f:
            f.write(file_content)

        state.update_progress(job_id, "upload", 50, "Validating...")

        try:
            subs = pysrt.open(str(upload_path), encoding='utf-8', errors='ignore')
            state.update_progress(job_id, "upload", 100, f"Valid: {len(subs)} lines")
        except Exception as e:
            state.update_progress(job_id, "upload", 0, f"Invalid: {e}")
            return

        with open(upload_path, 'r', encoding='utf-8', errors='ignore') as f:
            content = f.read()

        if re.search(r'[\u0600-\u06FF]', content):
            state.update_progress(job_id, "translate", 100, "Already Arabic")
            inject_subtitle(upload_path, cache_folder, job_id)
        else:
            arabic_path = translate_subtitles(upload_path, job_id)
            if arabic_path:
                inject_subtitle(arabic_path, cache_folder, job_id)
    except Exception as e:
        logger.error(f"Process error: {e}")

@app.get("/", response_class=HTMLResponse)
async def root():
    return HTML_TEMPLATE

@app.get("/health")
async def health():
    video_hash, cache_folder = state.get_active_video()
    cache_path = state.stremio_cache_path or find_stremio_cache()
    return {
        "status": "healthy",
        "active_video_hash": video_hash,
        "active_cache_folder": str(cache_folder) if cache_folder else None,
        "cache_path": str(cache_path) if cache_path else None,
        "temp_dir": str(CONFIG.TEMP_DIR),
        "model": CONFIG.MODEL
    }

@app.get("/force-detect")
async def force_detect():
    """Manual force detection for debugging"""
    logger.info("🚨 FORCE DETECT endpoint called")
    result = manual_check_cache()
    return result

@app.post("/upload")
async def upload(
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
    auto_translate: bool = Query(True)
):
    video_hash, cache_folder = state.get_active_video()
    if not cache_folder:
        raise HTTPException(400, "No active video detected!")

    job_id = hashlib.md5(f"{video_hash}_{time.time()}".encode()).hexdigest()[:12]
    content = await file.read()

    if auto_translate:
        background_tasks.add_task(process_upload, content, file.filename, 
                                 video_hash, cache_folder, job_id)

    return {"job_id": job_id, "status": "processing", "video": video_hash}

@app.get("/progress/{job_id}")
async def progress(job_id: str):
    p = state.get_progress(job_id)
    if not p:
        raise HTTPException(404, "Job not found")
    return p

@app.get("/open")
async def open_folder():
    try:
        os.startfile(CONFIG.TEMP_DIR)
        return {"status": "opened"}
    except:
        return {"status": "error", "path": str(CONFIG.TEMP_DIR)}

if __name__ == "__main__":
    logger.info("=" * 70)
    logger.info("Arabic Subtitle AI Server - DEBUG VERSION")
    logger.info("=" * 70)
    logger.info("Extensive logging enabled - Check console and arabic_subtitle_debug.log")
    logger.info("=" * 70)

    cache = find_stremio_cache()
    if cache:
        logger.info(f"✅ Initial cache found: {cache}")
        manual_check_cache()
    else:
        logger.warning("❌ No cache found at startup")

    watcher = Thread(target=watch_stremio_cache, daemon=True)
    watcher.start()
    logger.info("✅ Watcher started")

    webbrowser.open(f"http://localhost:{CONFIG.PORT}")
    logger.info(f"🌐 Opening http://localhost:{CONFIG.PORT}")
    logger.info("=" * 70)

    uvicorn.run(app, host="0.0.0.0", port=CONFIG.PORT)
