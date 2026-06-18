"""
Wildlife Video Downloader - Telegram Bot
- Supports 1000+ sites: Vimeo, archive.org, Dailymotion, Facebook, TikTok, Twitter, etc.
- Shows video name + size before downloading
- Downloads with live progress bar
- Splits videos into 2-minute clips and sends each one to Telegram

COMMANDS:
/start         - Welcome message
/list          - Built-in wildlife videos
/dl <number>   - Download built-in video
/dl <url>      - Download from ANY supported site (Vimeo, archive.org, etc.)
/dlall <url>   - Download entire archive.org collection
/cancel        - Cancel active download
/status        - Check download status
/subscribe     - Subscribe (₦3,000 / 14 days)
/verify        - Verify payment & activate subscription
"""

import os
import re
import json
import time
import sqlite3
import subprocess
import threading
import asyncio
import requests
from datetime import datetime, timedelta
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes

BOT_TOKEN = "8918950077:AAFH8Siv7UA-kh99KQyvNyMer_apo3zdRe8"

DOWNLOAD_FOLDER = "/tmp/wildlife"
os.makedirs(DOWNLOAD_FOLDER, exist_ok=True)

CLIP_DURATION = 120  # 2 minutes per clip
TELEGRAM_MAX_MB = 50

# ── Paystack billing config ─────────────────────────────────────────────
PAYSTACK_SECRET_KEY = os.environ.get("PAYSTACK_SECRET_KEY", "")
PAYSTACK_BASE = "https://api.paystack.co"
SUBSCRIPTION_AMOUNT_KOBO = 300000  # ₦3,000
SUBSCRIPTION_PERIOD_DAYS = 14
DB_PATH = os.environ.get("DB_PATH", "/tmp/wildlife/subscribers.db")  # set to a path on the
# Render persistent disk in production so this survives redeploys

VIDEOS = {
    1:  {"name": "Ice Fox - Arctic Survival",            "url": "https://archive.org/download/Natural_History_Wildlife/Nature%201993%20-%20Ice%20Fox.mp4"},
    2:  {"name": "The Face of the Deep - Ocean",         "url": "https://archive.org/download/Natural_History_Wildlife/Nature%201984%20-%20The%20Face%20of%20the%20Deep.mp4"},
    3:  {"name": "Shadows in a Desert Sea",              "url": "https://archive.org/download/Natural_History_Wildlife/Nature%201992%20-%20Shadows%20in%20a%20Desert%20Sea.mp4"},
    4:  {"name": "Echoes from the Ice - Alaska",         "url": "https://archive.org/download/Natural_History_Wildlife/Nature%201993%20-%20Echoes%20from%20the%20Ice.mp4"},
    5:  {"name": "Grand Canyon",                         "url": "https://archive.org/download/Natural_History_Wildlife/Nature%201997%20-%20Grand%20Canyon.mp4"},
    6:  {"name": "American Trickster - Coyote",          "url": "https://archive.org/download/Natural_History_Wildlife/Nature%201993%20-%20American%20Trickster.mp4"},
    7:  {"name": "The Call of Kakadu - Australia",       "url": "https://archive.org/download/Natural_History_Wildlife/Nature%201996%20-%20The%20Call%20of%20Kakadu.mp4"},
    8:  {"name": "Scandinavia - Midnight Sun",           "url": "https://archive.org/download/Natural_History_Wildlife/Nature%201990%20-%20Scandinavia%20Part%201%20-%20Land%20of%20the%20Midnight%20Sun.mp4"},
    9:  {"name": "Scandinavia - Fresh Waters Salt Seas", "url": "https://archive.org/download/Natural_History_Wildlife/Nature%201990%20-%20Scandinavia%20Part%202%20-%20Fresh%20Waters%2C%20Salt%20Seas.mp4"},
    10: {"name": "Lost World of the Medusa",             "url": "https://archive.org/download/Natural_History_Wildlife/Nature%201985%20-%20Lost%20World%20of%20the%20Medusa.mp4"},
    11: {"name": "Fungi - The Rotten World About Us",    "url": "https://archive.org/download/Natural_History_Wildlife/Nature%201983%20-%20Fungi%20-%20The%20Rotten%20World%20About%20Us.mp4"},
}

active_downloads = {}
cancel_flags = {}
active_processes = {}  # stores yt-dlp subprocess so cancel can kill it
bot_loop = None
bot_instance = None  # set in __main__, used by the billing thread


# ── Subscriber store (sqlite) ───────────────────────────────────────────

SUB_COLUMNS = ["chat_id", "email", "authorization_code", "reference", "active", "next_charge_at", "created_at"]
db_lock = threading.Lock()

def db_init():
    os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)
    with db_lock:
        conn = sqlite3.connect(DB_PATH)
        try:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS subscribers (
                    chat_id INTEGER PRIMARY KEY,
                    email TEXT,
                    authorization_code TEXT,
                    reference TEXT,
                    active INTEGER DEFAULT 0,
                    next_charge_at TEXT,
                    created_at TEXT
                )
            """)
            conn.commit()
        finally:
            conn.close()

def db_get(chat_id):
    with db_lock:
        conn = sqlite3.connect(DB_PATH)
        try:
            cur = conn.execute(f"SELECT {','.join(SUB_COLUMNS)} FROM subscribers WHERE chat_id=?", (chat_id,))
            row = cur.fetchone()
        finally:
            conn.close()
    return dict(zip(SUB_COLUMNS, row)) if row else None

def db_save(chat_id, **fields):
    current = db_get(chat_id) or {c: None for c in SUB_COLUMNS}
    current["chat_id"] = chat_id
    current.update(fields)
    if not current.get("created_at"):
        current["created_at"] = datetime.utcnow().isoformat()
    with db_lock:
        conn = sqlite3.connect(DB_PATH)
        try:
            placeholders = ",".join("?" for _ in SUB_COLUMNS)
            conn.execute(
                f"INSERT OR REPLACE INTO subscribers ({','.join(SUB_COLUMNS)}) VALUES ({placeholders})",
                [current[c] for c in SUB_COLUMNS]
            )
            conn.commit()
        finally:
            conn.close()

def db_all_active_due(now_iso):
    with db_lock:
        conn = sqlite3.connect(DB_PATH)
        try:
            cur = conn.execute(
                f"SELECT {','.join(SUB_COLUMNS)} FROM subscribers WHERE active=1 AND next_charge_at<=?",
                (now_iso,)
            )
            rows = cur.fetchall()
        finally:
            conn.close()
    return [dict(zip(SUB_COLUMNS, r)) for r in rows]

def is_active_subscriber(chat_id):
    row = db_get(chat_id)
    return bool(row and row.get("active"))


# ── Paystack helpers ─────────────────────────────────────────────────────

def paystack_headers():
    return {"Authorization": f"Bearer {PAYSTACK_SECRET_KEY}", "Content-Type": "application/json"}

def paystack_initialize(email, chat_id):
    reference = f"wildlife_{chat_id}_{int(time.time())}"
    payload = {
        "email": email,
        "amount": SUBSCRIPTION_AMOUNT_KOBO,
        "reference": reference,
        "channels": ["card"],
        "metadata": {"chat_id": chat_id},
    }
    r = requests.post(f"{PAYSTACK_BASE}/transaction/initialize", json=payload, headers=paystack_headers(), timeout=20)
    data = r.json()
    if not data.get("status"):
        raise RuntimeError(data.get("message", "Paystack initialize failed"))
    return reference, data["data"]["authorization_url"]

def paystack_verify(reference):
    r = requests.get(f"{PAYSTACK_BASE}/transaction/verify/{reference}", headers=paystack_headers(), timeout=20)
    data = r.json()
    if not data.get("status"):
        raise RuntimeError(data.get("message", "Paystack verify failed"))
    return data["data"]  # {status, authorization:{authorization_code, reusable}, customer:{email}}

def paystack_charge(authorization_code, email):
    payload = {"authorization_code": authorization_code, "email": email, "amount": SUBSCRIPTION_AMOUNT_KOBO}
    r = requests.post(f"{PAYSTACK_BASE}/transaction/charge_authorization", json=payload, headers=paystack_headers(), timeout=30)
    return r.json()


def billing_loop():
    """Background thread: every hour, charges any subscriber whose 14-day
    cycle is due, using their saved card authorization."""
    while bot_loop is None:  # wait for post_init to set the event loop
        time.sleep(1)
    while True:
        try:
            now_iso = datetime.utcnow().isoformat()
            for sub in db_all_active_due(now_iso):
                chat_id = sub["chat_id"]
                try:
                    result = paystack_charge(sub["authorization_code"], sub["email"])
                    if result.get("data", {}).get("status") == "success":
                        next_charge = (datetime.utcnow() + timedelta(days=SUBSCRIPTION_PERIOD_DAYS)).isoformat()
                        db_save(chat_id, active=1, next_charge_at=next_charge)
                        run(bot_instance.send_message(
                            chat_id=chat_id,
                            text=f"✅ *Renewed!* ₦3,000 charged. Next renewal: {next_charge[:10]}",
                            parse_mode="Markdown"))
                    else:
                        db_save(chat_id, active=0)
                        run(bot_instance.send_message(
                            chat_id=chat_id,
                            text="❌ Your renewal payment failed. Use /subscribe to reactivate."))
                except Exception as e:
                    db_save(chat_id, active=0)
                    try:
                        run(bot_instance.send_message(
                            chat_id=chat_id,
                            text=f"❌ Renewal failed: {e}. Use /subscribe to reactivate."))
                    except Exception:
                        pass
        except Exception:
            pass
        time.sleep(3600)  # check hourly


# ── Event loop fix ─────────────────────────────────────────────────────────

async def post_init(app):
    global bot_loop
    bot_loop = asyncio.get_event_loop()


# ── Thread-safe helpers ────────────────────────────────────────────────────

def run(coro):
    return asyncio.run_coroutine_threadsafe(coro, bot_loop)

def send_msg(bot, chat_id, text, parse_mode="Markdown"):
    return run(bot.send_message(chat_id=chat_id, text=text, parse_mode=parse_mode)).result(timeout=15)

def edit_msg(bot, chat_id, msg_id, text, parse_mode="Markdown"):
    try:
        run(bot.edit_message_text(chat_id=chat_id, message_id=msg_id, text=text, parse_mode=parse_mode)).result(timeout=10)
    except Exception:
        pass

def send_video_file(bot, chat_id, filepath, caption):
    """Send a single video file — used for whole-file sends (no split)."""
    with open(filepath, "rb") as f:
        run(bot.send_video(
            chat_id=chat_id,
            video=f,
            caption=caption,
            supports_streaming=True,
            read_timeout=180,
            write_timeout=180,
            connect_timeout=30,
        )).result(timeout=240)


# ── yt-dlp helpers ────────────────────────────────────────────────────────

def ytdlp_get_info(url):
    """Get video title and size using yt-dlp without downloading."""
    try:
        result = subprocess.run(
            ["yt-dlp", "--dump-json", "--no-playlist", url],
            capture_output=True, text=True, timeout=30
        )
        if result.returncode == 0:
            info = json.loads(result.stdout.split("\n")[0])
            title = info.get("title", "video")
            size = info.get("filesize") or info.get("filesize_approx") or 0
            size_mb = size / (1024 * 1024) if size else 0
            duration = info.get("duration", 0)
            return title, size_mb, duration
    except Exception:
        pass
    return "video", 0, 0

def ytdlp_download(url, output_path, chat_id, bot, msg_id):
    """Download using yt-dlp with live progress updates."""
    cmd = [
        "yt-dlp",
        "--no-playlist",
        "-f", "bv*[height<=720]+ba/b[height<=720]/best",  # cap resolution — keeps memory/disk use low
        "--merge-output-format", "mp4",
        "--newline",
        "-o", output_path,
        url
    ]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    active_processes[chat_id] = proc

    last_percent = -1
    for line in proc.stdout:
        if cancel_flags.get(chat_id):
            proc.kill()
            return False

        # Parse yt-dlp progress lines like: [download]  45.2% of 120.00MiB at 2.50MiB/s
        match = re.search(r"\[download\]\s+([\d.]+)%\s+of\s+([\d.]+)(\w+)\s+at\s+([\d.]+\w+/s)", line)
        if match:
            percent = float(match.group(1))
            total_size = match.group(2) + match.group(3)
            speed = match.group(4)
            if int(percent) >= last_percent + 5:
                last_percent = int(percent)
                bar = "█" * (int(percent) // 10) + "░" * (10 - int(percent) // 10)
                edit_msg(bot, chat_id, msg_id,
                    f"⬇️ *Downloading...*\n\n`[{bar}] {percent:.0f}%`\n"
                    f"Size: {total_size} • Speed: {speed}")

    proc.wait()
    active_processes.pop(chat_id, None)
    return proc.returncode == 0


# ── Split video into clips ─────────────────────────────────────────────────

def split_video(filepath):
    base = os.path.splitext(filepath)[0]
    pattern = f"{base}_clip_%03d.mp4"

    # Step 1: split with stream COPY — no re-encoding, near-zero memory/CPU.
    # Re-encoding a 20-30 min file in one ffmpeg pass is what was running the
    # bot out of memory (512MB plan). Copying just demuxes/remuxes instead.
    cmd = [
        "ffmpeg", "-i", filepath,
        "-c", "copy",
        "-segment_time", str(CLIP_DURATION),
        "-f", "segment", "-reset_timestamps", "1",
        pattern, "-y"
    ]
    subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    clips = sorted([
        os.path.join(os.path.dirname(filepath), f)
        for f in os.listdir(os.path.dirname(filepath))
        if os.path.basename(base) + "_clip_" in f and f.endswith(".mp4")
    ])

    # Step 2: only re-encode clips that are still too big for Telegram, one
    # short 2-minute clip at a time (low memory) instead of the whole video.
    final_clips = []
    for clip in clips:
        size_mb = os.path.getsize(clip) / (1024 * 1024)
        if size_mb <= TELEGRAM_MAX_MB:
            final_clips.append(clip)
            continue
        compressed = clip.replace(".mp4", "_c.mp4")
        compress_cmd = [
            "ffmpeg", "-i", clip,
            "-c:v", "libx264", "-crf", "30", "-preset", "veryfast", "-threads", "1",
            "-vf", "scale=-2:720",
            "-c:a", "aac", "-b:a", "96k",
            compressed, "-y"
        ]
        subprocess.run(compress_cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if os.path.exists(compressed) and os.path.getsize(compressed) > 0:
            try: os.remove(clip)
            except: pass
            final_clips.append(compressed)
        else:
            final_clips.append(clip)  # fallback to original if compression failed
    return final_clips


# ── Core process: download → split → send ─────────────────────────────────

def process_video(url, chat_id, bot, custom_name=None):
    filepath = None
    try:
        # ── Step 1: Get info ──
        msg_id = send_msg(bot, chat_id, "🔍 *Fetching video info...*").message_id
        title, size_mb, duration = ytdlp_get_info(url)
        safe_title = re.sub(r"[^a-zA-Z0-9_\-]", "_", title)[:60]
        filepath = os.path.join(DOWNLOAD_FOLDER, f"{safe_title}.mp4")
        duration_str = f"{int(duration)//60}m {int(duration)%60}s" if duration else "unknown"

        edit_msg(bot, chat_id, msg_id,
            f"📹 *{title}*\n"
            f"💾 Size: {size_mb:.1f} MB  •  ⏱ Duration: {duration_str}\n\n"
            f"⬇️ Starting download...")

        active_downloads[chat_id] = title

        # ── Step 2: Download ──
        success = ytdlp_download(url, filepath, chat_id, bot, msg_id)

        if cancel_flags.get(chat_id):
            cancel_flags.pop(chat_id, None)
            active_downloads.pop(chat_id, None)
            edit_msg(bot, chat_id, msg_id, "🛑 Download cancelled.")
            return

        if not success or not os.path.exists(filepath):
            edit_msg(bot, chat_id, msg_id, "❌ Download failed. The URL may not be supported or the video is private.")
            return

        # ── Step 3: Split ──
        edit_msg(bot, chat_id, msg_id, f"✂️ *Splitting into 2-minute clips...*")
        clips = split_video(filepath)

        if not clips:
            # No split needed or ffmpeg failed — send as single file if small enough
            size_mb = os.path.getsize(filepath) / (1024 * 1024)
            if size_mb <= TELEGRAM_MAX_MB:
                edit_msg(bot, chat_id, msg_id, f"📤 Sending video ({size_mb:.1f} MB)...")
                send_video_file(bot, chat_id, filepath, f"🎬 {title}")
                edit_msg(bot, chat_id, msg_id, "✅ *Done!* Video sent above ⬆️")
            else:
                edit_msg(bot, chat_id, msg_id,
                    f"⚠️ File is {size_mb:.1f} MB — too large for Telegram directly.\n"
                    f"Try a shorter video or use /dl with a specific clip URL.")
            return

        # ── Step 4: Send clips ──
        sent = 0
        edit_msg(bot, chat_id, msg_id, f"📤 Sending *{len(clips)} clips*... ⏳")
        for i, clip in enumerate(clips, 1):
            if cancel_flags.get(chat_id):
                break
            if not os.path.exists(clip):
                continue
            clip_mb = os.path.getsize(clip) / (1024 * 1024)
            success = False
            for attempt in range(5):  # up to 5 attempts per clip
                if cancel_flags.get(chat_id):
                    break
                try:
                    with open(clip, "rb") as f:
                        run(bot.send_video(
                            chat_id=chat_id,
                            video=f,
                            caption=f"🎬 *{title}*\nClip {i}/{len(clips)} • {clip_mb:.1f} MB",
                            supports_streaming=True,
                            read_timeout=180,
                            write_timeout=180,
                            connect_timeout=30,
                        )).result(timeout=240)
                    success = True
                    sent += 1
                    break
                except Exception as e:
                    err = str(e)
                    wait = (attempt + 1) * 10  # 10s, 20s, 30s, 40s, 50s
                    edit_msg(bot, chat_id, msg_id,
                        f"⏳ Clip {i}/{len(clips)} attempt {attempt+1} failed, retrying in {wait}s...\n_{err}_")
                    for _ in range(wait):
                        if cancel_flags.get(chat_id):
                            break
                        time.sleep(1)

            if cancel_flags.get(chat_id):
                break

            if not success:
                send_msg(bot, chat_id, f"❌ Clip {i} could not be sent after 5 attempts. Skipping.", parse_mode=None)

            try: os.remove(clip)
            except: pass

            edit_msg(bot, chat_id, msg_id, f"📤 Progress: {i}/{len(clips)} clips processed...")
            for _ in range(3):  # 3s pause between clips, interruptible by /cancel
                if cancel_flags.get(chat_id):
                    break
                time.sleep(1)

        if cancel_flags.get(chat_id):
            cancel_flags.pop(chat_id, None)
            edit_msg(bot, chat_id, msg_id, "🛑 Cancelled.")
        else:
            edit_msg(bot, chat_id, msg_id, f"✅ *Done!* Sent *{sent}/{len(clips)} clips* for:\n📹 {title}")

    except Exception as e:
        send_msg(bot, chat_id, f"❌ Error: {str(e)}", parse_mode=None)
    finally:
        active_downloads.pop(chat_id, None)
        if filepath:
            try: os.remove(filepath)
            except: pass


# ── archive.org collection resolver ────────────────────────────────────────

def resolve_archive_collection(url):
    try:
        from urllib.parse import unquote
        identifier = re.search(r"archive\.org/details/([^/?#]+)", url)
        if not identifier:
            return None
        item_id = identifier.group(1)
        data = requests.get(f"https://archive.org/metadata/{item_id}", timeout=20).json()
        mp4s = [(f"https://archive.org/download/{item_id}/{f['name']}", unquote(f["name"]))
                for f in data.get("files", []) if f["name"].endswith(".mp4")]
        return mp4s or None
    except:
        return None


# ── Command handlers ───────────────────────────────────────────────────────

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🎬 *Video Downloader Bot*\n\n"
        "Download from *1000+ sites* — Vimeo, archive.org, Dailymotion, Facebook, TikTok, Twitter & more!\n"
        "Videos are split into *2-minute clips* and sent directly to you here.\n\n"
        "📋 *Commands:*\n"
        "/dl <url> — Download any video URL\n"
        "/dl 3 — Download built-in wildlife video #3\n"
        "/list — See built-in wildlife videos\n"
        "/dlall <archive.org url> — Download whole collection\n"
        "/cancel — Cancel active download\n"
        "/status — Check progress\n"
        "/subscribe — Subscribe (₦3,000 / 14 days)\n"
        "/verify — Verify payment & activate\n\n"
        "*Examples:*\n"
        "`/dl https://vimeo.com/123456789`\n"
        "`/dl https://archive.org/details/ElephantsDream`",
        parse_mode="Markdown"
    )

async def list_videos(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = "📹 *Built-in Wildlife Videos:*\n\n"
    for num, v in VIDEOS.items():
        msg += f"*{num}.* {v['name']}\n"
    msg += "\nDownload: /dl <number>"
    await update.message.reply_text(msg, parse_mode="Markdown")

async def subscribe(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if not PAYSTACK_SECRET_KEY:
        await update.message.reply_text("⚠️ Billing isn't configured yet. Try again later.")
        return

    row = db_get(chat_id)
    if row and row.get("active"):
        await update.message.reply_text(
            f"✅ You're already subscribed.\nNext charge: {(row.get('next_charge_at') or '')[:10]}",
            parse_mode="Markdown")
        return

    if not context.args:
        await update.message.reply_text(
            "💳 *Subscribe — ₦3,000 every 14 days*\n\n"
            "Reply with your email like this:\n`/subscribe you@example.com`",
            parse_mode="Markdown")
        return

    email = context.args[0]
    if "@" not in email or "." not in email:
        await update.message.reply_text("❌ That doesn't look like a valid email. Try `/subscribe you@example.com`", parse_mode="Markdown")
        return

    try:
        reference, pay_url = paystack_initialize(email, chat_id)
    except Exception as e:
        await update.message.reply_text(f"❌ Couldn't start payment: {e}")
        return

    db_save(chat_id, email=email, reference=reference, active=0)
    await update.message.reply_text(
        f"💳 *Tap to pay ₦3,000:*\n{pay_url}\n\n"
        f"After paying, send /verify to activate your subscription.",
        parse_mode="Markdown")

async def verify(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    row = db_get(chat_id)
    if not row or not row.get("reference"):
        await update.message.reply_text("❌ No pending payment found. Use /subscribe first.")
        return

    try:
        data = paystack_verify(row["reference"])
    except Exception as e:
        await update.message.reply_text(f"❌ Couldn't verify payment: {e}")
        return

    if data.get("status") != "success":
        await update.message.reply_text(f"⏳ Payment not completed yet (status: {data.get('status')}). Pay first, then /verify again.")
        return

    auth = data.get("authorization", {})
    if not auth.get("reusable"):
        await update.message.reply_text("⚠️ This payment method can't be auto-renewed. Please pay with a card so renewals work, then /subscribe again.")
        return

    next_charge = (datetime.utcnow() + timedelta(days=SUBSCRIPTION_PERIOD_DAYS)).isoformat()
    db_save(chat_id,
            email=data.get("customer", {}).get("email", row.get("email")),
            authorization_code=auth.get("authorization_code"),
            active=1,
            next_charge_at=next_charge)
    await update.message.reply_text(
        f"✅ *Subscribed!* You're all set.\nNext charge: {next_charge[:10]}\n\nUse /dl to start downloading.",
        parse_mode="Markdown")

async def download(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    bot = context.bot

    if not is_active_subscriber(chat_id):
        await update.message.reply_text(
            "🔒 This bot requires an active subscription (₦3,000 / 14 days).\nUse /subscribe to get started.")
        return

    if not context.args:
        await update.message.reply_text("Usage: /dl <url>  or  /dl <number>\nExamples:\n`/dl https://vimeo.com/123456`\n`/dl 1`", parse_mode="Markdown")
        return

    if chat_id in active_downloads:
        await update.message.reply_text(f"⏳ Already working on: *{active_downloads[chat_id]}*\nUse /cancel first.", parse_mode="Markdown")
        return

    arg = context.args[0]

    # Built-in video number
    if arg.isdigit():
        video_id = int(arg)
        if video_id not in VIDEOS:
            await update.message.reply_text(f"No video #{video_id}. Use /list.")
            return
        v = VIDEOS[video_id]
        active_downloads[chat_id] = v["name"]  # claim immediately — closes the race with /cancel & duplicate updates
        cancel_flags.pop(chat_id, None)
        threading.Thread(target=process_video, args=(v["url"], chat_id, bot), daemon=True).start()
        return

    # archive.org collection (details page with multiple videos)
    if "archive.org/details/" in arg:
        await update.message.reply_text("🔍 Checking archive.org collection...")
        results = resolve_archive_collection(arg)
        if results and len(results) > 1:
            msg = f"📋 Found *{len(results)} videos* in collection:\n\n"
            for i, (url, name) in enumerate(results[:15], 1):
                msg += f"*{i}.* {name}\n"
            if len(results) > 15:
                msg += f"\n...and {len(results)-15} more."
            msg += f"\n\nDownload one: `/dl {results[0][0]}`\nDownload all: `/dlall {arg}`"
            await update.message.reply_text(msg, parse_mode="Markdown")
            return

    # Any URL — let yt-dlp handle it
    if arg.startswith("http"):
        active_downloads[chat_id] = arg[:60]  # claim immediately — closes the race with /cancel & duplicate updates
        cancel_flags.pop(chat_id, None)
        threading.Thread(target=process_video, args=(arg, chat_id, bot), daemon=True).start()
        return

    await update.message.reply_text("❌ Send a valid URL or number. Use /list to see built-in videos.")

async def download_all(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    bot = context.bot

    if not is_active_subscriber(chat_id):
        await update.message.reply_text(
            "🔒 This bot requires an active subscription (₦3,000 / 14 days).\nUse /subscribe to get started.")
        return

    if not context.args:
        await update.message.reply_text("Usage: /dlall <archive.org url>")
        return
    if chat_id in active_downloads:
        await update.message.reply_text(f"⏳ Already working on: *{active_downloads[chat_id]}*\nUse /cancel first.", parse_mode="Markdown")
        return

    url = context.args[0]
    await update.message.reply_text("🔍 Finding all videos in collection...")
    results = resolve_archive_collection(url)
    if not results:
        await update.message.reply_text("❌ No MP4 files found. Make sure it's an archive.org/details/ link.")
        return

    await update.message.reply_text(f"📥 Found *{len(results)} videos*. Processing one by one... ⏳", parse_mode="Markdown")

    active_downloads[chat_id] = f"batch of {len(results)} videos"  # claim immediately
    cancel_flags.pop(chat_id, None)

    def batch():
        for i, (file_url, filename) in enumerate(results, 1):
            if cancel_flags.get(chat_id):
                cancel_flags.pop(chat_id, None)
                active_downloads.pop(chat_id, None)
                run(bot.send_message(chat_id=chat_id, text="🛑 Batch cancelled."))
                return
            active_downloads[chat_id] = f"batch {i}/{len(results)}: {filename}"  # re-claim each round
            cancel_flags.pop(chat_id, None)
            run(bot.send_message(chat_id=chat_id, text=f"📹 *Video {i}/{len(results)}:* {filename}", parse_mode="Markdown"))
            process_video(file_url, chat_id, bot)
        active_downloads.pop(chat_id, None)
        run(bot.send_message(chat_id=chat_id, text=f"🎉 *All done!* Finished all {len(results)} videos.", parse_mode="Markdown"))

    threading.Thread(target=batch, daemon=True).start()

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    was_busy = chat_id in active_downloads or chat_id in active_processes

    # Always set the flag and clear state, even if the dicts look empty right
    # now — a send/retry loop running in another thread may flip them a
    # moment later, and we don't want a timing gap to make /cancel a no-op.
    cancel_flags[chat_id] = True
    proc = active_processes.get(chat_id)
    if proc:
        try:
            proc.kill()
        except Exception:
            pass
        active_processes.pop(chat_id, None)
    active_downloads.pop(chat_id, None)

    if was_busy:
        await update.message.reply_text("🛑 *Cancelled!*", parse_mode="Markdown")
    else:
        await update.message.reply_text("✅ Nothing was tracked as active, but I've sent a stop signal just in case anything is still running in the background.")

async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if chat_id in active_downloads:
        await update.message.reply_text(f"⏳ Working on: *{active_downloads[chat_id]}*", parse_mode="Markdown")
    else:
        await update.message.reply_text("✅ No active downloads.")


# ── Run ────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    # Check yt-dlp is installed
    try:
        subprocess.run(["yt-dlp", "--version"], capture_output=True, check=True)
    except FileNotFoundError:
        print("❌ yt-dlp not found! Run: pip install yt-dlp")
        exit(1)

    print("🎬 Video Downloader Bot starting...")
    print("Supports: Vimeo, archive.org, Dailymotion, Facebook, TikTok, Twitter & 1000+ more")

    db_init()
    if not PAYSTACK_SECRET_KEY:
        print("⚠️  PAYSTACK_SECRET_KEY not set — /subscribe will be disabled until it's configured.")

    app = (
        ApplicationBuilder()
        .token(BOT_TOKEN)
        .post_init(post_init)
        .build()
    )
    bot_instance = app.bot
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("list", list_videos))
    app.add_handler(CommandHandler("dl", download))
    app.add_handler(CommandHandler("dlall", download_all))
    app.add_handler(CommandHandler("cancel", cancel))
    app.add_handler(CommandHandler("status", status))
    app.add_handler(CommandHandler("subscribe", subscribe))
    app.add_handler(CommandHandler("verify", verify))

    threading.Thread(target=billing_loop, daemon=True).start()

    app.run_polling()
