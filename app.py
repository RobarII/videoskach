# /// script
# requires-python = ">=3.10"
# dependencies = [
#     "fastapi>=0.110.0",
#     "uvicorn>=0.28.0",
#     "yt-dlp>=2024.8.0",
#     "imageio-ffmpeg>=0.5.0",
#     "requests>=2.31.0",
#     "beautifulsoup4>=4.12.0",
# ]
# ///
import datetime
import html
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.parse
import uuid
import webbrowser
from typing import Any, Dict, List, Optional

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import BaseModel
import requests
from bs4 import BeautifulSoup
import uvicorn
import yt_dlp

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DOWNLOADS_DIR = os.path.join(BASE_DIR, "downloads")
os.makedirs(DOWNLOADS_DIR, exist_ok=True)

SOCKET_TIMEOUT = 30
HTTP_CHUNK_SIZE = 10 * 1024 * 1024
DOWNLOAD_ATTEMPTS = 4
RETRY_PAUSE_SECONDS = 5

# ---------------------------------------------------------------------------
# FFmpeg Auto-Locator
# ---------------------------------------------------------------------------
_cached_ffmpeg: Optional[str] = None


def get_ffmpeg_path() -> Optional[str]:
    global _cached_ffmpeg
    if _cached_ffmpeg and os.path.isfile(_cached_ffmpeg):
        return _cached_ffmpeg
    sys_path = shutil.which("ffmpeg")
    if sys_path and os.path.isfile(sys_path):
        _cached_ffmpeg = sys_path
        return sys_path
    try:
        import imageio_ffmpeg
        img_exe = imageio_ffmpeg.get_ffmpeg_exe()
        if img_exe and os.path.isfile(img_exe):
            _cached_ffmpeg = img_exe
            return img_exe
    except Exception:
        pass
    return None


def is_ffmpeg_ready() -> bool:
    path = get_ffmpeg_path()
    if not path:
        return False
    try:
        res = subprocess.run([path, "-version"], capture_output=True, text=True, timeout=5)
        return res.returncode == 0
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Video Scraper & Extractor
# ---------------------------------------------------------------------------
DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/128.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
}


def format_duration(seconds: int) -> str:
    if not seconds or seconds < 0:
        return ""
    h = seconds // 3600
    m = (seconds % 3600) // 60
    s = seconds % 60
    if h > 0:
        return f"{h:02d}:{m:02d}:{s:02d}"
    return f"{m:02d}:{s:02d}"


def format_eta(seconds: Any) -> str:
    if seconds is None:
        return ""
    try:
        sec = int(round(float(seconds)))
    except (ValueError, TypeError):
        return ""
    if sec <= 0:
        return ""
    if sec < 60:
        return f"{sec} сек"
    mins = sec // 60
    rem_sec = sec % 60
    if mins < 60:
        return f"{mins} мин {rem_sec} сек" if rem_sec else f"{mins} мин"
    hours = mins // 60
    rem_mins = mins % 60
    return f"{hours} ч {rem_mins} мин"



def scrape_webpage(page_url: str) -> List[str]:
    found: List[str] = []
    try:
        resp = requests.get(page_url, headers=DEFAULT_HEADERS, timeout=12)
        resp.raise_for_status()
        text = resp.text
    except Exception:
        return []

    soup = BeautifulSoup(text, "html.parser")
    for q in [{"property": "og:video"}, {"property": "og:video:url"}, {"property": "og:video:secure_url"}, {"name": "twitter:player:stream"}]:
        for tag in soup.find_all("meta", q):
            c = tag.get("content")
            if c and c.startswith("http"):
                found.append(c)

    for v in soup.find_all("video"):
        if v.get("src"):
            found.append(urllib.parse.urljoin(page_url, v["src"]))
        for s in v.find_all("source"):
            if s.get("src"):
                found.append(urllib.parse.urljoin(page_url, s["src"]))

    regex_urls = re.findall(r'https?://[^"\'\s<>\\]+?\.(?:mp4|m3u8|webm|mov|mpd)(?:\?[^"\'\s<>\\]*)?', text)
    for r in regex_urls:
        found.append(r.replace(r"\/", "/").replace(r"\u0026", "&"))

    seen = set()
    deduped = []
    for u in found:
        if u not in seen:
            seen.add(u)
            deduped.append(u)
    return deduped


def get_video_info(url: str) -> Dict[str, Any]:
    ffmpeg = get_ffmpeg_path()
    opts: Dict[str, Any] = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "noplaylist": True,
        "socket_timeout": SOCKET_TIMEOUT,
    }
    if ffmpeg:
        opts["ffmpeg_location"] = ffmpeg

    info = None
    target_url = url
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
    except Exception:
        candidates = scrape_webpage(url)
        for cand in candidates:
            try:
                with yt_dlp.YoutubeDL(opts) as ydl:
                    info = ydl.extract_info(cand, download=False)
                    target_url = cand
                    break
            except Exception:
                continue

    if not info:
        if any(url.lower().split("?")[0].endswith(ext) for ext in (".mp4", ".mkv", ".webm", ".mov", ".m3u8")):
            parsed_title = os.path.basename(url.split("?")[0]) or "Direct Video Stream"
            return {
                "url": url,
                "title": parsed_title,
                "duration": 0,
                "duration_str": "Поток",
                "thumbnail": "",
                "uploader": "Прямая ссылка",
                "extractor": "Прямая ссылка",
                "formats": [
                    {"id": "best", "label": "Лучшее качество (1080p / 4K)", "ext": "mp4"},
                    {"id": "audio_mp3", "label": "Только аудио (MP3)", "ext": "mp3"}
                ]
            }
        raise RuntimeError(f"Не удалось распознать видео по этой ссылке. Проверьте правильность адреса.")

    title = info.get("title") or "Видео"
    duration = int(info.get("duration") or 0)
    thumbnail = info.get("thumbnail") or ""
    uploader = info.get("uploader") or info.get("channel") or info.get("creator") or ""
    extractor = info.get("extractor", "Веб")

    formats = [
        {"id": "best", "label": "⭐️ Лучшее качество (1080p / 4K / 8K)", "ext": "mp4"},
        {"id": "audio_mp3", "label": "🎵 Только аудио (MP3 320k)", "ext": "mp3"}
    ]

    raw_formats = info.get("formats", [])
    added = set()
    res_list = []
    for f in raw_formats:
        h = f.get("height")
        vcodec = f.get("vcodec")
        if h and h > 0 and vcodec != "none" and h not in added:
            added.add(h)
            res_list.append(h)

    res_list.sort(reverse=True)
    for h in res_list:
        formats.append({
            "id": f"res_{h}",
            "label": f"🎬 {h}p",
            "ext": "mp4",
        })

    return {
        "url": target_url,
        "title": title,
        "duration": duration,
        "duration_str": format_duration(duration),
        "thumbnail": thumbnail,
        "uploader": uploader,
        "extractor": extractor,
        "formats": formats
    }


tasks_progress: Dict[str, Dict[str, Any]] = {}

NON_RETRYABLE_MARKERS = (
    "unsupported url",
    "video unavailable",
    "this video is unavailable",
    "private video",
    "requested format is not available",
    "sign in to confirm",
    "не удалось распознать",
)


def is_retryable_error(exc: BaseException) -> bool:
    text = str(exc).lower()
    return not any(marker in text for marker in NON_RETRYABLE_MARKERS)


def friendly_error(exc: Optional[BaseException]) -> str:
    text = str(exc) or exc.__class__.__name__
    low = text.lower()
    if "timed out" in low or "timeout" in low:
        return (
            "Сервер перестал отвечать (таймаут соединения). "
            "Нажмите «Начать загрузку» ещё раз — файл продолжит качаться с места обрыва."
        )
    if "connection" in low or "reset" in low or "temporary failure" in low:
        return f"Сеть оборвалась: {text}. Попробуйте ещё раз."
    return text


def run_download_task(task_id: str, url: str, quality: str, format_ext: str):
    ffmpeg = get_ffmpeg_path()
    is_audio = quality == "audio_mp3"

    postprocessors = []
    if is_audio:
        ydl_format = "bestaudio/best"
        postprocessors.append({
            "key": "FFmpegExtractAudio",
            "preferredcodec": "mp3",
            "preferredquality": "320",
        })
    elif quality.startswith("res_"):
        h = quality.replace("res_", "")
        ydl_format = f"bestvideo[height<={h}]+bestaudio/best[height<={h}]/best"
    else:
        ydl_format = "bestvideo*+bestaudio/best"

    last_file = {"path": None}

    def hook(d):
        status = d.get("status")
        if status == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
            downloaded = d.get("downloaded_bytes") or 0
            percent = round((downloaded / total * 100), 1) if total > 0 else 0.0
            speed = d.get("speed") or 0
            speed_str = f"{speed / (1024 * 1024):.2f} МБ/с" if speed else ""
            eta = d.get("eta")
            eta_str = format_eta(eta)
            tasks_progress[task_id] = {
                "status": "downloading",
                "percent": percent,
                "speed": speed_str,
                "eta": eta_str,
                "filename": os.path.basename(d.get("filename", "")),
            }
        elif status == "finished":
            fn = d.get("filename", "")
            last_file["path"] = fn
            tasks_progress[task_id] = {
                "status": "processing",
                "percent": 100.0,
                "speed": "Обработка",
                "eta": "",
                "message": "Объединение видео и аудио через FFmpeg...",
            }

    opts: Dict[str, Any] = {
        "format": ydl_format,
        "outtmpl": os.path.join(DOWNLOADS_DIR, "%(title).120B [%(id)s].%(ext)s"),
        "progress_hooks": [hook],
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "windowsfilenames": True,
        "socket_timeout": SOCKET_TIMEOUT,
        "retries": 10,
        "fragment_retries": 10,
        "http_chunk_size": HTTP_CHUNK_SIZE,
        "continuedl": True,
    }
    if ffmpeg:
        opts["ffmpeg_location"] = ffmpeg
        if not is_audio:
            opts["merge_output_format"] = format_ext
    if postprocessors:
        opts["postprocessors"] = postprocessors

    last_error: Optional[BaseException] = None
    for attempt in range(1, DOWNLOAD_ATTEMPTS + 1):
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=True)
                prep_name = ydl.prepare_filename(info)
                base, _ = os.path.splitext(prep_name)
                final_path = f"{base}.{format_ext}"
                if not os.path.exists(final_path) and os.path.exists(prep_name):
                    final_path = prep_name
                if not os.path.exists(final_path) and last_file["path"] and os.path.exists(last_file["path"]):
                    final_path = last_file["path"]

                file_size = os.path.getsize(final_path) if os.path.exists(final_path) else 0
                size_mb = round(file_size / (1024 * 1024), 2)
                fname = os.path.basename(final_path)

                tasks_progress[task_id] = {
                    "status": "finished",
                    "percent": 100.0,
                    "filename": fname,
                    "file_path": final_path,
                    "size_mb": size_mb,
                }
            return
        except Exception as e:
            last_error = e
            if attempt >= DOWNLOAD_ATTEMPTS or not is_retryable_error(e):
                break
            prev = tasks_progress.get(task_id, {})
            tasks_progress[task_id] = {
                "status": "retrying",
                "percent": prev.get("percent", 0.0),
                "speed": "",
                "eta": "",
                "attempt": attempt + 1,
                "total_attempts": DOWNLOAD_ATTEMPTS,
                "message": (
                    f"Соединение прервалось — переподключение "
                    f"({attempt + 1} из {DOWNLOAD_ATTEMPTS})..."
                ),
            }
            time.sleep(RETRY_PAUSE_SECONDS)

    tasks_progress[task_id] = {
        "status": "error",
        "percent": tasks_progress.get(task_id, {}).get("percent", 0.0),
        "error": friendly_error(last_error),
    }


# ---------------------------------------------------------------------------
# Single-Page UI (Design exactly based on /design mockup)
# ---------------------------------------------------------------------------
app = FastAPI(title="Видеоскач")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

SINGLE_PAGE_HTML = """<!DOCTYPE html>
<html lang="ru">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Видеоскач — Быстрое скачивание видео по ссылке</title>
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
  <link href="https://fonts.googleapis.com/css2?family=Plus+Jakarta+Sans:wght@400;500;600;700;800&display=swap" rel="stylesheet">
  <script src="https://cdn.tailwindcss.com"></script>
  <style>
    body {
      font-family: 'Plus Jakarta Sans', -apple-system, BlinkMacSystemFont, sans-serif;
      background-color: #f8fbff;
      background-image: 
        radial-gradient(circle at 100% 0%, rgba(224, 242, 254, 0.7) 0%, rgba(248, 251, 255, 0) 35%),
        radial-gradient(circle at 0% 85%, rgba(236, 254, 255, 0.7) 0%, rgba(248, 251, 255, 0) 30%);
    }
    .btn-gradient {
      background: linear-gradient(180deg, #53a6fd 0%, #3b82f6 100%);
      box-shadow: 0 10px 25px -4px rgba(59, 130, 246, 0.42);
      transition: all 0.2s cubic-bezier(0.16, 1, 0.3, 1);
    }
    .btn-gradient:hover:not(:disabled) {
      background: linear-gradient(180deg, #60a5fa 0%, #2563eb 100%);
      box-shadow: 0 14px 28px -4px rgba(59, 130, 246, 0.5);
      transform: translateY(-1px);
    }
    .btn-gradient:active:not(:disabled) {
      transform: translateY(0);
      box-shadow: 0 6px 16px -4px rgba(59, 130, 246, 0.4);
    }
    .input-shadow {
      box-shadow: 0 4px 24px -2px rgba(15, 23, 42, 0.04), 0 2px 6px -1px rgba(15, 23, 42, 0.02);
    }
    .card-shadow {
      box-shadow: 0 12px 36px -4px rgba(15, 23, 42, 0.06), 0 4px 12px -2px rgba(15, 23, 42, 0.03);
    }
  </style>
</head>
<body class="min-h-screen flex flex-col justify-between text-slate-800 antialiased selection:bg-blue-500 selection:text-white">

  <!-- Main View -->
  <main class="w-full max-w-4xl mx-auto px-4 pt-16 sm:pt-24 pb-12 flex-1 flex flex-col items-center">
    
    <!-- Logo Badge -->
    <div class="flex items-center gap-3 mb-10 select-none">
      <div class="w-12 h-12 rounded-2xl bg-gradient-to-b from-[#60a5fa] to-[#3b82f6] flex items-center justify-center text-white shadow-md shadow-blue-500/30">
        <svg class="w-6 h-6" fill="none" viewBox="0 0 24 24" stroke="currentColor">
          <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2.5" d="M19 14l-7 7m0 0l-7-7m7 7V3"/>
        </svg>
      </div>
      <span class="text-2xl font-extrabold tracking-tight text-slate-800">Видеоскач</span>
    </div>

    <!-- Hero Titles -->
    <div class="text-center space-y-3.5 max-w-xl mx-auto mb-10">
      <h1 class="text-3xl sm:text-5xl font-extrabold tracking-tight text-slate-900 leading-[1.18]">
        Быстрое скачивание<br>видео по ссылке
      </h1>
      <p class="text-slate-500 text-sm sm:text-base font-normal leading-relaxed">
        Просто вставьте ссылку на видео,<br class="hidden sm:inline">
        и скачайте его на своё устройство.
      </p>
    </div>

    <!-- Input Form Area -->
    <div class="w-full max-w-2xl mx-auto flex flex-col items-center gap-4">
      <div class="w-full relative">
        <div class="absolute inset-y-0 left-0 pl-5 flex items-center pointer-events-none text-slate-400">
          <!-- Link Icon -->
          <svg class="w-5 h-5" fill="none" viewBox="0 0 24 24" stroke="currentColor">
            <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M13.828 10.172a4 4 0 00-5.656 0l-4 4a4 4 0 105.656 5.656l1.102-1.101m-.758-4.899a4 4 0 005.656 0l4-4a4 4 0 00-5.656-5.656l-1.1 1.1"/>
          </svg>
        </div>
        <input
          id="urlInput"
          type="text"
          placeholder="Вставьте ссылку на видео"
          class="w-full h-14 pl-13 pr-24 rounded-full bg-white border border-slate-200/90 text-slate-800 text-base placeholder-slate-400 focus:outline-none focus:border-blue-500 focus:ring-4 focus:ring-blue-500/15 input-shadow transition-all"
          style="padding-left: 3.25rem;"
          autofocus
        >
        <button
          id="pasteBtn"
          type="button"
          class="absolute right-2 top-2 bottom-2 px-4 rounded-full bg-slate-100 hover:bg-slate-200/80 text-slate-600 text-xs font-bold transition flex items-center gap-1 select-none"
        >
          Вставить
        </button>
      </div>

      <!-- Action Button -->
      <button
        id="actionBtn"
        type="button"
        class="w-full sm:w-72 h-13 py-3.5 px-8 rounded-full text-white font-bold text-base flex items-center justify-center gap-2.5 btn-gradient cursor-pointer select-none"
      >
        <svg id="actionIcon" class="w-5 h-5 text-white" fill="none" viewBox="0 0 24 24" stroke="currentColor">
          <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2.5" d="M19 14l-7 7m0 0l-7-7m7 7V3"/>
        </svg>
        <span id="actionSpinner" class="w-5 h-5 border-2 border-white/30 border-t-white rounded-full animate-spin hidden"></span>
        <span id="actionText">Скачать видео</span>
      </button>

      <!-- Error Toast -->
      <div id="errorToast" class="w-full max-w-xl p-3.5 bg-rose-50 border border-rose-200/80 text-rose-700 text-xs font-medium rounded-2xl hidden flex items-center gap-2">
        <svg class="w-4 h-4 text-rose-500 shrink-0" fill="none" viewBox="0 0 24 24" stroke="currentColor">
          <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 8v4m0 4h.01M21 12a9 9 0 11-18 0 9 9 0 0118 0z"/>
        </svg>
        <span id="errorMessage" class="flex-1">Ошибка при поиске видео</span>
      </div>
    </div>

    <!-- Video Preview & Download Card (Expands when link is processed) -->
    <div id="mediaCard" class="w-full max-w-2xl mt-6 bg-white border border-slate-200/90 rounded-3xl p-6 card-shadow hidden space-y-5 transition-all">
      <div class="flex flex-col sm:flex-row gap-4 items-start">
        <img id="thumbImg" src="" alt="Thumbnail" class="w-full sm:w-44 h-28 object-cover rounded-2xl bg-slate-100 border border-slate-200 shrink-0">
        <div class="flex-1 min-w-0 space-y-1.5">
          <div class="flex items-center gap-2 flex-wrap">
            <span id="sourceTag" class="px-2.5 py-0.5 rounded-full text-[11px] font-bold bg-blue-50 text-blue-600 border border-blue-100">YouTube</span>
            <span id="durationTag" class="px-2.5 py-0.5 rounded-full text-[11px] font-semibold bg-slate-100 text-slate-600">03:45</span>
          </div>
          <h3 id="videoTitle" class="text-sm sm:text-base font-bold text-slate-900 leading-snug line-clamp-2">Название видео</h3>
          <p id="authorName" class="text-xs text-slate-400 font-medium">Автор</p>
        </div>
      </div>

      <div class="border-t border-slate-100 pt-4 space-y-3">
        <label class="block text-xs font-bold text-slate-500 uppercase tracking-wider">Качество:</label>
        <div class="relative">
          <select id="formatSelect" class="w-full h-11 px-4 rounded-xl bg-slate-50 border border-slate-200 text-sm font-semibold text-slate-800 focus:outline-none focus:border-blue-500 appearance-none">
          </select>
          <div class="absolute inset-y-0 right-0 flex items-center pr-3 pointer-events-none text-slate-400">
            <svg class="w-4 h-4" fill="none" viewBox="0 0 24 24" stroke="currentColor">
              <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 9l-7 7-7-7"/>
            </svg>
          </div>
        </div>

        <button
          id="startDownloadBtn"
          type="button"
          class="w-full h-12 rounded-xl text-white font-bold text-sm flex items-center justify-center gap-2 btn-gradient cursor-pointer"
        >
          <svg class="w-4 h-4" fill="none" viewBox="0 0 24 24" stroke="currentColor">
            <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2.5" d="M4 16v1a3 3 0 003 3h10a3 3 0 003-3v-1m-4-4l-4 4m0 0l-4-4m4 4V4"/>
          </svg>
          <span>Начать загрузку</span>
        </button>
      </div>

      <!-- Live Progress -->
      <div id="progressArea" class="border-t border-slate-100 pt-4 space-y-2 hidden">
        <div class="flex justify-between text-xs font-semibold text-slate-600">
          <span id="progressStatus">Загрузка файла...</span>
          <span id="progressPercent" class="text-blue-600 font-bold">0%</span>
        </div>
        <div class="w-full bg-slate-100 h-2.5 rounded-full overflow-hidden">
          <div id="progressBar" class="bg-blue-500 h-full w-0 transition-all duration-200 rounded-full"></div>
        </div>
        <div class="flex justify-between text-[11px] text-slate-400 font-medium">
          <span id="progressSpeed">Скорость: -</span>
          <span id="progressEta">Осталось: -</span>
        </div>
      </div>

      <!-- Download Complete Box -->
      <div id="completeBox" class="p-4 bg-emerald-50 border border-emerald-200/80 rounded-2xl space-y-3 hidden">
        <div class="flex items-center gap-2 text-emerald-800 font-bold text-sm">
          <svg class="w-5 h-5 text-emerald-600" fill="none" viewBox="0 0 24 24" stroke="currentColor">
            <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2.2" d="M5 13l4 4L19 7"/>
          </svg>
          <span>Файл готов!</span>
        </div>
        <p id="fileDetails" class="text-xs text-emerald-700">Видео сохранено в папку downloads</p>
        <div class="flex flex-wrap gap-2 pt-1">
          <a id="browserDownloadLink" href="#" download class="px-4 py-2 rounded-xl bg-emerald-600 hover:bg-emerald-700 text-white font-bold text-xs shadow-sm transition">
            💾 Скачать на диск
          </a>
          <button id="openFolderBtn" type="button" class="px-4 py-2 rounded-xl bg-white hover:bg-slate-50 border border-emerald-300 text-emerald-800 font-bold text-xs shadow-sm transition">
            📁 Открыть папку
          </button>
        </div>
      </div>
    </div>

    <!-- Supported Platforms Icons (Exact match from /design) -->
    <div class="mt-14 w-full flex flex-col items-center">
      <p class="text-xs font-medium text-slate-400 mb-6">Поддерживаются популярные платформы:</p>
      
      <div class="flex items-center justify-center gap-6 sm:gap-9 flex-wrap">
        
        <!-- YouTube -->
        <div class="flex flex-col items-center gap-2 select-none group cursor-pointer" onclick="fillPlatform('https://youtube.com')">
          <div class="w-11 h-11 rounded-2xl bg-[#ff0000] flex items-center justify-center text-white shadow-sm transition group-hover:scale-105">
            <svg class="w-6 h-6 fill-current" viewBox="0 0 24 24">
              <path d="M23.498 6.186a3.016 3.016 0 0 0-2.122-2.136C19.505 3.545 12 3.545 12 3.545s-7.505 0-9.377.505A3.017 3.017 0 0 0 .502 6.186C0 8.07 0 12 0 12s0 3.93.502 5.814a3.016 3.016 0 0 0 2.122 2.136c1.871.505 9.376.505 9.376.505s7.505 0 9.377-.505a3.015 3.015 0 0 0 2.122-2.136C24 15.93 24 12 24 12s0-3.93-.502-5.814zM9.545 15.568V8.432L15.818 12l-6.273 3.568z"/>
            </svg>
          </div>
          <span class="text-xs font-medium text-slate-500">YouTube</span>
        </div>

        <!-- TikTok -->
        <div class="flex flex-col items-center gap-2 select-none group cursor-pointer" onclick="fillPlatform('https://tiktok.com')">
          <div class="w-11 h-11 rounded-2xl bg-black flex items-center justify-center text-white shadow-sm transition group-hover:scale-105">
            <svg class="w-5 h-5 fill-current" viewBox="0 0 24 24">
              <path d="M12.525.02c1.31-.02 2.61-.01 3.91-.02.08 1.53.63 3.09 1.75 4.17 1.12 1.11 2.7 1.62 4.24 1.79v4.03c-1.44-.05-2.89-.35-4.2-.97-.57-.26-1.1-.59-1.62-.93-.01 2.92.01 5.84-.02 8.75-.08 1.4-.54 2.79-1.35 3.94-1.31 1.92-3.58 3.17-5.91 3.21-1.43.08-2.86-.31-4.08-1.03-2.02-1.19-3.44-3.37-3.65-5.71-.02-.5-.03-1-.01-1.49.18-1.9 1.12-3.72 2.58-4.96 1.66-1.44 3.98-2.13 6.15-1.72.02 1.48-.04 2.96-.04 4.44-.99-.32-2.15-.23-3.02.37-.63.41-1.11 1.04-1.36 1.75-.21.51-.24 1.07-.14 1.61.24 1.64 1.82 3.02 3.5 2.87 1.12-.01 2.19-.66 2.77-1.61.19-.33.4-.67.41-1.06.1-1.79.06-3.57.07-5.36.01-4.03-.01-8.05.02-12.07z"/>
            </svg>
          </div>
          <span class="text-xs font-medium text-slate-500">TikTok</span>
        </div>

        <!-- Instagram -->
        <div class="flex flex-col items-center gap-2 select-none group cursor-pointer" onclick="fillPlatform('https://instagram.com')">
          <div class="w-11 h-11 rounded-2xl bg-gradient-to-tr from-[#f9ce34] via-[#ee2a7b] to-[#6228d7] flex items-center justify-center text-white shadow-sm transition group-hover:scale-105">
            <svg class="w-5 h-5 fill-current" viewBox="0 0 24 24">
              <path d="M12 2.163c3.204 0 3.584.012 4.85.07 3.252.148 4.771 1.691 4.919 4.919.058 1.265.069 1.645.069 4.849 0 3.205-.012 3.584-.069 4.849-.149 3.225-1.664 4.771-4.919 4.919-1.266.058-1.644.07-4.85.07-3.204 0-3.584-.012-4.849-.07-3.26-.149-4.771-1.699-4.919-4.92-.058-1.265-.07-1.644-.07-4.849 0-3.204.013-3.583.07-4.849.149-3.227 1.664-4.771 4.919-4.919 1.266-.057 1.645-.069 4.849-.069zm0-2.163c-3.259 0-3.667.014-4.947.072-4.358.2-6.78 2.618-6.98 6.98-.059 1.281-.073 1.689-.073 4.948 0 3.259.014 3.668.072 4.948.2 4.358 2.618 6.78 6.98 6.98 1.281.058 1.689.072 4.948.072 3.259 0 3.668-.014 4.948-.072 4.354-.2 6.782-2.618 6.979-6.98.059-1.28.073-1.689.073-4.948 0-3.259-.014-3.667-.072-4.947-.196-4.354-2.617-6.78-6.979-6.98-1.281-.059-1.69-.073-4.949-.073zm0 5.838c-3.403 0-6.162 2.759-6.162 6.162s2.759 6.163 6.162 6.163 6.162-2.759 6.162-6.163c0-3.403-2.759-6.162-6.162-6.162zm0 10.162c-2.209 0-4-1.79-4-4 0-2.209 1.791-4 4-4s4 1.791 4 4c0 2.21-1.791 4-4 4zm6.406-11.845c-.796 0-1.441.645-1.441 1.44s.645 1.44 1.441 1.44c.795 0 1.439-.645 1.439-1.44s-.644-1.44-1.439-1.44z"/>
            </svg>
          </div>
          <span class="text-xs font-medium text-slate-500">Instagram</span>
        </div>

        <!-- ВКонтакте -->
        <div class="flex flex-col items-center gap-2 select-none group cursor-pointer" onclick="fillPlatform('https://vk.com/video')">
          <div class="w-11 h-11 rounded-2xl bg-[#0077ff] flex items-center justify-center text-white shadow-sm transition group-hover:scale-105">
            <svg class="w-5 h-5 fill-current" viewBox="0 0 24 24">
              <path d="M15.072 17.5c-4.938 0-7.75-3.375-7.875-9h2.469c.094 4.125 1.906 5.875 3.344 6.25v-6.25h2.344v3.563c1.438-.156 2.906-1.781 3.406-3.563h2.344c-.406 2.25-2.031 3.875-3.219 4.563 1.219.563 3.031 2 3.656 4.438h-2.594c-.656-2.031-2.281-3.594-3.969-3.75v3.75h-.906z"/>
            </svg>
          </div>
          <span class="text-xs font-medium text-slate-500">ВКонтакте</span>
        </div>

        <!-- Telegram -->
        <div class="flex flex-col items-center gap-2 select-none group cursor-pointer" onclick="fillPlatform('https://t.me')">
          <div class="w-11 h-11 rounded-2xl bg-[#2aabee] flex items-center justify-center text-white shadow-sm transition group-hover:scale-105">
            <svg class="w-5 h-5 fill-current" viewBox="0 0 24 24">
              <path d="M12 0C5.373 0 0 5.373 0 12s5.373 12 12 12 12-5.373 12-12S18.627 0 12 0zm5.894 8.221l-1.97 9.28c-.145.658-.537.818-1.084.508l-3-2.21-1.446 1.394c-.16.16-.295.295-.605.295l.213-3.053 5.56-5.023c.242-.213-.054-.333-.373-.121l-6.871 4.326-2.96-.924c-.643-.204-.657-.643.136-.953l11.57-4.461c.537-.194 1.006.128.832.942z"/>
            </svg>
          </div>
          <span class="text-xs font-medium text-slate-500">Telegram</span>
        </div>

        <!-- и другие -->
        <div class="flex flex-col items-center gap-2 select-none group cursor-pointer" onclick="fillPlatform('')">
          <div class="w-11 h-11 rounded-2xl bg-slate-200/80 flex items-center justify-center text-slate-500 shadow-sm transition group-hover:scale-105">
            <svg class="w-5 h-5 fill-current" viewBox="0 0 24 24">
              <circle cx="5" cy="12" r="2"/>
              <circle cx="12" cy="12" r="2"/>
              <circle cx="19" cy="12" r="2"/>
            </svg>
          </div>
          <span class="text-xs font-medium text-slate-500">и другие</span>
        </div>

      </div>
    </div>

    <!-- Downloaded History Section (Minimalist & Compact) -->
    <div class="w-full max-w-2xl mt-12 pt-6 border-t border-slate-200/80">
      <div class="flex items-center justify-between mb-3 px-1">
        <span class="text-xs font-bold text-slate-400 uppercase tracking-wider">История скачиваний</span>
        <button id="refreshFilesBtn" class="text-xs text-blue-600 hover:text-blue-700 font-semibold transition">Обновить</button>
      </div>
      <div id="historyList" class="space-y-2">
        <p class="text-xs text-slate-400 py-1 px-1">Файлы пока не скачивались</p>
      </div>
    </div>

  </main>

  <!-- Footer (Exact match from /design) -->
  <footer class="w-full border-t border-slate-200/70 py-6 text-center text-xs text-slate-400">
    <div class="flex items-center justify-center gap-4">
      <a href="#" class="hover:text-slate-600 transition">О нас</a>
      <span>|</span>
      <a href="#" class="hover:text-slate-600 transition">Помощь</a>
      <span>|</span>
      <a href="#" class="hover:text-slate-600 transition">Контакты</a>
    </div>
  </footer>

  <script>
    const urlInput = document.getElementById('urlInput');
    const pasteBtn = document.getElementById('pasteBtn');
    const actionBtn = document.getElementById('actionBtn');
    const actionText = document.getElementById('actionText');
    const actionIcon = document.getElementById('actionIcon');
    const actionSpinner = document.getElementById('actionSpinner');
    const errorToast = document.getElementById('errorToast');
    const errorMessage = document.getElementById('errorMessage');

    const mediaCard = document.getElementById('mediaCard');
    const thumbImg = document.getElementById('thumbImg');
    const sourceTag = document.getElementById('sourceTag');
    const durationTag = document.getElementById('durationTag');
    const videoTitle = document.getElementById('videoTitle');
    const authorName = document.getElementById('authorName');
    const formatSelect = document.getElementById('formatSelect');
    const startDownloadBtn = document.getElementById('startDownloadBtn');

    const progressArea = document.getElementById('progressArea');
    const progressBar = document.getElementById('progressBar');
    const progressStatus = document.getElementById('progressStatus');
    const progressPercent = document.getElementById('progressPercent');
    const progressSpeed = document.getElementById('progressSpeed');
    const progressEta = document.getElementById('progressEta');

    const completeBox = document.getElementById('completeBox');
    const fileDetails = document.getElementById('fileDetails');
    const browserDownloadLink = document.getElementById('browserDownloadLink');
    const openFolderBtn = document.getElementById('openFolderBtn');
    const historyList = document.getElementById('historyList');
    const refreshFilesBtn = document.getElementById('refreshFilesBtn');

    let currentMeta = null;
    let pollInterval = null;

    function fillPlatform(url) {
      if (url) {
        urlInput.value = url;
        urlInput.focus();
      } else {
        urlInput.focus();
      }
    }

    pasteBtn.addEventListener('click', async () => {
      try {
        const text = await navigator.clipboard.readText();
        if (text) {
          urlInput.value = text.trim();
          handleInspect();
        }
      } catch (e) {
        urlInput.focus();
      }
    });

    urlInput.addEventListener('keydown', (e) => {
      if (e.key === 'Enter') handleInspect();
    });

    actionBtn.addEventListener('click', handleInspect);

    async function handleInspect() {
      const url = urlInput.value.trim();
      if (!url) {
        urlInput.focus();
        return;
      }

      errorToast.classList.add('hidden');
      actionIcon.classList.add('hidden');
      actionSpinner.classList.remove('hidden');
      actionText.textContent = 'Поиск видео...';
      actionBtn.disabled = true;

      mediaCard.classList.add('hidden');
      completeBox.classList.add('hidden');
      progressArea.classList.add('hidden');

      try {
        const res = await fetch('/api/info', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ url })
        });
        const data = await res.json();
        if (!res.ok) throw new Error(data.detail || 'Не удалось распознать видео');

        currentMeta = data;
        renderMediaCard(data);
      } catch (err) {
        errorMessage.textContent = err.message;
        errorToast.classList.remove('hidden');
      } finally {
        actionSpinner.classList.add('hidden');
        actionIcon.classList.remove('hidden');
        actionText.textContent = 'Скачать видео';
        actionBtn.disabled = false;
      }
    }

    function renderMediaCard(data) {
      videoTitle.textContent = data.title;
      authorName.textContent = data.uploader ? `Автор: ${data.uploader}` : '';
      durationTag.textContent = data.duration_str || 'Видео';
      sourceTag.textContent = data.extractor || 'Web';

      if (data.thumbnail) {
        thumbImg.src = data.thumbnail;
        thumbImg.classList.remove('hidden');
      } else {
        thumbImg.classList.add('hidden');
      }

      formatSelect.innerHTML = '';
      data.formats.forEach(f => {
        const opt = document.createElement('option');
        opt.value = f.id;
        opt.dataset.ext = f.ext;
        opt.textContent = `${f.label} [${f.ext}]`;
        formatSelect.appendChild(opt);
      });

      mediaCard.classList.remove('hidden');
      mediaCard.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
    }

    startDownloadBtn.addEventListener('click', async () => {
      if (!currentMeta) return;

      const opt = formatSelect.options[formatSelect.selectedIndex];
      const quality = opt.value;
      const formatExt = opt.dataset.ext || 'mp4';

      startDownloadBtn.disabled = true;
      progressArea.classList.remove('hidden');
      completeBox.classList.add('hidden');
      progressStatus.classList.remove('text-rose-600');
      progressBar.style.width = '0%';
      progressPercent.textContent = '0%';
      progressStatus.textContent = 'Подключение к видеопотоку...';
      progressSpeed.textContent = '';
      progressEta.textContent = '';

      try {
        const res = await fetch('/api/download', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            url: currentMeta.url,
            quality: quality,
            format_ext: formatExt,
          })
        });
        const data = await res.json();
        if (!res.ok) throw new Error(data.detail || 'Ошибка старта загрузки');

        trackProgress(data.task_id);
      } catch (err) {
        progressStatus.textContent = 'Ошибка: ' + err.message;
        progressStatus.classList.add('text-rose-600');
        startDownloadBtn.disabled = false;
      }
    });

    function formatEtaText(eta) {
      if (!eta) return '';
      if (typeof eta === 'string' && (eta.includes('сек') || eta.includes('мин') || eta.includes('ч'))) {
        return eta;
      }
      const num = parseFloat(eta);
      if (isNaN(num) || num <= 0) return '';
      const totalSec = Math.round(num);
      if (totalSec < 60) return `${totalSec} сек`;
      const m = Math.floor(totalSec / 60);
      const s = totalSec % 60;
      return s > 0 ? `${m} мин ${s} сек` : `${m} мин`;
    }

    function trackProgress(taskId) {
      if (pollInterval) clearInterval(pollInterval);

      pollInterval = setInterval(async () => {
        try {
          const res = await fetch(`/api/progress/${taskId}`);
          const data = await res.json();

          if (data.status === 'downloading') {
            progressBar.style.width = `${data.percent}%`;
            progressPercent.textContent = `${data.percent}%`;
            progressSpeed.textContent = data.speed ? `Скорость: ${data.speed}` : '';
            const etaFormatted = formatEtaText(data.eta);
            progressEta.textContent = etaFormatted ? `Осталось: ${etaFormatted}` : '';
            progressStatus.textContent = 'Скачивание видео...';
          } else if (data.status === 'processing') {
            progressBar.style.width = '100%';
            progressPercent.textContent = '100%';
            progressStatus.textContent = data.message || 'Объединение дорожек через FFmpeg...';
          } else if (data.status === 'retrying') {
            progressBar.style.width = `${data.percent}%`;
            progressPercent.textContent = `${data.percent}%`;
            progressSpeed.textContent = '';
            progressEta.textContent = '';
            progressStatus.textContent = data.message || 'Переподключение...';
          } else if (data.status === 'finished') {
            clearInterval(pollInterval);
            progressBar.style.width = '100%';
            progressPercent.textContent = '100%';
            progressStatus.textContent = 'Завершено!';
            startDownloadBtn.disabled = false;

            fileDetails.textContent = `Файл: ${data.filename} (${data.size_mb} МБ)`;
            browserDownloadLink.href = `/api/download_file/${encodeURIComponent(data.filename)}`;
            browserDownloadLink.setAttribute('download', data.filename);
            completeBox.classList.remove('hidden');

            loadHistory();
          } else if (data.status === 'error') {
            clearInterval(pollInterval);
            progressStatus.textContent = 'Ошибка: ' + (data.error || 'Сбой скачивания');
            progressStatus.classList.add('text-rose-600');
            startDownloadBtn.disabled = false;
          }
        } catch (e) {
          console.error(e);
        }
      }, 500);
    }

    openFolderBtn.addEventListener('click', async () => {
      await fetch('/api/open_folder', { method: 'POST' });
    });

    async function loadHistory() {
      try {
        const res = await fetch('/api/files');
        const files = await res.json();
        if (!files || files.length === 0) {
          historyList.innerHTML = '<p class="text-xs text-slate-400 py-1 px-1">Файлы пока не скачивались</p>';
          return;
        }

        historyList.innerHTML = files.map(f => `
          <div class="flex items-center justify-between p-3 rounded-2xl bg-white border border-slate-200/80 hover:border-slate-300 transition">
            <div class="min-w-0 flex-1 pr-3">
              <div class="text-xs font-bold text-slate-800 truncate" title="${f.name}">🎬 ${f.name}</div>
              <div class="text-[11px] text-slate-400">${f.size_mb} МБ • ${f.date}</div>
            </div>
            <a href="/api/download_file/${encodeURIComponent(f.name)}" download class="px-3 py-1.5 bg-blue-50 hover:bg-blue-100 text-blue-600 font-bold rounded-xl text-xs transition shrink-0">
              Скачать
            </a>
          </div>
        `).join('');
      } catch (e) {
        historyList.innerHTML = '<p class="text-xs text-rose-500">Не удалось загрузить историю</p>';
      }
    }

    refreshFilesBtn.addEventListener('click', loadHistory);
    loadHistory();
  </script>
</body>
</html>
"""


class UrlReq(BaseModel):
    url: str


class DownloadReq(BaseModel):
    url: str
    quality: str = "best"
    format_ext: str = "mp4"


@app.get("/", response_class=HTMLResponse)
def index():
    return SINGLE_PAGE_HTML


@app.post("/api/info")
def info_api(req: UrlReq):
    try:
        return get_video_info(req.url)
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.post("/api/download")
def download_api(req: DownloadReq):
    task_id = str(uuid.uuid4())
    tasks_progress[task_id] = {
        "status": "starting",
        "percent": 0.0,
        "speed": "",
        "eta": "",
    }
    t = threading.Thread(
        target=run_download_task,
        args=(task_id, req.url, req.quality, req.format_ext),
        daemon=True,
    )
    t.start()
    return {"task_id": task_id}


@app.get("/api/progress/{task_id}")
def progress_api(task_id: str):
    p = tasks_progress.get(task_id)
    if not p:
        raise HTTPException(status_code=404, detail="Task not found")
    return p


@app.get("/api/files")
def files_api():
    files = []
    if os.path.exists(DOWNLOADS_DIR):
        for name in os.listdir(DOWNLOADS_DIR):
            fp = os.path.join(DOWNLOADS_DIR, name)
            if os.path.isfile(fp) and not name.endswith(".part"):
                size = os.path.getsize(fp)
                mtime = os.path.getmtime(fp)
                dt = datetime.datetime.fromtimestamp(mtime).strftime("%d.%m.%Y %H:%M")
                files.append({
                    "name": name,
                    "size_mb": round(size / (1024 * 1024), 2),
                    "date": dt,
                })
    files.sort(key=lambda x: x["date"], reverse=True)
    return files


@app.get("/api/download_file/{filename}")
def serve_file(filename: str):
    clean_name = os.path.basename(filename)
    fp = os.path.join(DOWNLOADS_DIR, clean_name)
    if not os.path.isfile(fp):
        raise HTTPException(status_code=404, detail="File not found")
    return FileResponse(fp, media_type="application/octet-stream", filename=clean_name)


@app.post("/api/open_folder")
def open_folder():
    try:
        if os.name == "nt":
            os.startfile(DOWNLOADS_DIR)
        else:
            subprocess.Popen(["xdg-open", DOWNLOADS_DIR])
        return {"status": "ok"}
    except Exception as e:
        return {"status": "error", "message": str(e)}


def find_available_port(start_port: int = 8000, max_attempts: int = 15) -> int:
    import socket
    for port in range(start_port, start_port + max_attempts):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    return start_port


def main():
    host = "127.0.0.1"
    port = find_available_port(8000)
    url = f"http://{host}:{port}"

    print("=" * 60)
    print(f"  Видеоскач запущен: {url}")
    print(f"  FFmpeg: {'[OK]' if is_ffmpeg_ready() else '[!] Не найден'}")
    print("=" * 60)

    # Open browser automatically after 1 second
    threading.Timer(1.0, lambda: webbrowser.open(url)).start()

    uvicorn.run(app, host=host, port=port, log_level="warning")


if __name__ == "__main__":
    main()
