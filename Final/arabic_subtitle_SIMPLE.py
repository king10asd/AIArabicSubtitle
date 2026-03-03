#!/usr/bin/env python3
"""
Arabic Subtitle AI - ENHANCED v4
Fixes:
  - SubDL: correct endpoint + params (film_name works for search; no 'type' needed for search by name)
  - OpenSubtitles: correct REST v1 search params, proper JWT auth
  - Injection: writes <video_hash>.srt AND <video_hash>.en.srt so Stremio picks it up in sub list
  - Translation: smaller chunks (5 lines), one-line-per-response mode, better anime prompt
  - Debug endpoint: /debug shows live state + last raw API responses
"""

import os
import time
import hashlib
import shutil
import json
import re
import webbrowser
import zipfile
import io
from pathlib import Path
from threading import Thread, Lock
import requests

import uvicorn
from fastapi import FastAPI, File, UploadFile, BackgroundTasks, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse

# ─── API KEYS ─────────────────────────────────────────────────────────────────
SUBDL_API_KEY           = "sZZkTyFFMdUSPfjBb_s_5dNlfS_O3PzT"
OPENSUBTITLES_API_KEY   = "f22fr0hgPZzYk7DfZfg1MfIoVFRJvSqY"
OPENSUBTITLES_JWT       = (
    "eyJ0eXAiOiJKV1QiLCJhbGciOiJIUzI1NiJ9"
    ".eyJpc3MiOiJmMjJmcjBoZ1BaellrN0RmWmZnMU1mSW9WRlJKdlNxWSIsImV4cCI6MTc3MjU4MDU0OX0"
    ".1-Cp7ejxgvmxdmZo0eYWCVu5NJdcv_NzTDbTbPb_zMU"
)

# ─── Config ───────────────────────────────────────────────────────────────────
STREMIO_CACHE = Path("C:/Users/AWD/AppData/Roaming/stremio/stremio-server/stremio-cache")
TEMP_DIR      = Path("C:/temp/arabic_subs")
ALL_SUBS_DIR  = Path("C:/temp/arabic_subs/all_subs")   # user-chosen subtitles saved here

OLLAMA_URL    = "http://localhost:11434/api/generate"
MODEL         = "llama3.1:8b"
PORT          = 8000

# SubDL
SUBDL_SEARCH = "https://api.subdl.com/api/v1/subtitles"
SUBDL_DL     = "https://dl.subdl.com"

# OpenSubtitles REST v1
OS_SEARCH = "https://api.opensubtitles.com/api/v1/subtitles"
OS_DL     = "https://api.opensubtitles.com/api/v1/download"
OS_UA     = "ArabicSubAI v4"

TEMP_DIR.mkdir(parents=True, exist_ok=True)
ALL_SUBS_DIR.mkdir(parents=True, exist_ok=True)

# ─── State ────────────────────────────────────────────────────────────────────
state = {
    "hash":       None,
    "folder":     None,
    "video":      None,
    "progress":   {"percent": 0, "message": "Idle"},
    "lock":       Lock(),
    # Debug logs for raw API responses
    "_subdl_raw":  "",
    "_os_raw":     "",
}

app = FastAPI()

# =============================================================================
# HELPERS
# =============================================================================

def set_progress(pct: int, msg: str):
    with state["lock"]:
        state["progress"] = {"percent": pct, "message": msg}
    print(f"[Progress {pct}%] {msg}")


# =============================================================================
# STREMIO DETECTION
# =============================================================================

def find_video():
    if not STREMIO_CACHE.exists():
        return None
    best_folder = best_video = None
    best_size = best_time = 0
    try:
        for folder in STREMIO_CACHE.iterdir():
            if not folder.is_dir() or len(folder.name) != 40:
                continue
            for f in folder.iterdir():
                if f.is_file():
                    size  = f.stat().st_size
                    mtime = f.stat().st_mtime
                    if size > 5 * 1024 * 1024 and mtime > best_time:
                        best_time   = mtime
                        best_folder = folder
                        best_video  = f
                        best_size   = size
    except Exception as e:
        print(f"[Watcher] Scan error: {e}")
    if best_folder:
        return {"hash": best_folder.name, "folder": best_folder,
                "video": best_video, "size": best_size}
    return None


def watch():
    last_hash = None
    print("[Watcher] Started")
    while True:
        try:
            result = find_video()
            if result:
                if result["hash"] != last_hash:
                    print(f"[Watcher] New video: {result['hash'][:8]} ({result['size']/1024/1024:.1f} MB) — {result['video'].name}")
                    with state["lock"]:
                        state["hash"]   = result["hash"]
                        state["folder"] = result["folder"]
                        state["video"]  = result["video"]
                    last_hash = result["hash"]
            else:
                if last_hash:
                    print("[Watcher] No video")
                    with state["lock"]:
                        state["hash"] = state["folder"] = state["video"] = None
                last_hash = None
        except Exception as e:
            print(f"[Watcher] Error: {e}")
        time.sleep(3)


# =============================================================================
# SEARCH — SubDL
# Correct params: api_key, film_name, languages (comma-separated codes),
#                 subs_per_page, season_number, episode_number
# Do NOT pass 'type' for a name search — it restricts results badly
# =============================================================================

def search_subdl(query: str, season: str = "", episode: str = "", lang: str = "") -> list:
    params = {
        "api_key":       SUBDL_API_KEY,
        "film_name":     query.strip(),
        "subs_per_page": 30,
    }
    if lang.strip():
        params["languages"] = lang.strip()
    if season.strip():
        params["season_number"] = season.strip()
    if episode.strip():
        params["episode_number"] = episode.strip()

    try:
        r = requests.get(
            SUBDL_SEARCH,
            params=params,
            timeout=15,
            headers={"User-Agent": OS_UA},
        )
        raw = r.text[:3000]
        with state["lock"]:
            state["_subdl_raw"] = f"HTTP {r.status_code}\nURL: {r.url}\n\n{raw}"
        print(f"[SubDL] {r.status_code} — {r.url}")

        data = r.json()
        if not data.get("status"):
            print(f"[SubDL] Non-OK response: {data}")
            return []

        results = []
        for s in data.get("subtitles", []):
            lang_raw = (s.get("language") or "").upper()
            LANG_NAMES = {"AR":"Arabic","ARA":"Arabic","EN":"English","ENG":"English",
                          "FR":"French","DE":"German","ES":"Spanish","IT":"Italian",
                          "PT":"Portuguese","TR":"Turkish","RU":"Russian",
                          "JA":"Japanese","KO":"Korean","ZH":"Chinese"}
            lang_lbl = LANG_NAMES.get(lang_raw, lang_raw or "Unknown")
            url_path = s.get("url", "")
            results.append({
                "source":    "SubDL",
                "name":      (s.get("release_name") or s.get("name") or "Unknown").strip(),
                "lang":      lang_lbl,
                "lang_code": lang_raw,
                "url":       SUBDL_DL + url_path if url_path else "",
                "file_id":   None,
            })
        print(f"[SubDL] {len(results)} results")
        return results

    except Exception as e:
        print(f"[SubDL] Exception: {e}")
        with state["lock"]:
            state["_subdl_raw"] = f"Exception: {e}"
        return []


# =============================================================================
# SEARCH — OpenSubtitles REST v1
# Auth: Api-Key header (no login needed for search)
# Correct params: query, languages (ar,en), season_number, episode_number
# =============================================================================

def _os_headers() -> dict:
    return {
        "Api-Key":       OPENSUBTITLES_API_KEY,
        "Authorization": f"Bearer {OPENSUBTITLES_JWT}",
        "User-Agent":    OS_UA,
        "Content-Type":  "application/json",
        "Accept":        "application/json",
    }


def search_opensubtitles(query: str, season: str = "", episode: str = "", lang: str = "") -> list:
    params: dict = {
        "query":    query.strip(),
        "per_page": 30,
    }
    if lang.strip():
        params["languages"] = lang.strip()
    if season.strip():
        try:
            params["season_number"] = int(season.strip())
        except ValueError:
            pass
    if episode.strip():
        try:
            params["episode_number"] = int(episode.strip())
        except ValueError:
            pass

    try:
        r = requests.get(OS_SEARCH, params=params, headers=_os_headers(), timeout=15)
        raw = r.text[:3000]
        with state["lock"]:
            state["_os_raw"] = f"HTTP {r.status_code}\nURL: {r.url}\n\n{raw}"
        print(f"[OpenSubs] {r.status_code} — {r.url}")

        data = r.json()
        results = []
        for item in data.get("data", []):
            attrs    = item.get("attributes", {})
            files    = attrs.get("files") or [{}]
            lang     = attrs.get("language", "")
            LANG_NAMES = {"ar":"Arabic","en":"English","fr":"French","de":"German","es":"Spanish",
                          "it":"Italian","pt":"Portuguese","tr":"Turkish","ru":"Russian",
                          "ja":"Japanese","ko":"Korean","zh":"Chinese"}
            lang_lbl = LANG_NAMES.get(lang, lang.upper() or "Unknown")
            file_id  = files[0].get("file_id") if files else None
            # Try multiple name fields
            feat     = attrs.get("feature_details") or {}
            name     = (
                attrs.get("release")
                or feat.get("title")
                or feat.get("movie_name")
                or "Unknown"
            ).strip()
            if attrs.get("season_number") and attrs.get("episode_number"):
                name += f"  S{attrs['season_number']:02d}E{attrs['episode_number']:02d}"
            results.append({
                "source":    "OpenSubtitles",
                "name":      name,
                "lang":      lang_lbl,
                "lang_code": lang.upper(),
                "url":       None,
                "file_id":   file_id,
            })
        print(f"[OpenSubs] {len(results)} results")
        return results

    except Exception as e:
        print(f"[OpenSubs] Exception: {e}")
        with state["lock"]:
            state["_os_raw"] = f"Exception: {e}"
        return []


# =============================================================================
# DOWNLOADS
# =============================================================================

def _extract_best_srt(zip_bytes: bytes, dest: Path) -> bool:
    try:
        z    = zipfile.ZipFile(io.BytesIO(zip_bytes))
        exts = (".srt", ".vtt", ".sub", ".ass", ".ssa")
        subs = [f for f in z.namelist() if any(f.lower().endswith(e) for e in exts)
                and not f.startswith("__MACOSX")]
        if not subs:
            print(f"[ZIP] No sub files found. Contents: {z.namelist()}")
            return False
        # Prefer Arabic-named files
        subs.sort(key=lambda f: ("arabic" in f.lower() or ".ar." in f.lower()), reverse=True)
        print(f"[ZIP] Extracting: {subs[0]}")
        data = z.read(subs[0])
        dest.write_bytes(data)
        return True
    except Exception as e:
        print(f"[ZIP] Error: {e}")
        return False


def download_subdl(url: str, dest: Path) -> bool:
    try:
        r  = requests.get(url, timeout=40, headers={"User-Agent": OS_UA}, allow_redirects=True)
        ct = r.headers.get("Content-Type", "")
        print(f"[SubDL-DL] {r.status_code}  ct={ct}  size={len(r.content)}")
        if "zip" in ct or url.lower().endswith(".zip") or r.content[:2] == b"PK":
            return _extract_best_srt(r.content, dest)
        dest.write_bytes(r.content)
        return dest.stat().st_size > 0
    except Exception as e:
        print(f"[SubDL-DL] Error: {e}")
        return False


def download_opensubtitles(file_id: int, dest: Path) -> bool:
    try:
        r    = requests.post(OS_DL, json={"file_id": file_id},
                             headers=_os_headers(), timeout=20)
        data = r.json()
        print(f"[OpenSubs-DL] {r.status_code} — {json.dumps(data)[:300]}")
        link = data.get("link")
        if not link:
            return False
        content = requests.get(link, timeout=40).content
        dest.write_bytes(content)
        return dest.stat().st_size > 0
    except Exception as e:
        print(f"[OpenSubs-DL] Error: {e}")
        return False


# =============================================================================
# SRT PARSER / BUILDER
# =============================================================================

def is_arabic(text: str) -> bool:
    count = sum(1 for c in text if "\u0600" <= c <= "\u06FF")
    return count > 30


def vtt_to_srt(content: str) -> str:
    content = re.sub(r"WEBVTT[^\n]*\n+", "", content)
    content = re.sub(r"NOTE[^\n]*(\n[^\n]*)?\n", "", content)
    content = re.sub(r"(\d{2}:\d{2}:\d{2})\.(\d{3})", r"\1,\2", content)
    # Add missing index numbers
    blocks = re.split(r"\n{2,}", content.strip())
    result = []
    idx    = 1
    for block in blocks:
        b = block.strip()
        if "-->" in b:
            result.append(f"{idx}\n{b}")
            idx += 1
    return "\n\n".join(result)


def ass_to_srt(content: str) -> str:
    """Convert ASS/SSA to basic SRT"""
    lines   = content.splitlines()
    entries = []
    in_ev   = False
    fmt     = []
    for line in lines:
        if line.strip().lower() == "[events]":
            in_ev = True
            continue
        if in_ev:
            if line.startswith("Format:"):
                fmt = [f.strip() for f in line[7:].split(",")]
            elif line.startswith("Dialogue:"):
                parts = line[9:].split(",", len(fmt) - 1)
                d = dict(zip(fmt, parts))
                start = d.get("Start", "0:00:00.00").strip()
                end   = d.get("End",   "0:00:00.00").strip()
                text  = d.get("Text",  "").strip()
                # Remove ASS override codes
                text = re.sub(r"\{[^}]*\}", "", text).replace("\\N", "\n")
                def _ts(t):
                    t = t.replace(".", ",")
                    parts2 = t.split(":")
                    if len(parts2) == 3:
                        h, m, s = parts2
                        if "," not in s:
                            s += ",000"
                        return f"{int(h):02d}:{int(m):02d}:{s}"
                    return t
                entries.append((_ts(start), _ts(end), text))
    result = []
    for i, (s, e, t) in enumerate(entries, 1):
        result.append(f"{i}\n{s} --> {e}\n{t}\n")
    return "\n".join(result)


def parse_srt(content: str) -> list:
    content = content.replace("\r\n", "\n").replace("\r", "\n")
    # Strip BOM
    content = content.lstrip("\ufeff")
    blocks  = re.split(r"\n{2,}", content.strip())
    subs    = []
    for block in blocks:
        lines = block.strip().splitlines()
        if len(lines) < 3:
            continue
        idx_line = lines[0].strip()
        if not re.match(r"^\d+$", idx_line):
            continue
        tc = re.match(
            r"(\d{1,2}:\d{2}:\d{2}[,\.]\d{3})\s*-->\s*(\d{1,2}:\d{2}:\d{2}[,\.]\d{3})",
            lines[1].strip()
        )
        if not tc:
            continue
        subs.append({
            "index": idx_line,
            "start": tc.group(1).replace(".", ","),
            "end":   tc.group(2).replace(".", ","),
            "text":  "\n".join(lines[2:]).strip(),
        })
    return subs


def build_srt(subs: list) -> str:
    parts = []
    for i, s in enumerate(subs):
        parts.append(f"{i+1}\n{s['start']} --> {s['end']}\n{s['text']}\n")
    return "\n".join(parts)


# =============================================================================
# TRANSLATION — Anime-Friendly Arabic via Ollama
# Strategy: translate ONE line at a time for reliability,
#           batching is fast but causes hallucination — small chunks of 5
# =============================================================================

SYSTEM_PROMPT = """أنت مترجم أنمي محترف. تترجم حوارات الأنمي والمانغا إلى اللهجة العربية الواضحة.

قواعد الترجمة:
• اللغة: عربية محكية طبيعية — ليست فصحى جافة ولا ركيكة
• الشخصية: حافظ على نبرة الشخصية بالكامل (غضب، صراخ، ضعف، حب، كوميديا)
• الأسماء: احتفظ بالأسماء اليابانية كما هي (ناروتو، غوكو، زورو، إيتاشي...)
• مصطلحات: senpai=سنباي | sensei=سنسي | nakama=رفاق | jutsu=جوتسو | nii-san=نيسان
• الصراخ والتعجب: أبرزه بقوة في الترجمة مع علامات التعجب
• لا تضف تفسيرات أو أقواس — الحوار فقط
• إذا كان الكلام أغنية أو شعر — ترجمه بنفس الإيقاع
• الحوار القصير يبقى قصيراً — لا تطوّل"""


def translate_lines_batch(texts: list) -> list:
    """Translate a small batch (≤5 lines). Returns list same length as input."""
    if not texts:
        return texts

    # Build numbered input
    numbered = "\n".join(f"[{i+1}] {t}" for i, t in enumerate(texts))

    prompt = (
        SYSTEM_PROMPT + "\n\n"
        "ترجم الأسطر التالية. أعد النتائج بنفس الترقيم [1] [2] ... بدون أي نص إضافي:\n\n"
        + numbered
    )

    try:
        r = requests.post(
            OLLAMA_URL,
            json={
                "model":   MODEL,
                "prompt":  prompt,
                "stream":  False,
                "options": {
                    "temperature": 0.3,
                    "num_predict": 800,
                    "stop": ["\n\n\n"],
                },
            },
            timeout=180,
        )
        raw = r.json().get("response", "").strip()

        # Try to extract [N] answer lines
        result = list(texts)  # fallback = original
        for i in range(len(texts)):
            # Match [N] followed by the translation on same line or next
            m = re.search(rf"\[{i+1}\]\s*(.+?)(?=\n\[{i+2}\]|\Z)", raw, re.DOTALL)
            if m:
                line = m.group(1).strip().strip('"').strip("'")
                # Remove any leading quote char
                line = re.sub(r'^["""\'\']+', "", line).strip()
                if line:
                    result[i] = line

        # If parsing failed completely, try JSON fallback
        if result == list(texts):
            m2 = re.search(r"\[[\s\S]*?\]", raw)
            if m2:
                try:
                    parsed = json.loads(m2.group())
                    if isinstance(parsed, list) and len(parsed) >= len(texts):
                        result = [str(p).strip() for p in parsed[:len(texts)]]
                except Exception:
                    pass

        return result

    except Exception as e:
        print(f"[Ollama] Error: {e}")
        return texts


def translate_srt_file(input_path: Path, output_path: Path) -> bool:
    try:
        raw = input_path.read_text(encoding="utf-8-sig", errors="ignore")

        # Convert format if needed
        if "WEBVTT" in raw[:200]:
            raw = vtt_to_srt(raw)
        elif raw.strip().startswith("[Script Info]") or "[V4" in raw[:500]:
            raw = ass_to_srt(raw)

        subs = parse_srt(raw)
        if not subs:
            print("[Trans] parse_srt returned 0 subs — malformed?")
            return False

        total      = len(subs)
        chunk_size = 5  # small = reliable
        print(f"[Trans] Translating {total} subtitles in chunks of {chunk_size}")

        for i in range(0, total, chunk_size):
            chunk = subs[i: i + chunk_size]
            # Strip HTML/ASS tags, collapse multiline to single space
            texts = []
            for s in chunk:
                t = re.sub(r"<[^>]+>", "", s["text"])
                t = re.sub(r"\{[^}]*\}", "", t)
                t = re.sub(r"\s+", " ", t).strip()
                texts.append(t if t else "...")

            translated = translate_lines_batch(texts)

            for j, tr in enumerate(translated):
                subs[i + j]["text"] = tr.strip()

            pct = int((i + len(chunk)) * 100 / total)
            set_progress(min(pct, 94), f"ترجمة {i + len(chunk)} / {total}")

        # utf-8-sig = BOM header — required for Arabic display on Windows / Stremio
        output_path.write_text(build_srt(subs), encoding="utf-8-sig")
        print(f"[Trans] Done → {output_path}")
        return True

    except Exception as e:
        print(f"[Trans] Fatal: {e}")
        import traceback; traceback.print_exc()
        return False


# =============================================================================
# STREMIO SUBTITLE INJECTION
#
# Stremio addon SDK subtitle response format (subtitles.md):
#   { id, url, lang }
#   - url must be HTTP-accessible, pointing to the raw .srt file
#   - lang is BCP-47 code e.g. "ara" for Arabic
#
# Strategy:
#   1. Save the final .srt to TEMP_DIR/current_ar.srt
#   2. Serve it via FastAPI at GET /subtitle/current_ar.srt
#   3. Also write it physically into the Stremio cache folder so
#      Stremio's local file scanner picks it up too.
#   Physical file naming that Stremio local scanner recognises:
#      <40-char-hash>.srt  (same name as the cache folder)
# =============================================================================

# Track the last injected file so the HTTP endpoint can serve it
state["injected_srt"] = None


def inject_subtitle(srt_path: Path) -> bool:
    """
    Dual injection:
      A) Copy into Stremio cache folder as <hash_folder_name>.srt
         (Stremio local scanner matches subtitle by folder/stream name)
      B) Copy to TEMP_DIR/current_ar.srt so HTTP /subtitle/current_ar.srt serves it
    """
    with state["lock"]:
        folder   = state["folder"]
        video    = state["video"]
        hash_id  = state["hash"]          # 40-char folder name = stream hash

    if not folder or not video or not hash_id:
        print("[Inject] No active video in state")
        return False

    try:
        content = srt_path.read_bytes()

        # ── A. Physical injection into Stremio cache folder ──────────────────
        # Remove stale injected files first
        for pattern in ["*.ar.srt", "*.ara.srt", "AWD_AR.srt"]:
            for old in folder.glob(pattern):
                try:
                    old.unlink()
                    print(f"[Inject] Removed old: {old.name}")
                except Exception:
                    pass

        # Write as <hash_id>.srt — Stremio local subtitle scanner matches this
        # and also write <video_stem>.ar.srt for the StremioSubtitleInjector pattern
        for target_name in [f"{hash_id}.srt", f"{video.stem}.ar.srt"]:
            target = folder / target_name
            target.write_bytes(content)
            print(f"[Inject] ✓ cache → {target.name}")

        # ── B. Copy to TEMP_DIR so HTTP endpoint can serve it ─────────────────
        http_target = TEMP_DIR / "current_ar.srt"
        shutil.copy2(srt_path, http_target)
        print(f"[Inject] ✓ http  → {http_target}")

        with state["lock"]:
            state["injected_srt"] = http_target

        return True

    except Exception as e:
        print(f"[Inject] Error: {e}")
        import traceback; traceback.print_exc()
        return False


# =============================================================================
# BACKGROUND JOBS
# =============================================================================

def _normalize_to_srt(path: Path, job_id: str) -> Path:
    """Ensure file is valid SRT, convert if needed. Returns new path."""
    suffix = path.suffix.lower()
    if suffix in (".ass", ".ssa"):
        raw = path.read_text(encoding="utf-8-sig", errors="ignore")
        converted = ass_to_srt(raw)
        new_path = TEMP_DIR / f"{job_id}_conv.srt"
        new_path.write_text(converted, encoding="utf-8")
        return new_path
    if suffix == ".vtt":
        raw = path.read_text(encoding="utf-8-sig", errors="ignore")
        new_path = TEMP_DIR / f"{job_id}_conv.srt"
        new_path.write_text(vtt_to_srt(raw), encoding="utf-8")
        return new_path
    return path


def process_uploaded_srt(upload_path: Path, job_id: str):
    set_progress(5, "قراءة الملف…")
    upload_path = _normalize_to_srt(upload_path, job_id)
    content     = upload_path.read_text(encoding="utf-8-sig", errors="ignore")

    if is_arabic(content):
        set_progress(80, "الملف عربي — جاري الحقن في Stremio…")
        ok = inject_subtitle(upload_path)
        set_progress(100, "✅ تم الحقن (الملف عربي)" if ok else "❌ فشل الحقن")
    else:
        set_progress(10, "جاري الترجمة إلى العربية…")
        # AI output saved to TEMP_DIR/current_ar.srt (and named by job_id)
        ar_path = TEMP_DIR / f"{job_id}.ar.srt"
        ok = translate_srt_file(upload_path, ar_path)
        if ok:
            set_progress(96, "جاري الحقن في Stremio…")
            inject_subtitle(ar_path)
            set_progress(100, "✅ اكتملت الترجمة والحقن")
        else:
            set_progress(0, "❌ فشلت الترجمة — تحقق من Ollama")


def _save_to_all_subs(src: Path, name_hint: str) -> Path:
    """Save a subtitle copy to ALL_SUBS_DIR with a clean filename."""
    safe_name = re.sub(r'[\\/:*?"<>|]', "_", name_hint)[:80].strip("_. ")
    if not safe_name:
        safe_name = f"subtitle_{int(time.time())}"
    dest = ALL_SUBS_DIR / f"{safe_name}.srt"
    counter = 1
    while dest.exists():
        dest = ALL_SUBS_DIR / f"{safe_name}_{counter}.srt"
        counter += 1
    try:
        shutil.copy2(src, dest)
        print(f"[AllSubs] Saved → {dest.name}")
    except Exception as e:
        print(f"[AllSubs] Save error: {e}")
    return dest


def process_search_result(source: str, url: str, file_id, job_id: str, sub_name: str = ""):
    raw_path = TEMP_DIR / f"{job_id}_raw.srt"
    set_progress(8, f"تحميل من {source}…")

    ok = False
    if source == "SubDL" and url:
        ok = download_subdl(url, raw_path)
    elif source == "OpenSubtitles" and file_id:
        ok = download_opensubtitles(int(file_id), raw_path)

    if not ok or not raw_path.exists() or raw_path.stat().st_size == 0:
        set_progress(0, f"❌ فشل التحميل من {source}")
        return

    set_progress(28, "تم التحميل — تحليل الملف…")
    raw_path = _normalize_to_srt(raw_path, job_id)
    content  = raw_path.read_text(encoding="utf-8-sig", errors="ignore")

    if is_arabic(content):
        # Save Arabic original to all_subs
        _save_to_all_subs(raw_path, sub_name or f"{source}_{job_id}_ar")
        inject_subtitle(raw_path)
        set_progress(100, "✅ تم الحقن مباشرة (ترجمة عربية)")
    else:
        set_progress(33, "الترجمة إنجليزية — جاري الترجمة بالذكاء الاصطناعي…")
        ar_path = TEMP_DIR / f"{job_id}_ar.srt"
        ok2 = translate_srt_file(raw_path, ar_path)
        if ok2:
            # Save both original and translated to all_subs
            _save_to_all_subs(raw_path, sub_name or f"{source}_{job_id}_orig")
            _save_to_all_subs(ar_path,  (sub_name or f"{source}_{job_id}") + "_AR")
            inject_subtitle(ar_path)
            set_progress(100, "✅ ترجمة وحقن ناجح")
        else:
            set_progress(0, "❌ فشلت الترجمة — تحقق من Ollama")


# =============================================================================
# API ENDPOINTS
# =============================================================================

@app.get("/status")
def get_status():
    with state["lock"]:
        if state["hash"]:
            sz = state["video"].stat().st_size if state["video"] else 0
            return {
                "hash":  state["hash"][:16],
                "size":  f"{sz/1024/1024:.1f} MB",
                "video": state["video"].name if state["video"] else "",
            }
        return {"hash": None}


@app.post("/upload")
async def do_upload(background_tasks: BackgroundTasks, file: UploadFile = File(...)):
    with state["lock"]:
        if not state["hash"]:
            return JSONResponse({"error": "لا يوجد فيديو يعمل في Stremio"})

    fname = (file.filename or "").lower()
    valid = (".srt", ".vtt", ".sub", ".ass", ".ssa")
    if not any(fname.endswith(e) for e in valid):
        return JSONResponse({"error": "صيغة غير مدعومة. استخدم: srt / vtt / sub / ass / ssa"})

    job_id  = hashlib.md5(f"{time.time()}".encode()).hexdigest()[:12]
    suffix  = Path(file.filename).suffix.lower()
    up_path = TEMP_DIR / f"{job_id}{suffix}"
    up_path.write_bytes(await file.read())

    background_tasks.add_task(process_uploaded_srt, up_path, job_id)
    return {"job_id": job_id}


@app.get("/search")
def search_subtitles(
    q:       str = Query(...),
    season:  str = Query(default=""),
    episode: str = Query(default=""),
    lang:    str = Query(default="ALL"),
    source:  str = Query(default="ALL"),
):
    if not q.strip():
        return {"results": [], "total": 0, "error": "Empty query"}

    lang_upper = lang.strip().upper()
    # Build language strings for each API
    # SubDL: uppercase comma-separated e.g. "AR" or "" for all
    # OpenSubtitles: lowercase e.g. "ar" or "" for all
    if lang_upper in ("", "ALL"):
        subdl_lang = ""
        os_lang    = ""
    else:
        subdl_lang = lang_upper
        os_lang    = lang.strip().lower()

    source_upper = source.strip().upper()

    subdl_results = []
    os_results    = []

    if source_upper in ("ALL", "SUBDL"):
        subdl_results = search_subdl(q, season, episode, subdl_lang)

    if source_upper in ("ALL", "OPENSUBTITLES"):
        os_results = search_opensubtitles(q, season, episode, os_lang)

    results = subdl_results + os_results

    # Post-filter by lang_code if a specific language was requested
    if lang_upper not in ("", "ALL"):
        results = [r for r in results
                   if r.get("lang_code", "").upper().startswith(lang_upper[:2])]

    return {
        "results":    results,
        "total":      len(results),
        "from_subdl": len([r for r in results if r["source"] == "SubDL"]),
        "from_os":    len([r for r in results if r["source"] == "OpenSubtitles"]),
    }


@app.post("/download")
async def download_and_use(background_tasks: BackgroundTasks, request: Request):
    try:
        payload = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON"})

    source   = payload.get("source",   "")
    url      = payload.get("url",      "") or ""
    file_id  = payload.get("file_id")
    sub_name = payload.get("sub_name", "") or ""

    with state["lock"]:
        if not state["hash"]:
            return JSONResponse({"error": "لا يوجد فيديو يعمل في Stremio"})

    job_id = hashlib.md5(f"{time.time()}".encode()).hexdigest()[:12]
    background_tasks.add_task(process_search_result, source, url, file_id, job_id, sub_name)
    return {"job_id": job_id}


@app.get("/progress/{job_id}")
def get_progress_by_id(job_id: str):
    with state["lock"]:
        return state["progress"] or {"percent": 0, "message": "Starting…"}


@app.get("/progress")
def get_progress_latest():
    with state["lock"]:
        return state["progress"] or {"percent": 0, "message": "Idle"}


@app.get("/open_folder")
def open_folder():
    try:
        os.startfile(str(TEMP_DIR))
        return {"ok": True}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@app.get("/open_stremio_folder")
def open_stremio_folder():
    with state["lock"]:
        folder = state["folder"]
    if folder and folder.exists():
        try:
            os.startfile(str(folder))
            return {"ok": True}
        except Exception as e:
            return {"ok": False, "error": str(e)}
    return {"ok": False, "error": "No active video"}


@app.get("/clear_cache")
def clear_cache():
    try:
        deleted = 0
        for f in TEMP_DIR.iterdir():
            if f.is_file():
                f.unlink()
                deleted += 1
        set_progress(0, "Idle")
        return {"ok": True, "deleted": deleted}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@app.get("/debug")
def debug_info():
    """Show raw API responses for troubleshooting search"""
    with state["lock"]:
        return {
            "state": {
                "hash":   state["hash"],
                "video":  str(state["video"]) if state["video"] else None,
                "folder": str(state["folder"]) if state["folder"] else None,
            },
            "subdl_last_response":  state["_subdl_raw"],
            "os_last_response":     state["_os_raw"],
            "progress":             state["progress"],
        }


from fastapi.responses import FileResponse as _FileResponse

@app.get("/subtitle/{filename}")
def serve_subtitle(filename: str):
    """
    HTTP subtitle file server.
    Stremio addon SDK requires subtitles to be served over HTTP.
    Ref: https://github.com/Stremio/stremio-addon-sdk/blob/master/docs/api/responses/subtitles.md
    URL: http://localhost:8000/subtitle/current_ar.srt
    """
    safe = Path(filename).name          # no path traversal
    path = TEMP_DIR / safe
    if not path.exists() or path.suffix.lower() not in (".srt", ".vtt"):
        return JSONResponse({"error": "not found"}, status_code=404)
    return _FileResponse(
        path,
        media_type="text/plain; charset=utf-8",
        headers={"Access-Control-Allow-Origin": "*"},
    )


@app.get("/subtitle_info")
def subtitle_info():
    """
    Returns Stremio addon SDK subtitle descriptor for the current injected file.
    Shape: { subtitles: [ { id, url, lang } ] }
    Ref: https://github.com/Stremio/stremio-addon-sdk/blob/master/docs/api/responses/subtitles.md
    """
    with state["lock"]:
        hash_id = state["hash"] or ""
        srt     = state.get("injected_srt")
    if not srt or not Path(str(srt)).exists():
        return {"subtitles": []}
    short = hash_id[:8]
    return {
        "subtitles": [
            {
                "id":   f"ai-ara-{short}",
                "url":  f"http://localhost:{PORT}/subtitle/current_ar.srt",
                "lang": "ara",
            }
        ]
    }


# =============================================================================
# WEB UI
# =============================================================================

HTML = r"""<!DOCTYPE html>
<html lang="ar" dir="rtl">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Arabic Subtitle AI</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{
  font-family:'Segoe UI',Tahoma,sans-serif;
  background:linear-gradient(135deg,#0d0221,#1a0533,#0d1b4b);
  color:#e8e8f0;min-height:100vh;padding:14px;
}
h1{
  text-align:center;font-size:1.5em;margin:8px 0 16px;
  letter-spacing:1px;text-shadow:0 0 18px rgba(0,200,255,.4);
}
.hi{color:#00e5ff}.sub{color:#b388ff}
.grid{display:grid;grid-template-columns:1fr 1fr;gap:12px;max-width:980px;margin:0 auto}
@media(max-width:660px){.grid{grid-template-columns:1fr}}
.card{
  background:rgba(255,255,255,.06);border:1px solid rgba(255,255,255,.1);
  border-radius:14px;padding:16px;backdrop-filter:blur(6px);
}
.card h2{font-size:.92em;margin-bottom:10px;color:#00e5ff}
.badge{display:inline-block;padding:3px 12px;border-radius:20px;font-weight:700;font-size:.82em}
.badge.on{background:#00c853;color:#000}.badge.off{background:#d50000;color:#fff}
.info{font-size:.76em;color:#9ab;margin-top:7px;line-height:1.65;word-break:break-all}
.btn{
  background:linear-gradient(135deg,#0288d1,#01579b);
  color:#fff;border:none;padding:7px 13px;border-radius:8px;
  cursor:pointer;font-size:.82em;white-space:nowrap;transition:filter .15s;
}
.btn:hover{filter:brightness(1.2)}
.btn.g{background:linear-gradient(135deg,#00c853,#1b5e20)}
.btn.r{background:linear-gradient(135deg,#e53935,#7f0000)}
.btn.gray{background:rgba(255,255,255,.14)}
.btn:disabled{opacity:.4;cursor:default}
.actions{display:flex;flex-wrap:wrap;gap:7px;margin-top:4px}
.drop{
  border:2px dashed rgba(255,255,255,.2);border-radius:10px;
  padding:24px 14px;text-align:center;cursor:pointer;
  color:#aab;font-size:.86em;transition:all .2s;
}
.drop:hover,.drop.over{border-color:#00e5ff;color:#00e5ff;background:rgba(0,229,255,.05)}
input[type=file]{display:none}
.pbar-wrap{margin-top:10px}
.pbar-bg{background:rgba(0,0,0,.45);border-radius:20px;height:22px;overflow:hidden}
.pbar{
  height:100%;background:linear-gradient(90deg,#00b4d8,#7c4dff);
  border-radius:20px;text-align:center;line-height:22px;
  font-size:.74em;font-weight:700;color:#fff;transition:width .35s ease;min-width:22px;
}
#progMsg{font-size:.76em;color:#9ab;margin-top:4px;text-align:center;min-height:16px;direction:rtl}
.srow{display:flex;gap:6px;flex-wrap:wrap;margin-bottom:8px}
.srow input{
  flex:1;min-width:110px;background:rgba(255,255,255,.09);
  border:1px solid rgba(255,255,255,.18);border-radius:8px;
  padding:7px 10px;color:#fff;font-size:.86em;
}
.srow input::placeholder{color:#667}
#results{max-height:270px;overflow-y:auto;margin-top:6px}
#results::-webkit-scrollbar{width:4px}
#results::-webkit-scrollbar-thumb{background:#444;border-radius:4px}
.ri{
  background:rgba(255,255,255,.05);border-radius:8px;
  padding:8px 11px;margin-bottom:5px;
  display:flex;justify-content:space-between;align-items:center;gap:8px;
}
.ri .rname{font-size:.8em;word-break:break-all;line-height:1.4}
.src{
  font-size:.66em;padding:2px 6px;border-radius:10px;font-weight:700;
  color:#fff;white-space:nowrap;margin-left:4px;display:inline-block;margin-bottom:3px;
}
.src.subdl{background:#e65100}.src.os{background:#1565c0}
.src.ar{background:#2e7d32}.src.en{background:#37474f}
.log{
  background:rgba(0,0,0,.45);border-radius:8px;padding:9px;
  font-family:monospace;font-size:.73em;max-height:120px;overflow-y:auto;color:#9ab;
  direction:ltr;text-align:left;
}
.full{grid-column:span 2}
@media(max-width:660px){.full{grid-column:span 1}}
.spin{display:inline-block;animation:spin .9s linear infinite}
@keyframes spin{to{transform:rotate(360deg)}}
.no-res{font-size:.8em;color:#556;padding:6px}
.inject-note{
  background:rgba(0,229,255,.08);border:1px solid rgba(0,229,255,.2);
  border-radius:8px;padding:9px 12px;margin-top:8px;font-size:.78em;color:#adf;
}
</style>
</head>
<body>
<h1>🎬 <span class="hi">Arabic</span> <span class="sub">Subtitle AI</span></h1>

<div class="grid">

<!-- STATUS -->
<div class="card">
  <h2>📡 حالة Stremio</h2>
  <span id="badge" class="badge off">لا يوجد فيديو</span>
  <div class="info" id="statusInfo">ابدأ تشغيل فيديو في Stremio…</div>
  <div class="inject-note" id="injectNote" style="display:none">
    💡 بعد الحقن: اضغط زر الترجمة داخل Stremio واختر <strong>.ar</strong> من القائمة
  </div>
</div>

<!-- ACTIONS -->
<div class="card">
  <h2>⚙️ إجراءات</h2>
  <div class="actions">
    <button class="btn gray" onclick="refresh()">🔄 تحديث</button>
    <button class="btn gray" onclick="openStremio()">📂 مجلد Stremio</button>
    <button class="btn gray" onclick="openSubs()">📁 مجلد الترجمات</button>
    <button class="btn r"    onclick="clearCache()">🗑️ مسح الكاش</button>
    <button class="btn gray" onclick="openDebug()">🔧 Debug</button>
  </div>
</div>

<!-- UPLOAD -->
<div class="card">
  <h2>📤 رفع ترجمة يدوياً</h2>
  <div class="drop" id="dropZone"
       onclick="document.getElementById('fi').click()"
       ondragover="event.preventDefault();this.classList.add('over')"
       ondragleave="this.classList.remove('over')"
       ondrop="handleDrop(event)">
    <p>اسحب الملف هنا أو انقر للاختيار</p>
    <p style="font-size:.75em;color:#667;margin-top:4px">srt • vtt • sub • ass • ssa</p>
    <input type="file" id="fi" accept=".srt,.vtt,.sub,.ass,.ssa" onchange="uploadFile(this)">
  </div>
  <div class="pbar-wrap" id="progWrap" style="display:none">
    <div class="pbar-bg"><div class="pbar" id="bar" style="width:2%">0%</div></div>
    <div id="progMsg"></div>
  </div>
</div>

<!-- SEARCH -->
<div class="card">
  <h2>🔍 بحث عن ترجمة</h2>
  <div class="srow">
    <input id="qT" placeholder="اسم الأنمي / الفيلم / المسلسل" style="flex:3;color:#000;background:#fff" onkeydown="if(event.key==='Enter')doSearch()">
    <input id="qS" placeholder="S"  style="flex:.6;max-width:44px;text-align:center;color:#000;background:#fff">
    <input id="qE" placeholder="EP" style="flex:.6;max-width:44px;text-align:center;color:#000;background:#fff">
  </div>
  <div class="srow" style="margin-top:6px">
    <input id="qLang" placeholder="اللغة: ar  /  en  /  fr  /  اتركه فارغاً للكل"
           style="flex:2;color:#000;background:#fff;font-size:.82em">
    <select id="qSource" style="flex:1;background:#fff;color:#000;border:1px solid #ccc;border-radius:8px;padding:7px 8px;font-size:.82em;cursor:pointer">
      <option value="ALL">🌐 الكل (SubDL + OpenSubtitles)</option>
      <option value="SubDL">🟠 SubDL فقط</option>
      <option value="OpenSubtitles">🔵 OpenSubtitles فقط</option>
    </select>
    <button class="btn" onclick="doSearch()">🔍 بحث</button>
  </div>
  <div id="searchStatus" class="no-res">أدخل اسماً للبحث…</div>
  <div id="results"></div>
</div>

<!-- LOG -->
<div class="card full">
  <h2>📋 السجل</h2>
  <div class="log" id="logBox"></div>
</div>

</div>

<script>
let _poll=null, _results=[];

function lg(msg){
  const b=document.getElementById('logBox');
  const t=new Date().toLocaleTimeString();
  b.innerHTML=`[${t}] ${msg}\n`+b.innerHTML;
}

async function refresh(){
  try{
    const d=await(await fetch('/status')).json();
    const badge=document.getElementById('badge');
    const info=document.getElementById('statusInfo');
    const note=document.getElementById('injectNote');
    if(d.hash){
      badge.textContent='نشط ✓';badge.className='badge on';
      info.innerHTML=`Hash: <b>${d.hash}</b><br>الحجم: ${d.size}<br>الملف: ${d.video}`;
      note.style.display='block';
    }else{
      badge.textContent='لا يوجد فيديو';badge.className='badge off';
      info.innerHTML='ابدأ تشغيل فيديو في Stremio…';
      note.style.display='none';
    }
  }catch(e){lg('خطأ: '+e.message)}
}

function setBar(pct,msg){
  document.getElementById('progWrap').style.display='block';
  const b=document.getElementById('bar');
  b.style.width=Math.max(pct,2)+'%';
  b.textContent=pct+'%';
  document.getElementById('progMsg').textContent=msg||'';
}

function startPoll(jobId){
  if(_poll)clearInterval(_poll);
  _poll=setInterval(async()=>{
    try{
      const d=await(await fetch('/progress/'+jobId)).json();
      setBar(d.percent,d.message);
      if(d.percent>=100||d.percent===0){
        clearInterval(_poll);_poll=null;
        lg((d.percent>=100?'✅ ':'❌ ')+(d.message||''));
        if(d.percent>=100){refresh();showInjectTip();}
      }
    }catch(e){}
  },700);
}

function showInjectTip(){
  lg('💡 افتح Stremio → زر الترجمة → اختر ".ar" من القائمة');
}

function handleDrop(e){
  e.preventDefault();
  document.getElementById('dropZone').classList.remove('over');
  const f=e.dataTransfer.files[0];
  if(f)_doUpload(f);
}
async function uploadFile(inp){if(inp.files.length)_doUpload(inp.files[0]);}
async function _doUpload(file){
  const form=new FormData();form.append('file',file);
  lg('رفع: '+file.name);setBar(3,'جاري الرفع…');
  try{
    const d=await(await fetch('/upload',{method:'POST',body:form})).json();
    if(d.error){lg('⚠️ '+d.error);setBar(0,d.error);return;}
    if(d.job_id)startPoll(d.job_id);
  }catch(e){lg('فشل: '+e.message);}
}

async function doSearch(){
  const q    = document.getElementById('qT').value.trim();
  const s    = document.getElementById('qS').value.trim();
  const ep   = document.getElementById('qE').value.trim();
  const lang = document.getElementById('qLang').value.trim();        // free text e.g. "ar" or ""
  const src  = document.getElementById('qSource').value;             // ALL / SubDL / OpenSubtitles
  if(!q){lg('⚠️ أدخل اسم للبحث');return;}
  const ss  = document.getElementById('searchStatus');
  const box = document.getElementById('results');
  ss.innerHTML='<span class="spin">⏳</span> جاري البحث…';
  box.innerHTML='';
  lg(`بحث: "${q}"${s?' S'+s:''}${ep?' E'+ep:''}  lang=${lang||'ALL'}  src=${src}`);
  try{
    const url='/search?q='+encodeURIComponent(q)
             +'&season='+encodeURIComponent(s)
             +'&episode='+encodeURIComponent(ep)
             +'&lang='+encodeURIComponent(lang||'ALL')
             +'&source='+encodeURIComponent(src);
    const d=await(await fetch(url)).json();
    _results=d.results||[];
    if(d.error){ss.innerHTML='⚠️ '+d.error;return;}
    ss.innerHTML=_results.length
      ? `وُجد <b>${d.total}</b> نتيجة`
      : 'لا توجد نتائج — جرب اسماً مختلفاً أو ارفع يدوياً';
    renderResults(_results);
    lg(`نتائج: ${d.total}  (SubDL: ${d.from_subdl||0}, OpenSubs: ${d.from_os||0})`);
  }catch(e){
    ss.innerHTML='❌ خطأ: '+e.message;
    lg('خطأ بحث: '+e.message);
  }
}

function esc(s){return(s||'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]))}

function renderResults(items){
  const box=document.getElementById('results');
  box.innerHTML=items.map((it,i)=>{
    const sc=it.source==='SubDL'?'subdl':'os';
    const lc=it.lang==='Arabic'?'ar':'en';
    return `<div class="ri">
      <div style="min-width:0;flex:1">
        <span class="src ${sc}">${esc(it.source)}</span>
        <span class="src ${lc}">${esc(it.lang)}</span>
        <div class="rname">${esc(it.name)}</div>
      </div>
      <button class="btn g" style="flex-shrink:0" onclick="useResult(${i})">⬇️ استخدام</button>
    </div>`;
  }).join('');
}

async function useResult(i){
  const it=_results[i];
  lg('تحميل: '+it.name+' ('+it.source+')');
  setBar(4,'تحميل…');
  try{
    const d=await(await fetch('/download',{
      method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({
        source:   it.source,
        url:      it.url||'',
        file_id:  it.file_id||null,
        sub_name: it.name||''
      })
    })).json();
    if(d.error){lg('⚠️ '+d.error);return;}
    if(d.job_id)startPoll(d.job_id);
  }catch(e){lg('فشل: '+e.message);}
}

async function openSubs(){await fetch('/open_folder');lg('فتح: C:\\temp\\arabic_subs');}
async function openStremio(){
  const d=await(await fetch('/open_stremio_folder')).json();
  lg(d.ok?'فتح مجلد Stremio ✓':'⚠️ '+(d.error||'لا يوجد فيديو نشط'));
}
async function clearCache(){
  if(!confirm('مسح جميع الملفات المؤقتة في C:\\temp\\arabic_subs؟'))return;
  const d=await(await fetch('/clear_cache')).json();
  lg(d.ok?`✅ تم المسح (${d.deleted} ملف)`:'⚠️ '+d.error);
}
function openDebug(){window.open('/debug','_blank');}

setInterval(refresh,3000);
refresh();
</script>
</body>
</html>"""


@app.get("/", response_class=HTMLResponse)
def index():
    return HTML


# =============================================================================
# MAIN
# =============================================================================

if __name__ == "__main__":
    print("=" * 62)
    print("  Arabic Subtitle AI  —  v4")
    print("=" * 62)
    print("  ✓ SubDL search  (api_key active)")
    print("  ✓ OpenSubtitles search  (api_key + JWT)")
    print("  ✓ Anime-friendly Arabic (Ollama llama3.1:8b)")
    print("  ✓ Inject: <video_stem>.ar.srt  (StremioSubtitleInjector naming)")
    print("  ✓ Formats: srt / vtt / sub / ass / ssa")
    print("  ✓ /debug endpoint for troubleshooting")
    print("=" * 62)

    Thread(target=watch, daemon=True).start()
    webbrowser.open(f"http://localhost:{PORT}")
    print(f"\n  http://localhost:{PORT}\n")

    uvicorn.run(app, host="0.0.0.0", port=PORT, log_level="warning")
