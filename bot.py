import os
import re
import uuid
import time
import shutil
import asyncio
import threading
import yt_dlp

from urllib.parse import urlparse, urlunparse

from telegram import Update
from telegram.constants import ChatAction
from telegram.error import TimedOut, NetworkError, TelegramError
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

# =========================================================
# CONFIG
# =========================================================

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DOWNLOAD_DIR = os.path.join(BASE_DIR, "downloads")

# Cookie file otomatis terbaca dari folder project di GitHub
COOKIE_FILE = os.path.join(BASE_DIR, "cookies.txt")

os.makedirs(DOWNLOAD_DIR, exist_ok=True)

CONCURRENT_FRAGMENTS = 8
PROGRESS_UPDATE_INTERVAL = 1.2
UPLOAD_TIMEOUT = 600

ACTIVE_JOBS = {}
progress_lock = threading.Lock()

# =========================================================
# UTILITIES & PLATFORM
# =========================================================

def get_url(text):
    if not text:
        return None
    pattern = r"https?://[^\s]+"
    match = re.search(pattern, text)
    if not match:
        return None
    return match.group(0).rstrip(".,);]}>\"'")

def detect_platform(url):
    try:
        domain = urlparse(url).netloc.lower()
        if "instagram.com" in domain or "instagr.am" in domain:
            return "Instagram", "📸"
        if "facebook.com" in domain or "fb.watch" in domain:
            return "Facebook", "🔵"
        if "tiktok.com" in domain:
            return "TikTok", "🎵"
        if "youtube.com" in domain or "youtu.be" in domain:
            return "YouTube", "🔴"
        if "threads.net" in domain or "threads.com" in domain:
            return "Threads", "🧵"
        if "twitter.com" in domain or "x.com" in domain:
            return "X / Twitter", "🐦"
        return domain.replace("www.", ""), "🌐"
    except Exception:
        return "Website", "🌐"

def is_instagram_url(url):
    try:
        domain = urlparse(url).netloc.lower()
        return "instagram.com" in domain or "instagr.am" in domain
    except Exception:
        return False

def clean_url(url):
    try:
        parsed = urlparse(url)
        domain = parsed.netloc.lower()
        if "instagram.com" in domain or "instagr.am" in domain:
            cleaned = parsed._replace(query="", fragment="")
            return urlunparse(cleaned)
        return url
    except Exception:
        return url

def cookie_status():
    if not os.path.isfile(COOKIE_FILE):
        return False, "File cookies.txt tidak ditemukan"
    try:
        if os.path.getsize(COOKIE_FILE) <= 0:
            return False, "cookies.txt kosong"
        with open(COOKIE_FILE, "r", encoding="utf-8", errors="ignore") as file:
            first_part = file.read(500)
        if "Netscape HTTP Cookie File" not in first_part and "# HTTP Cookie File" not in first_part:
            return False, "cookies.txt bukan format Netscape"
        return True, "READY"
    except Exception as e:
        return False, str(e)

def format_bytes(size):
    if size is None:
        return "?"
    try:
        size = float(size)
    except Exception:
        return "?"
    units = ["B", "KB", "MB", "GB", "TB"]
    for unit in units:
        if size < 1024:
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} PB"

def format_speed(speed):
    if not speed:
        return "..."
    return f"{format_bytes(speed)}/s"

def format_time(seconds):
    if seconds is None:
        return "..."
    try:
        seconds = int(seconds)
    except Exception:
        return "..."
    if seconds < 0:
        return "..."
    minutes, seconds = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours:02d}:{minutes:02d}:{seconds:02d}"
    return f"{minutes:02d}:{seconds:02d}"

def format_duration(seconds):
    if not seconds:
        return "?"
    try:
        seconds = int(seconds)
    except Exception:
        return "?"
    minutes, seconds = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{seconds:02d}"
    return f"{minutes}:{seconds:02d}"

def safe_title(title, max_length=70):
    if not title:
        return "Video"
    title = str(title).replace("<", "").replace(">", "").replace("&", "dan")
    if len(title) > max_length:
        title = title[:max_length - 3] + "..."
    return title

def progress_bar(percent, length=12):
    try:
        percent = float(percent)
    except Exception:
        percent = 0
    percent = max(0, min(100, percent))
    filled = round(length * percent / 100)
    return "█" * filled + "░" * (length - filled)

def create_progress_data():
    return {
        "status": "starting",
        "percent": 0,
        "downloaded": 0,
        "total": 0,
        "speed": 0,
        "eta": None,
        "filename": "",
        "auth": False,
        "updated": time.time(),
    }

def make_progress_hook(progress_data):
    def hook(d):
        with progress_lock:
            status = d.get("status")
            if status == "downloading":
                downloaded = d.get("downloaded_bytes", 0)
                total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
                percent = (downloaded / total) * 100 if total else 0
                progress_data.update({
                    "status": "downloading",
                    "percent": percent,
                    "downloaded": downloaded,
                    "total": total,
                    "speed": d.get("speed"),
                    "eta": d.get("eta"),
                    "filename": d.get("filename", ""),
                    "updated": time.time(),
                })
            elif status == "finished":
                progress_data.update({
                    "status": "processing",
                    "percent": 100,
                    "speed": 0,
                    "eta": 0,
                    "updated": time.time(),
                })
    return hook

def build_ydl_options(url, output_template, progress_hook):
    options = {
        "format": "best[ext=mp4][vcodec!=none][acodec!=none]/bestvideo[ext=mp4]+bestaudio[ext=m4a]/bestvideo+bestaudio/best",
        "outtmpl": output_template,
        "merge_output_format": "mp4",
        "noplaylist": True,
        "concurrent_fragment_downloads": CONCURRENT_FRAGMENTS,
        "retries": 3,
        "fragment_retries": 3,
        "socket_timeout": 30,
        "progress_hooks": [progress_hook],
        "quiet": True,
        "no_warnings": False,
        "windowsfilenames": True,
    }
    if is_instagram_url(url):
        cookie_ok, cookie_message = cookie_status()
        if not cookie_ok:
            raise RuntimeError(f"Instagram membutuhkan cookies.txt.\n\nStatus: {cookie_message}")
        options["cookiefile"] = COOKIE_FILE
    return options

def download_video(url, progress_data):
    job_id = str(uuid.uuid4())[:8]
    job_folder = os.path.join(DOWNLOAD_DIR, job_id)
    os.makedirs(job_folder, exist_ok=True)
    output_template = os.path.join(job_folder, "%(title).80s_%(id)s.%(ext)s")
    progress_hook = make_progress_hook(progress_data)
    clean_download_url = clean_url(url)

    if is_instagram_url(clean_download_url):
        with progress_lock:
            progress_data.update({"status": "authenticating", "auth": True, "updated": time.time()})

    ydl_opts = build_ydl_options(clean_download_url, output_template, progress_hook)

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(clean_download_url, download=True)

        title = info.get("title", "Video")
        uploader = info.get("uploader") or info.get("channel") or info.get("creator") or "Unknown"
        duration = info.get("duration")
        webpage_url = info.get("webpage_url", clean_download_url)

        files = []
        for filename in os.listdir(job_folder):
            path = os.path.join(job_folder, filename)
            if not os.path.isfile(path) or filename.endswith((".part", ".ytdl", ".temp")):
                continue
            files.append(path)

        if not files:
            raise RuntimeError("File hasil download tidak ditemukan.")

        video_extensions = (".mp4", ".mkv", ".webm", ".mov", ".m4v")
        video_files = [f for f in files if f.lower().endswith(video_extensions)]
        if video_files:
            files = video_files

        files.sort(key=os.path.getsize, reverse=True)
        return {
            "file": files[0],
            "folder": job_folder,
            "title": title,
            "uploader": uploader,
            "duration": duration,
            "url": webpage_url,
            "used_cookie": is_instagram_url(clean_download_url),
        }
    except Exception:
        raise

async def monitor_progress(status_message, progress_data, platform, icon, stop_event):
    last_text = ""
    while not stop_event.is_set():
        await asyncio.sleep(PROGRESS_UPDATE_INTERVAL)
        with progress_lock:
            data = dict(progress_data)
        status = data.get("status")

        if status == "authenticating":
            text = f"⚡ MAN'S VIDEO DOWNLOADER\n\n{icon} {platform}\n\n🍪 Memuat session Instagram...\n⏳ Mohon tunggu..."
        elif status == "downloading":
            percent = data.get("percent", 0)
            downloaded = format_bytes(data.get("downloaded"))
            total = format_bytes(data.get("total"))
            speed = format_speed(data.get("speed"))
            eta = format_time(data.get("eta"))
            bar = progress_bar(percent)
            text = f"⚡ MAN'S VIDEO DOWNLOADER\n\n{icon} {platform}\n\n⬇️ DOWNLOAD\n{bar}  {percent:.1f}%\n\n📦 {downloaded} / {total}\n🚀 {speed}\n⏱ ETA {eta}"
        elif status == "processing":
            text = f"⚡ MAN'S VIDEO DOWNLOADER\n\n{icon} {platform}\n\n⚙️ Memproses video..."
        else:
            text = f"⚡ MAN'S VIDEO DOWNLOADER\n\n{icon} {platform}\n\n🔎 Menganalisis link...\n⏳ Mohon tunggu..."

        if text != last_text:
            try:
                await status_message.edit_text(text)
                last_text = text
            except TelegramError:
                pass

async def upload_video(update, video_path, title, platform, icon, duration, uploader, status_message):
    file_size = os.path.getsize(video_path)
    file_size_text = format_bytes(file_size)
    title_short = safe_title(title)
    uploader_short = safe_title(uploader, 40)
    duration_text = format_duration(duration)

    await status_message.edit_text(f"⚡ MAN'S VIDEO DOWNLOADER\n\n✅ Download selesai!\n📤 Mengirim ke Telegram...")

    try:
        await update.effective_chat.send_action(ChatAction.UPLOAD_VIDEO)
        with open(video_path, "rb") as video:
            await update.message.reply_video(
                video=video,
                caption=f"✅ DOWNLOAD COMPLETE\n\n{icon} {platform}\n🎬 {title_short}\n👤 {uploader_short}\n⏱ {duration_text}\n📦 {file_size_text}",
                supports_streaming=True,
                filename=os.path.basename(video_path),
                write_timeout=UPLOAD_TIMEOUT,
                read_timeout=UPLOAD_TIMEOUT,
            )
        return True
    finally:
        pass

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cookie_ok, _ = cookie_status()
    ig_status = "🟢 READY" if cookie_ok else "🔴 COOKIE ERROR"
    text = f"⚡ MAN'S VIDEO DOWNLOADER V3\n\n🎬 Kirim link video (Instagram, TikTok, YouTube, dll) dan bot akan mendownloadnya.\n\nInstagram Cookie: {ig_status}"
    await update.message.reply_text(text, disable_web_page_preview=True)

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🛠 Kirimkan saja link video publik yang ingin didownload.")

async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(f"⚡ SYSTEM ONLINE\nJob aktif: {len(ACTIVE_JOBS)}")

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text or ""
    url = get_url(text)
    if not url:
        await update.message.reply_text("❌ Link tidak ditemukan.")
        return

    url = clean_url(url)
    user_id = update.effective_user.id if update.effective_user else 0
    job_key = f"{user_id}_{uuid.uuid4().hex[:6]}"
    platform, icon = detect_platform(url)
    progress_data = create_progress_data()
    ACTIVE_JOBS[job_key] = {"platform": platform, "start": time.time()}

    job_folder = None
    status_message = await update.message.reply_text(f"⚡ Menganalisis link {platform}...")
    stop_monitor = asyncio.Event()
    monitor_task = asyncio.create_task(monitor_progress(status_message, progress_data, platform, icon, stop_monitor))

    try:
        result = await asyncio.to_thread(download_video, url, progress_data)
        job_folder = result["folder"]
        stop_monitor.set()
        await monitor_task

        await upload_video(
            update=update,
            video_path=result["file"],
            title=result["title"],
            platform=platform,
            icon=icon,
            duration=result["duration"],
            uploader=result["uploader"],
            status_message=status_message,
        )
        await status_message.edit_text("✅ Selesai!")
    except Exception as e:
        stop_monitor.set()
        await status_message.edit_text(f"❌ Gagal: {str(e)[:500]}")
    finally:
        ACTIVE_JOBS.pop(job_key, None)
        if job_folder:
            shutil.rmtree(job_folder, ignore_errors=True)

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    print("Error:", context.error)

def main():
    if not BOT_TOKEN:
        print("ERROR: TELEGRAM_BOT_TOKEN belum diset!")
        return

    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("status", status_command))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_error_handler(error_handler)
    
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
