import os
import html as html_module
import time
import asyncio
import re
import logging
import shutil
import aiohttp
from aiohttp import web
import sys
import subprocess
import socket
import threading
from pyrogram import Client, filters, enums, idle
from pyrogram.errors import FloodWait
from pyrogram.types import BotCommand, InlineKeyboardMarkup, InlineKeyboardButton
from datetime import datetime, timedelta

from aiogram import Bot as AioBot
from aiogram.types import FSInputFile

from database import db
from config import Config
import dashboard
from dashboard import start_flask
from helpers import (
    parse_range,
    is_allowed
)

from pfm_downloader import PFMDownloader

def get_user_info_level(uid, show_id=None):
    """Returns 'full' if user has extra_episode enabled, else 'max'"""
    # Owner always gets full access
    if uid in Config.OWNER_IDS:
        return 'full'
        
    if show_id:
        db.cursor.execute('SELECT 1 FROM subscriptions WHERE user_id = ? AND sub_type = "extra_ep_remove_story" AND sub_data = ?', (uid, show_id))
        if db.cursor.fetchone():
            return 'max'
            
    db.cursor.execute('SELECT 1 FROM subscriptions WHERE user_id = ? AND sub_type = "extra_episode"', (uid,))
    if db.cursor.fetchone():
        return 'full'
        
    if show_id:
        db.cursor.execute('SELECT 1 FROM subscriptions WHERE user_id = ? AND sub_type = "extra_ep_story" AND sub_data = ?', (uid, show_id))
        if db.cursor.fetchone():
            return 'full'
            
    return 'max'

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s"
)

logger = logging.getLogger("BOT")

THUMB_DIR = "thumbs"
ARTIST_DIR = "artists"
os.makedirs(THUMB_DIR, exist_ok=True)
os.makedirs(ARTIST_DIR, exist_ok=True)

# --- Cloudflare Tunnel & Dummy Web Server ---
tunnel_url = None
tunnel_process = None

def get_free_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(('', 0))
        return s.getsockname()[1]

dashboard_port = get_free_port()

def stop_tunnel():
    global tunnel_process, tunnel_url
    if tunnel_process:
        try:
            tunnel_process.terminate()
            tunnel_process.kill()
        except Exception: pass
        tunnel_process = None
    tunnel_url = None

def restart_tunnel():
    stop_tunnel()
    cf_path = os.path.join(os.getcwd(), "cloudflared.exe" if os.name == "nt" else "cloudflared")
    if not os.path.exists(cf_path):
        return

    def read_stream(stream):
        global tunnel_url
        for line in iter(stream.readline, b''):
            decoded = line.decode('utf-8', errors='ignore').strip()
            if ".trycloudflare.com" in decoded and not tunnel_url:
                match = re.search(r"https://[a-zA-Z0-9\-]+\.trycloudflare\.com", decoded)
                if match:
                    tunnel_url = match.group(0)
                    logger.info(f"Cloudflare Tunnel available at: {tunnel_url}")

    try:
        global tunnel_process
        tunnel_process = subprocess.Popen(
            [cf_path, "tunnel", "--url", f"http://127.0.0.1:{dashboard_port}"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        threading.Thread(target=read_stream, args=(tunnel_process.stdout,), daemon=True).start()
        threading.Thread(target=read_stream, args=(tunnel_process.stderr,), daemon=True).start()
    except Exception as e:
        logger.error(f"Failed to start tunnel: {e}")

async def ensure_cloudflared():
    cf_path = os.path.join(os.getcwd(), "cloudflared.exe" if os.name == "nt" else "cloudflared")
    if not os.path.exists(cf_path):
        logger.info("cloudflared not found...\nDownloading...")
        is_windows = os.name == "nt"
        url = "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-windows-amd64.exe" if is_windows else "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64"
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url) as resp:
                    if resp.status == 200:
                        with open(cf_path, "wb") as f:
                            f.write(await resp.read())
                        if not is_windows:
                            os.chmod(cf_path, 0o755)
        except Exception as e:
            logger.error(f"Error downloading cloudflared: {e}")

app = Client(
    "pfm_bot",
    bot_token=Config.BOT_TOKEN,
    api_id=Config.API_ID,
    api_hash=Config.API_HASH,
    workers=20,
    sleep_threshold=60
)


aio_bot = AioBot(token=Config.BOT_TOKEN)

downloader = PFMDownloader()

user_show = {}
user_awaiting_range = {}
user_is_all_download = {}
active_downloads = {}
user_queues = {}
cancel_flags = {}
gen_state = {}
user_processes = {}  # Per-user process tracking for cancel
expired_notified = set()


def is_download_active(uid):
    """Check if a download is active for this user"""
    return uid in active_downloads


async def send_log(log_ch_key, text, photo=None):
    log_ch = db.get_setting(log_ch_key)
    if not log_ch:
        return
    
    try:
        chat_id = int(log_ch)
    except (ValueError, TypeError):
        logger.error(f"Invalid log channel ID for {log_ch_key}: {log_ch}")
        return

    # Try sending with main app first
    for bot in [app]:
        if not bot.is_connected:
            continue
        
        # Peer Resolution: Try to get chat info first to cache the entity
        try:
            await bot.get_chat(chat_id)
        except Exception as e:
            logger.warning(f"Could not resolve chat {chat_id} with {bot.name}: {e}")
        
        # 1. Try Photo if provided
        if photo:
            try:
                await bot.send_photo(chat_id, photo=photo, caption=text)
                logger.info(f"Successfully sent photo log to {chat_id} using {bot.name}")
                return True
            except Exception as e:
                logger.warning(f"Failed to send photo log to {chat_id} using {bot.name}: {e}")
        
        # 2. Try Text (as primary or fallback)
        try:
            await bot.send_message(chat_id, text)
            logger.info(f"Successfully sent text log to {chat_id} using {bot.name}")
            return True
        except Exception as e:
            logger.warning(f"Failed to send text log to {chat_id} using {bot.name}: {e}")

    # FINAL FALLBACK: Direct Bot API (bypasses library peer resolution)
    logger.info(f"Attempting direct Bot API fallback for {chat_id}")
    try:
        url = f"https://api.telegram.org/bot{Config.BOT_TOKEN}/"
        async with aiohttp.ClientSession() as session:
            if photo:
                payload = aiohttp.FormData()
                payload.add_field("chat_id", str(chat_id))
                payload.add_field("photo", photo)
                payload.add_field("caption", text)
                payload.add_field("parse_mode", "Markdown")
                async with session.post(url + "sendPhoto", data=payload) as resp:
                    res = await resp.json()
                    if res.get("ok"): 
                        logger.info("Direct API Photo Log Success")
                        return True
            
            # Text fallback if photo failed or wasn't provided
            payload = {"chat_id": str(chat_id), "text": text, "parse_mode": "Markdown"}
            async with session.post(url + "sendMessage", json=payload) as resp:
                res = await resp.json()
                if res.get("ok"):
                    logger.info("Direct API Text Log Success")
                    return True
                else:
                    logger.error(f"Direct API Log FAIL: {res}")
                    err_msg = f"Log Delivery Critical Failure\n\nChannel: `{chat_id}`\nDirect API Error: `{res.get('description')}`"
                    for owner_id in Config.OWNER_IDS:
                        try: await app.send_message(owner_id, err_msg)
                        except: pass
    except Exception as e:
        logger.error(f"Direct API Exception: {e}")
    
    return False



async def fast_upload(chat_id, filepath, title, artist, duration, thumb_path=None):
    """Upload using direct Bot API for faster speeds"""
    url = f"https://api.telegram.org/bot{Config.BOT_TOKEN}/sendAudio"
    
    data = aiohttp.FormData()
    data.add_field('chat_id', str(chat_id))
    data.add_field('title', title)
    data.add_field('performer', artist)
    data.add_field('duration', str(duration))
    data.add_field('caption', title)
    
    audio_file = open(filepath, 'rb')
    data.add_field('audio', audio_file, filename=os.path.basename(filepath))
    
    thumb_file = None
    if thumb_path and os.path.exists(thumb_path):
        thumb_file = open(thumb_path, 'rb')
        data.add_field('thumb', thumb_file)

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(url, data=data, timeout=aiohttp.ClientTimeout(total=300)) as resp:
                result = await resp.json()
                if not result.get("ok"):
                    if result.get("error_code") == 429:
                        retry_after = result.get("parameters", {}).get("retry_after", 30)
                        raise FloodWait(retry_after)
                    logger.error(f"Bot API upload failed: {result}")
                    return False
                return True
    except Exception as e:
        logger.error(f"Fast upload error: {e}")
        if isinstance(e, FloodWait):
            raise e
        return False
    finally:
        audio_file.close()
        if thumb_file:
            thumb_file.close()

async def auto_delete_task():
    """Periodic cleanup task that runs every hour"""
    while True:
        try:
            logger.info("Running periodic cleanup...")
            download_dir = Config.DOWNLOAD_DIR
            if os.path.exists(download_dir):
                for item in os.listdir(download_dir):
                    item_path = os.path.join(download_dir, item)
                    if item.startswith('.'): continue
                    
                    if os.path.isdir(item_path):
                        mtime = os.path.getmtime(item_path)
                        # Delete if older than 1 hour or if it's an empty directory
                        if (time.time() - mtime) > 3600 or not os.listdir(item_path):
                            shutil.rmtree(item_path, ignore_errors=True)
                            logger.info(f"Auto-deleted old/empty directory: {item}")
                    elif os.path.isfile(item_path):
                        if (time.time() - os.path.getmtime(item_path)) > 3600:
                            os.remove(item_path)
                            logger.info(f"Auto-deleted old file: {item}")
        except Exception as e:
            logger.error(f"Auto delete error: {e}")
        
        await asyncio.sleep(3600) # Run every 1 hour

async def check_expired_task():
    """Background task to check for newly expired users and notify admin group"""
    while True:
        try:
            from datetime import datetime, timezone, timedelta
            ist = timezone(timedelta(hours=5, minutes=30))
            now = datetime.now(ist)
            
            # Fetch all users who have subscriptions
            db.cursor.execute('SELECT DISTINCT user_id FROM subscriptions')
            all_users = db.cursor.fetchall()
            
            for (uid,) in all_users:
                # Check if already notified
                db.cursor.execute('SELECT value FROM settings WHERE key = ?', (f"notified_expired_{uid}",))
                notified = db.cursor.fetchone()
                if notified and notified[0] == "1":
                    continue
                    
                db.cursor.execute('SELECT expiry FROM subscriptions WHERE user_id = ?', (uid,))
                subs = db.cursor.fetchall()
                
                if not subs:
                    continue
                    
                is_expired = True
                for (expiry,) in subs:
                    if not expiry:
                        is_expired = False
                        break
                    try:
                        exp_dt = datetime.fromisoformat(expiry)
                        if now <= exp_dt:
                            is_expired = False
                            break
                    except:
                        pass
                        
                if is_expired:
                    db.set_setting(f"notified_expired_{uid}", "1")
                    user = db.get_user(uid)
                    username = user[0] if user else ""
                    name = user[1] if user else "Unknown"
                    u_name_text = f"\n\n@{username}" if username else ""
                    exp_msg = f"Validity Expired...\n\n{name}\n\n`{uid}`{u_name_text}"
                    try:
                        await app.send_message(Config.ADMIN_GROUP, exp_msg)
                    except Exception as e:
                        logger.error(f"Error auto-sending expired msg to admin group: {e}")
        except Exception as e:
            logger.error(f"Check expired task error: {e}")
            
        await asyncio.sleep(60) # Check every 1 minute

async def set_cmds(client):
    try:
        await client.set_bot_commands([])
    except Exception as e:
        logger.error(f"Cmd error: {e}")

def get_show_markup(user_id, show_id):
    if db.check_user_show(user_id, show_id):
        save_btn = InlineKeyboardButton("Remove from Saved", callback_data=f"unsave_{show_id}")
    else:
        save_btn = InlineKeyboardButton("Save Story", callback_data=f"save_{show_id}")
        
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("All Episodes", callback_data=f"all_{show_id}")],
        [InlineKeyboardButton("Select Episode", callback_data=f"multiple_{show_id}")],
        [save_btn]
    ])

group_avatars_cache = {}
last_avatar_check = {}

async def download_group_avatar_bg(chat_id, client):
    import time
    now = time.time()
    if now - last_avatar_check.get(chat_id, 0) < 60:
        return
    last_avatar_check[chat_id] = now

    try:
        chat = await client.get_chat(chat_id)
        if not chat.photo: return
        
        file_id = chat.photo.big_file_id
        if group_avatars_cache.get(chat_id) == file_id:
            return
            
        avatar_path = os.path.join("avatars", f"{chat_id}.jpg")
        await client.download_media(file_id, file_name=avatar_path)
        group_avatars_cache[chat_id] = file_id
    except Exception as e:
        logger.error(f"Failed to download group avatar for {chat_id}: {e}")

# Custom filter to restrict bot access to authorized/subscribed users only
async def check_auth_filter(_, client, update):
    allowed, _ = is_allowed(update)
    if allowed:
        chat_id = getattr(update.chat, "id", 0) if hasattr(update, "chat") and update.chat else 0
        if chat_id < 0:
            asyncio.create_task(download_group_avatar_bg(chat_id, client))
    return allowed

auth_filter = filters.create(check_auth_filter)



@app.on_message(filters.command("update") & ~filters.bot)
async def update_cmd(client, message):
    if message.chat.id != Config.ADMIN_GROUP:
        return
    uid = message.from_user.id
    if uid not in Config.OWNER_IDS:
        return await message.reply("Unauthorized Access...")
    
    m = await message.reply("Pulling updates from GitHub...")
    try:
        # Save local changes to prevent merge conflicts
        subprocess.run(["git", "stash"], capture_output=True)
        # Run git pull
        result = subprocess.run(["git", "pull"], capture_output=True, text=True, check=True)
        
        out = result.stdout.strip() if result.stdout else ""
        
        if "Already up to date" in out:
            await m.edit("Git Pull Successful...\n\nAlready up to date...")
            return
            
        out_trunc = out[-3000:] if len(out) > 3000 else out
        await m.edit(f"Git Pull Successful...\n\n{out_trunc}\n\nRestarting bot...")
        
        # Save restart state
        with open("restart.txt", "w") as f:
            f.write(f"{message.chat.id}|{m.id}|update")
        
        # Force-clear ALL user tasks before restart (os.execv kills finally blocks)
        logger.info("Pre-restart cleanup: clearing all user states...")
        for proc_uid, procs in user_processes.items():
            for proc in procs:
                try: proc.kill()
                except: pass
        active_downloads.clear()
        user_queues.clear()
        cancel_flags.clear()
        user_processes.clear()
        user_show.clear()
        user_awaiting_range.clear()
        user_is_all_download.clear()
        gen_state.clear()
            
        # Restart the process
        stop_tunnel()
        os.execv(sys.executable, [sys.executable] + sys.argv)
    except subprocess.CalledProcessError as e:
        await m.edit(f"Git Pull Failed!\n\nError: `{e.stderr}`")
    except Exception as e:
        await m.edit(f"Update Failed!\n\nError: `{str(e)}`")

@app.on_message(filters.command("dashboard") & ~filters.bot)
async def dashboard_cmd(client, message):
    if message.chat.id != Config.ADMIN_GROUP:
        return
    uid = message.from_user.id
    
    is_admin = False
    if uid in Config.OWNER_IDS:
        is_admin = True
    else:
        try:
            member = await client.get_chat_member(message.chat.id, uid)
            if member.status in [enums.ChatMemberStatus.ADMINISTRATOR, enums.ChatMemberStatus.OWNER]:
                is_admin = True
        except:
            pass
            
    if not is_admin:
        return await message.reply("Unauthorized Access...")
        
    m = await message.reply("Generating new dashboard URL...")
    
    # Restart the tunnel to force a new URL
    restart_tunnel()
    
    # Wait for new URL
    global tunnel_url
    for _ in range(30): # wait up to 15 seconds
        if tunnel_url:
            break
        await asyncio.sleep(0.5)
        
    if tunnel_url:
        pwd = dashboard.update_password()
        await m.edit(f"Dashboard URL...\n\n`{pwd}`\n\n{tunnel_url}")
    else:
        await m.edit("Failed to generate a new URL. Try again later...")

@app.on_message(filters.command("backup") & ~filters.bot)
async def backup_cmd(client, message):
    if message.chat.id != Config.ADMIN_GROUP:
        return
    uid = message.from_user.id
    
    is_admin = False
    if uid in Config.OWNER_IDS:
        is_admin = True
    else:
        try:
            member = await client.get_chat_member(message.chat.id, uid)
            if member.status in [enums.ChatMemberStatus.ADMINISTRATOR, enums.ChatMemberStatus.OWNER]:
                is_admin = True
        except:
            pass
            
    if not is_admin:
        return await message.reply("Unauthorized Access...")

    m = await message.reply("Creating backup files...")
    
    import zipfile
    import glob
    
    unix_time = int(time.time())
    zip_name = f"{unix_time}.zip"
    
    try:
        with zipfile.ZipFile(zip_name, 'w', zipfile.ZIP_DEFLATED) as zipf:
            for file in glob.glob("pocketfm.db*"):
                zipf.write(file)
                
        await client.send_document(
            chat_id=message.chat.id,
            document=zip_name,
            reply_to_message_id=message.id
        )
    except Exception as e:
        await message.reply(f"Failed to create backup: {e}")
    finally:
        await m.delete()
        if os.path.exists(zip_name):
            os.remove(zip_name)

@app.on_message(filters.command("restart") & ~filters.bot)
async def restart_cmd(client, message):
    if message.chat.id != Config.ADMIN_GROUP:
        return
    uid = message.from_user.id
    
    is_admin = False
    if uid in Config.OWNER_IDS:
        is_admin = True
    else:
        try:
            member = await client.get_chat_member(message.chat.id, uid)
            if member.status in [enums.ChatMemberStatus.ADMINISTRATOR, enums.ChatMemberStatus.OWNER]:
                is_admin = True
        except:
            pass
            
    if not is_admin:
        return await message.reply("Unauthorized Access...")
        
    m = await message.reply("Restarting the bot...")
    
    with open("restart.txt", "w") as f:
        f.write(f"{message.chat.id}|{m.id}|restart")
        
    logger.info("Pre-restart cleanup: clearing all user states...")
    for proc_uid, procs in user_processes.items():
        for proc in procs:
            try: proc.kill()
            except: pass
    active_downloads.clear()
    user_queues.clear()
    cancel_flags.clear()
    user_processes.clear()
    user_show.clear()
    user_awaiting_range.clear()
    user_is_all_download.clear()
    gen_state.clear()
        
    stop_tunnel()
    os.execv(sys.executable, [sys.executable] + sys.argv)

@app.on_message(filters.command("restore") & ~filters.bot)
async def restore_cmd(client, message):
    if message.chat.id != Config.ADMIN_GROUP:
        return
    uid = message.from_user.id
    if uid not in Config.OWNER_IDS:
        return await message.reply("Unauthorized Access...")

    if not message.reply_to_message or not message.reply_to_message.document or not message.reply_to_message.document.file_name.endswith('.zip'):
        return await message.reply("Please reply with the ZIP backup file for restoration...")
        
    m = await message.reply("Restoring data...")
    
    try:
        zip_path = await message.reply_to_message.download()
        
        db.conn.close()
        
        import zipfile
        with zipfile.ZipFile(zip_path, 'r') as zipf:
            zipf.extractall(".")
            
        os.remove(zip_path)
        
        await m.edit("Data restored successfully...")
        await asyncio.sleep(1)
        
        # Force-clear ALL user tasks before restart (os.execv kills finally blocks)
        logger.info("Pre-restart cleanup: clearing all user states...")
        for proc_uid, procs in user_processes.items():
            for proc in procs:
                try: proc.kill()
                except: pass
        active_downloads.clear()
        user_queues.clear()
        cancel_flags.clear()
        user_processes.clear()
        user_show.clear()
        user_awaiting_range.clear()
        user_is_all_download.clear()
        gen_state.clear()
        
        stop_tunnel()
        os.execv(sys.executable, [sys.executable] + sys.argv)
    except Exception as e:
        await m.edit(f"Restore failed: {e}")


async def download_avatar_bg(uid, client):
    avatar_path = os.path.join("avatars", f"{uid}.jpg")
    os.makedirs("avatars", exist_ok=True)
    try:
        async for photo in client.get_chat_photos(uid, limit=1):
            await client.download_media(photo.file_id, file_name=avatar_path)
            break
    except Exception:
        pass

@app.on_message(filters.command("start") & ~filters.bot)
async def start(client, message):
    if message.chat.id == Config.ADMIN_GROUP:
        return await message.reply(
            "Rio Rio Downloader\n\n"
            "For Admins...\n"
            "/dashboard Dashboard URL\n"
            "/backup Backup database\n"
            "/restart Restart bot\n\n"
            "For Owner Only...\n"
            "/update Pull and restart\n"
            "/restore Restore database"
        )
    await set_cmds(client)
    if message.from_user:
        uid = message.from_user.id
        name = message.from_user.first_name
        username = message.from_user.username
        if not username and hasattr(message.from_user, 'active_usernames') and message.from_user.active_usernames:
            username = message.from_user.active_usernames[0]
        elif not username and hasattr(message.from_user, 'usernames') and message.from_user.usernames:
            username = message.from_user.usernames[0].username
    else:
        uid = message.sender_chat.id
        name = message.sender_chat.title
        username = message.sender_chat.username
        
    if not username:
        try:
            full_user = await client.get_users(uid)
            username = full_user.username
            if not username and hasattr(full_user, 'active_usernames') and full_user.active_usernames:
                username = full_user.active_usernames[0]
            elif not username and hasattr(full_user, 'usernames') and full_user.usernames:
                username = full_user.usernames[0].username
        except Exception:
            pass
            
    is_new = db.add_user(uid, username, name)
    asyncio.create_task(download_avatar_bg(uid, client))
    
    if is_new:
        try:
            u_name_text = f"\n\n@{username}" if username else ""
            new_user_msg = f"New User...\n\n{name}\n\n`{uid}`{u_name_text}"
            await client.send_message(Config.ADMIN_GROUP, new_user_msg)
        except Exception as e:
            logger.error(f"Error sending new user msg to admin group: {e}")
            
        total, today, week = db.get_user_stats()
        log_text = (
            "NEW USER JOINED!\n\n"
            "User Details:\n"
            f"├ ID: `{uid}`\n"
            f"├ Name: {name}\n"
            f"├ Username: {('@' + username) if username else 'None'}\n"
            f"└ Profile: [Click to contact](tg://user?id={uid})\n\n"
            "Statistics:\n"
            f"├ Total Users: {total}\n"
            f"├ New Today: {today}\n"
            f"└ New This Week: {week}\n\n"
            f"Joined: {time.strftime('%Y-%m-%d %I:%M:%S %p IST')}"
        )
        await send_log("log_usr_channel", log_text)

    allowed, msg = is_allowed(message)
    is_group = message.chat.type in [enums.ChatType.GROUP, enums.ChatType.SUPERGROUP]
    
    if not allowed:
        if not is_group:
            if msg == "Subscription Expired Renew it":
                await message.reply(f"Hey {name}\n\nYour validity has expired\n\nContact Admin\n@Index_Guide")
            else:
                await message.reply(f"Hey {name}\n\nContact Admin\n@Index_Guide")
        return

    if is_group:
        asyncio.create_task(download_group_avatar_bg(message.chat.id, client))

    logger.info(f"Start by {uid}")

    # Owner gets no validity message, just the welcome
    if uid in Config.OWNER_IDS:
        validity_text = ""
    else:
        validity = db.get_user_validity(uid)
        if validity == "No Active Subscription":
            validity_text = "You don't have an active subscription.\n\n"
        elif validity == "Lifetime":
            validity_text = "You have lifetime validity\n\n"
        else:
            try:
                from datetime import datetime
                dt = datetime.fromisoformat(validity)
                formatted = dt.strftime("%I:%M %p %d/%m/%Y")
                if formatted.startswith("0"):
                    formatted = formatted[1:]
                validity_text = f"Your validity until\n{formatted}\n\n"
            except:
                validity_text = f"Your validity until\n{validity}\n\n"

    db.cursor.execute('SELECT value FROM settings WHERE key = ?', (f"set_cover_{uid}",))
    c_row = db.cursor.fetchone()
    has_cover = True if uid in Config.OWNER_IDS else ((c_row[0] == "1") if c_row else False)
    
    db.cursor.execute('SELECT value FROM settings WHERE key = ?', (f"set_artist_{uid}",))
    a_row = db.cursor.fetchone()
    has_artist = True if uid in Config.OWNER_IDS else ((a_row[0] == "1") if a_row else False)

    cmd_text = "/saved - Get Saved stories\n"
    if has_cover:
        cmd_text += "/set_cover - Set episode cover\n/d_cover - Delete episode cover\n"
    if has_artist:
        cmd_text += "/set_artist - Set episode artist\n/d_artist - Delete episode artist\n"
    cmd_text += "/cancel - Stop active download\n/stop - Stop active download"

    await message.reply(
        f"Hey {name}\n\n"
        f"{validity_text}"
        "Send story link to download\n\n"
        "Use below command to access bot\n"
        f"{cmd_text}"
    )

@app.on_message(filters.command("debug") & auth_filter & ~filters.bot)
async def debug_cmd(client, message):
    if message.chat.id != Config.ADMIN_GROUP:
        return
    text = message.text.split(" ")
    if len(text) > 1:
        if text[1].lower() == "on":
            db.set_setting("debug_mode", "on")
            await message.reply("Debug mode enabled. JSON response files will be sent here...")
        elif text[1].lower() == "off":
            db.set_setting("debug_mode", "off")
            await message.reply("Debug mode disabled. Response file sending is off...")
        else:
            await message.reply("Usage: /debug [on|off]")
    else:
        current = db.get_setting("debug_mode", "off")
        await message.reply(f"Debug mode is currently {current}...")

@app.on_message(filters.command("get_auth") & auth_filter & ~filters.bot)
async def get_auth_cmd(client, message):
    if message.chat.id != Config.ADMIN_GROUP:
        return
    uid = message.from_user.id
    if uid not in Config.OWNER_IDS:
        return await message.reply("Unauthorized Access...")
    
    token = downloader.token.get('auth-token', 'Not Found')
    
    # Generate Netscape format cookies and find earliest expiry
    netscape_cookies = "# Netscape HTTP Cookie File\n# http://curl.haxx.se/rfc/cookie_spec.html\n# This is a generated file!  Do not edit.\n\n"
    earliest_expiry = None
    
    if hasattr(downloader, 'cookies_jar'):
        for cookie in downloader.cookies_jar:
            # domain  flag  path  secure  expiration  name  value
            domain = cookie.domain
            flag = "TRUE" if domain.startswith(".") else "FALSE"
            path = cookie.path
            secure = "TRUE" if cookie.secure else "FALSE"
            expiry = cookie.expires if cookie.expires else 0
            
            if expiry > 0:
                if earliest_expiry is None or expiry < earliest_expiry:
                    earliest_expiry = expiry
            
            netscape_cookies += f"{domain}\t{flag}\t{path}\t{secure}\t{expiry}\t{cookie.name}\t{cookie.value}\n"
    
    expiry_text = "Never"
    if earliest_expiry:
        expiry_text = datetime.fromtimestamp(earliest_expiry).strftime('%Y-%m-%d %I:%M:%S %p')

    with open("cookies.txt", "w") as f:
        f.write(netscape_cookies)
    
    await message.reply_document(
        "cookies.txt", 
        caption=(
            f"PocketFM Auth Info\n\n"
            f"Auth Token:\n`{token}`\n\n"
            f"Earliest Cookie Expiry:\n`{expiry_text}`\n\n"
            f"_Cookies sent in Netscape format_"
        )
    )
    if os.path.exists("cookies.txt"):
        os.remove("cookies.txt")

@app.on_message(filters.command("set_cover") & auth_filter & ~filters.bot)
async def set_cover(client, message):
    uid = message.from_user.id
    if uid not in Config.OWNER_IDS:
        db.cursor.execute('SELECT value FROM settings WHERE key = ?', (f"set_cover_{uid}",))
        c_row = db.cursor.fetchone()
        if not c_row or c_row[0] != "1": return

    if not message.reply_to_message or not message.reply_to_message.photo:
        return await message.reply("Reply to an image...")
    path = os.path.join(THUMB_DIR, f"{message.from_user.id}.jpg")
    await message.reply_to_message.download(path)
    await message.reply("Cover saved successfully...")

@app.on_message(filters.command("d_cover") & auth_filter & ~filters.bot)
async def d_cover(client, message):
    uid = message.from_user.id
    if uid not in Config.OWNER_IDS:
        db.cursor.execute('SELECT value FROM settings WHERE key = ?', (f"set_cover_{uid}",))
        c_row = db.cursor.fetchone()
        if not c_row or c_row[0] != "1": return

    path = os.path.join(THUMB_DIR, f"{uid}.jpg")
    if os.path.exists(path):
        os.remove(path)
        await message.reply("Cover deleted successfully...")
    else:
        await message.reply("No Cover found...")

@app.on_message(filters.command("set_artist") & auth_filter & ~filters.bot)
async def set_artist(client, message):
    uid = message.from_user.id
    if uid not in Config.OWNER_IDS:
        db.cursor.execute('SELECT value FROM settings WHERE key = ?', (f"set_artist_{uid}",))
        a_row = db.cursor.fetchone()
        if not a_row or a_row[0] != "1": return

    if not message.reply_to_message or not message.reply_to_message.text:
        return await message.reply("Reply to a text msg...")
    
    artist_name = message.reply_to_message.text.strip()
    path = os.path.join(ARTIST_DIR, f"{message.from_user.id}.txt")
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write(artist_name[:100])
        await message.reply("Artist saved successfully...")
    except Exception as e:
        logger.error(f"Set Artist Error: {e}")
        await message.reply("Error saving artist name.")

@app.on_message(filters.command("d_artist") & auth_filter & ~filters.bot)
async def d_artist(client, message):
    uid = message.from_user.id
    if uid not in Config.OWNER_IDS:
        db.cursor.execute('SELECT value FROM settings WHERE key = ?', (f"set_artist_{uid}",))
        a_row = db.cursor.fetchone()
        if not a_row or a_row[0] != "1": return

    path = os.path.join(ARTIST_DIR, f"{uid}.txt")
    if os.path.exists(path):
        os.remove(path)
        await message.reply("Artist deleted successfully...")
    else:
        await message.reply("No artist found...")

@app.on_message(filters.command("saved") & auth_filter & ~filters.bot)
async def saved_cmd(client, message):
    uid = message.from_user.id
    shows = db.get_user_shows(uid)
    if not shows:
        return await message.reply("You haven't saved any stories yet...")
    
    buttons = []
    for show_id, title in shows:
        buttons.append([
            InlineKeyboardButton(text=f"{title}", callback_data=f"show_{show_id}")
        ])
    await message.reply("Your Saved Stories...", reply_markup=InlineKeyboardMarkup(buttons))


@app.on_message(filters.command(["stop", "cancel"]) & auth_filter & ~filters.bot)
async def cancel_cmd(client, message):
    uid = message.from_user.id
    logger.info(f"Stop/Cancel request by {uid}")
    if is_download_active(uid):
        cancel_flags[uid] = True
        
        # Kill only this user's active downloader subprocesses
        try:
            procs = user_processes.get(uid, [])
            for proc in procs:
                try: proc.kill()
                except: pass
        except Exception as e:
            logger.error(f"Error killing processes: {e}")

        # Immediately clear ALL state so user is free to start new tasks
        # The background task will detect cancel_flag and exit on its own
        active_downloads.pop(uid, None)
        user_queues.pop(uid, None)
        cancel_flags.pop(uid, None)
        user_processes.pop(uid, None)
        
        await message.reply("Stopping process...")
    else:
        # Force-clean any leftover ghost state just in case
        active_downloads.pop(uid, None)
        user_queues.pop(uid, None)
        cancel_flags.pop(uid, None)
        user_processes.pop(uid, None)
        await message.reply("No running task...")



@app.on_message(filters.command("gen") & auth_filter & ~filters.bot)
async def gen_cmd(client, message):
    allowed, msg = is_allowed(message)
    if not allowed:
        return
    gen_state[message.from_user.id] = True
    await message.reply("Please send the PocketFM story link.")

@app.on_message(filters.text & (filters.private | filters.group) & auth_filter & ~filters.bot)
async def handle_messages(client, message):
    if not message.from_user or message.from_user.is_bot: return
    uid = message.from_user.id
    
    # Explicitly ignore own messages to prevent recursive loops
    if uid == client.me.id: return
    
    name = message.from_user.first_name
    username = message.from_user.username
    if not username and hasattr(message.from_user, 'active_usernames') and message.from_user.active_usernames:
        username = message.from_user.active_usernames[0]
    elif not username and hasattr(message.from_user, 'usernames') and message.from_user.usernames:
        username = message.from_user.usernames[0].username
        
    if not username:
        try:
            full_user = await client.get_users(uid)
            username = full_user.username
            if not username and hasattr(full_user, 'active_usernames') and full_user.active_usernames:
                username = full_user.active_usernames[0]
            elif not username and hasattr(full_user, 'usernames') and full_user.usernames:
                username = full_user.usernames[0].username
        except Exception:
            pass
            
    is_new = db.add_user(uid, username, name)
    asyncio.create_task(download_avatar_bg(uid, client))
    
    chat_id = message.chat.id
    text = message.text.strip() if message.text else ""
    logger.info(f"Message from {uid} in {chat_id}: {text}")



    # Normal user message handling starts here...
    text = message.text.strip() if message.text else ""
    chat_id = message.chat.id
    
    # Act completely dead for unauthorized/expired users
    allowed, msg = is_allowed(message)
    if not allowed:
        return
        
    # Check if it's a PocketFM link
    if "pocketfm" in text.lower():
        show_id = None
        
        # 1. Try extracting entity_id or af_sub4 from complex parameters (AppsFlyer/OneLink)
        id_match = re.search(r'entity_id=([a-f0-9]{32,})', text)
        if not id_match:
            id_match = re.search(r'af_sub4=([a-f0-9]{32,})', text)
        
        if id_match:
            show_id = id_match.group(1)
        
        status_msg = None
        
        # 2. If not found, try resolving onelink directly
        if not show_id and ("onelink.me" in text or "appsflyersdk.com" in text):
            status_msg = await message.reply("Getting show details...")
            url_match = re.search(r'(https?://[^\s]+)', text)
            if url_match:
                url = url_match.group(1)
                try:
                    async with aiohttp.ClientSession() as session:
                        headers = {"User-Agent": "Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/114.0.0.0 Mobile Safari/537.36"}
                        async with session.get(url, headers=headers) as res:
                            html = await res.text()
                            id_match = re.search(r'entity_id=([a-f0-9]{32,})', html)
                            if not id_match:
                                id_match = re.search(r'af_sub4=([a-f0-9]{32,})', html)
                            if id_match:
                                show_id = id_match.group(1)
                except Exception as e:
                    logger.error(f"Link resolve error: {e}")

        # 3. Standard PocketFM URL patterns
        if not show_id and "pocketfm.com" in text:
            patterns = [
                r'/show/[^/]+/([a-f0-9]{32,})',
                r'/show/([a-f0-9]{32,})',
                r'/story/[^/]+/([a-f0-9]{32,})',
                r'/story/([a-f0-9]{32,})',
                r'pocketfm\.com/([a-f0-9]{32,})'
            ]
            for pattern in patterns:
                match = re.search(pattern, text)
                if match:
                    show_id = match.group(1)
                    break
            
        if not show_id:
            if status_msg:
                return await status_msg.edit("Failed to get show details...\n\nPlease check the link and try again...")
            elif "pocketfm" in text or "onelink.me" in text:
                return await message.reply("Failed to get show details...\n\nPlease check the link and try again...")
            return

        if not status_msg:
            status_msg = await message.reply("Getting show details...")
        
        user_info_level = get_user_info_level(uid, show_id)
        show_info = await downloader.get_show_info(show_id, info_level=user_info_level)
        if not show_info:
            return await status_msg.edit("Failed to get show details...\n\nPlease check the link and try again...")
        
        caption = (
            f"{show_info['title']}\n\n"
            f"Language - {show_info.get('language', 'Unknown')}\n\n"
            f"{show_info['total_episodes']} Episodes"
        )

        # Check authorization with show details (Language)
        allowed, msg = is_allowed(message, show_id=show_id, language=show_info.get('language'))
        
        await status_msg.delete()

        if not allowed:
            if show_info.get("image"):
                await client.send_photo(chat_id, photo=show_info["image"], caption=caption)
            else:
                await client.send_message(chat_id, text=caption)
                
            if msg == "This story is blocked for your account.":
                return await client.send_message(chat_id, "You are not allowed to download this story...")
            else:
                return await client.send_message(chat_id, f"You are not allowed to download {show_info.get('language', 'Unknown')} stories...")

        db.save_story(show_id, show_info['title'], f"https://www.pocketfm.com/show/{show_id}")
        user_show[chat_id] = show_id
        
        markup = get_show_markup(uid, show_id)
        if show_info.get("image"):
            await client.send_photo(chat_id, photo=show_info["image"], caption=caption, reply_markup=markup)
        else:
            await client.send_message(chat_id, text=caption, reply_markup=markup)
            
        # Send Debug Dump
        if chat_id == Config.ADMIN_GROUP:
            if hasattr(downloader, 'last_debug_info') and show_id in downloader.last_debug_info:
                if db.get_setting("debug_mode", "off") == "on":
                    file_name = f"debug_{show_id}_info.json"
                    try:
                        import json
                        with open(file_name, "w", encoding="utf-8") as f:
                            json.dump(downloader.last_debug_info[show_id], f, indent=4)
                        await client.send_document(chat_id, file_name, caption="Server Request & Response (Show Details)")
                    except: pass
                    if os.path.exists(file_name): os.remove(file_name)
                downloader.last_debug_info[show_id] = []
        return

    # Handle non-numeric input when awaiting episode range
    if chat_id in user_show and user_awaiting_range.get(chat_id) and not re.match(r'^[0-9,\- ]+$', text):
        return await message.reply("Invalid episode number...\n\nSend episode number which you want to download...\n\nSingle 1\nMultiple 1 10")

    # Handle Episode Range
    if chat_id in user_show and user_awaiting_range.get(chat_id) and re.match(r'^[0-9,\- ]+$', text):
        user_awaiting_range[chat_id] = False
        # First check generic authorization (just in case)
        allowed, msg = is_allowed(message)
        if not allowed:
            return await message.reply(msg or "You are not authorized to use this bot.")

        show_id = user_show.get(chat_id)
        invalid_msg = "Invalid episode number...\n\nSend episode number which you want to download...\n\nSingle 1\nMultiple 1 10"
        
        try:
            episodes = parse_range(text)
        except Exception as e:
            if str(e) == "INVALID_RANGE":
                user_awaiting_range[chat_id] = True
                return await message.reply(invalid_msg)
            return await message.reply(f"Invalid range: {str(e)}\n\nExample: `1 10`")
            
        if not episodes:
            user_awaiting_range[chat_id] = True
            return await message.reply(invalid_msg)

        start_seq, end_seq = min(episodes), max(episodes)

        is_all = user_is_all_download.pop(chat_id, False)
        
        task_data = {
            "show_id": show_id,
            "episodes": episodes,
            "start_seq": start_seq,
            "end_seq": end_seq,
            "is_all": is_all,
            "text": text,
            "chat_id": chat_id,
            "message": message
        }

        if uid not in user_queues:
            user_queues[uid] = []

        if is_download_active(uid):
            if len(user_queues[uid]) >= 2:
                return await message.reply("Maximum 3 task...\n\nPlease wait for the running task to finish and try again...")
            else:
                user_queues[uid].append(task_data)
                return await message.reply("Added to waiting list...")

        user_queues[uid].append(task_data)
        active_downloads[uid] = True
        if chat_id != uid:
            active_downloads[chat_id] = True
            
        task_counter = 0
        _cleanup_done = False

        try:
            while user_queues[uid]:
                current_task = user_queues[uid].pop(0)
                task_counter += 1
                
                t_show_id = current_task["show_id"]
                t_episodes = current_task["episodes"]
                t_start_seq = current_task["start_seq"]
                t_end_seq = current_task["end_seq"]
                t_is_all = current_task["is_all"]
                t_text = current_task["text"]
                t_chat_id = current_task["chat_id"]
                t_msg = current_task["message"]

                if task_counter > 1:
                    await t_msg.reply(f"Starting Task {task_counter}...")

                logger.info(f"Starting download for user {uid}: {t_show_id} range {t_start_seq}-{t_end_seq}")
                
                story_title = "Unknown Story"
                ep_text = f"{t_start_seq}-{t_end_seq}" if t_start_seq != t_end_seq else str(t_start_seq)
                if len(t_episodes) > 1 and (t_end_seq - t_start_seq + 1) != len(t_episodes):
                    ep_text = t_text
                    
                # Log the request
                try:
                    show_info_for_log = await downloader.get_show_info(t_show_id, info_level=get_user_info_level(uid, t_show_id))
                    if show_info_for_log:
                        story_title = show_info_for_log.get('title', 'Unknown Story')
                        image_url = show_info_for_log.get('image')
                    else:
                        db_story = db.get_story(t_show_id)
                        story_title = db_story[1] if db_story else 'Unknown Story'
                        image_url = None

                    req_log_text = (
                        "Episode Request\n\n"
                        f"User: {t_msg.from_user.first_name} (@{t_msg.from_user.username or 'None'})\n"
                        f"ID: `{uid}`\n"
                        f"Story: {story_title}\n"
                        f"Episodes: {ep_text}\n"
                        f"Time: {datetime.now().strftime('%Y-%m-%d %I:%M:%S %p IST')}"
                    )
                    await send_log("log_req_channel", req_log_text, photo=image_url)
                except Exception as e:
                    logger.error(f"Error preparing request log: {e}")

                if t_is_all:
                    status_msg = await t_msg.reply("Downloading all episodes...\n\nIf you want to cancel or stop the process just send /stop")
                else:
                    if t_start_seq == t_end_seq:
                        status_msg = await t_msg.reply(f"Downloading Ep - {t_start_seq}\n\nIf you want to cancel or stop the process just send /stop")
                    else:
                        status_msg = await t_msg.reply(f"Downloading Ep from {t_start_seq} - {t_end_seq}\n\nIf you want to cancel or stop the process just send /stop")

                pipeline_state = {
                    "discovered": 0, "downloaded": 0, "uploaded": 0,
                    "failed": 0, "total": len(t_episodes),
                    "status": "Starting discovery..."
                }
                
                cancel_flags[uid] = False
                discovered_episodes = set()
                successful_uploads = 0
                successful_downloads = 0
                download_failed_eps = []
                upload_failed_eps = []
                episode_titles = {}
                thumb_path = os.path.join(THUMB_DIR, f"{uid}.jpg")
                thumb = thumb_path if os.path.exists(thumb_path) else None
                artist_path = os.path.join(ARTIST_DIR, f"{uid}.txt")

                upload_queue = asyncio.Queue()
                semaphore = asyncio.Semaphore(1)
                discovery_done_event = asyncio.Event()
                
                locked_episodes = set()
                episode_lock = asyncio.Lock()
                msg_objs = {}
                
                async def perform_upload(task_data):
                    nonlocal successful_uploads, successful_downloads
                    seq, filepath, duration = task_data[:3]
                    error_reason = task_data[3] if len(task_data) > 3 else None
                    ep_title = episode_titles.get(seq, f"Ep {seq}")
                    
                    if not filepath:
                        logger.error(f"Download failed upstream for Ep {seq}")
                        pipeline_state["failed"] += 1
                        download_failed_eps.append(seq)
                        if seq in locked_episodes:
                            episode_lock.release()
                            locked_episodes.remove(seq)
                        semaphore.release()
                        # Delete downloading msg and send error serially
                        asyncio.create_task(bg_delete(seq))
                        if error_reason == "not_found":
                            await bg_send_plain(f"Download Error...\n\nEpisode {seq} not found...")
                        else:
                            await bg_send_plain(f"Download Error...\n\n{ep_title}")
                        return

                    successful_downloads += 1
                    pipeline_state["status"] = f"Uploading Ep {seq}..."
                    upload_gap = int(db.get_setting("upload_gap", 0))

                    try:
                        filename = os.path.basename(filepath)
                        title = os.path.splitext(filename)[0]
                        artist_name = "Unknown Artist"
                        if os.path.exists(artist_path):
                            try:
                                with open(artist_path, "r") as f: artist_name = f.read().strip()
                            except: pass
                        
                        logger.info(f"Aiogram Upload Start for Ep {seq}: {title}")
                        
                        # Edit tracked msg to "Uploading..." (fire-and-forget)
                        asyncio.create_task(bg_edit(seq, f"Uploading...\n\n{ep_title}"))
                        
                        # Start upload IMMEDIATELY
                        res_msg = None
                        for attempt in range(5):
                            if cancel_flags.get(uid): break
                            
                            # Edit retry notification (from 2nd attempt onwards)
                            if attempt > 0:
                                asyncio.create_task(bg_edit(seq, f"Uploading...\nTrying {attempt + 1}\n\n{ep_title}"))
                            
                            try:
                                file_size = os.path.getsize(filepath) if os.path.exists(filepath) else 0
                                if file_size > 49 * 1024 * 1024:
                                    logger.info(f"File size {file_size} is > 49MB. Using Pyrogram for upload...")
                                    res_msg = await app.send_audio(
                                        t_chat_id, audio=filepath, caption=title,
                                        title=title, performer=artist_name, duration=duration, thumb=thumb
                                    )
                                else:
                                    res_msg = await fast_upload(t_chat_id, filepath, title, artist_name, duration, thumb)
                                if res_msg: break
                            except (FloodWait) as e:
                                wait_time = e.value if hasattr(e, "value") else (e.retry_after if hasattr(e, "retry_after") else 30)
                                logger.warning(f"FloodWait in uploader Ep {seq}: {wait_time}s")
                                await asyncio.sleep(wait_time + 1)
                            except Exception as e:
                                logger.error(f"Upload attempt {attempt+1} failed for Ep {seq}: {e}")
                                await asyncio.sleep(2)
                        
                        if res_msg:
                            logger.info(f"Aiogram upload SUCCESS for Ep {seq}")
                            try:
                                if os.path.exists(filepath): os.remove(filepath)
                                parent_dir = os.path.dirname(filepath)
                                if parent_dir != Config.DOWNLOAD_DIR and os.path.exists(parent_dir):
                                    if not os.listdir(parent_dir): 
                                        shutil.rmtree(parent_dir, ignore_errors=True)
                                        uid_dir = os.path.dirname(parent_dir)
                                        if uid_dir != Config.DOWNLOAD_DIR and os.path.exists(uid_dir):
                                            if not os.listdir(uid_dir):
                                                shutil.rmtree(uid_dir, ignore_errors=True)
                            except Exception as e: logger.error(f"Cleanup error: {e}")

                            successful_uploads += 1
                            pipeline_state["uploaded"] += 1
                        else:
                            logger.error(f"Aiogram upload PERMANENTLY FAILED for Ep {seq}")
                            pipeline_state["failed"] += 1
                            upload_failed_eps.append(seq)
                            # Fire-and-forget upload error message
                            asyncio.create_task(bg_send_plain(f"Upload Error...\n\n{ep_title}"))
                        
                        if upload_gap > 0: await asyncio.sleep(upload_gap)
                                    
                    except Exception as e:
                        logger.error(f"Critical error in uploader Ep {seq}: {e}")
                        pipeline_state["failed"] += 1
                    finally:
                        # Delete status message after upload (fire-and-forget)
                        asyncio.create_task(bg_delete(seq))
                        if seq in locked_episodes:
                            episode_lock.release()
                            locked_episodes.remove(seq)
                        semaphore.release()

                async def upload_worker():
                    upload_buffer = {}
                    next_seq_idx = 0
                    while True:
                        if not cancel_flags.get(uid) and next_seq_idx < len(t_episodes):
                            target_seq = t_episodes[next_seq_idx]
                            if target_seq in upload_buffer:
                                t_data = upload_buffer.pop(target_seq)
                                await perform_upload(t_data)
                                next_seq_idx += 1
                                continue
                            
                            if discovery_done_event.is_set() and target_seq not in discovered_episodes:
                                logger.warning(f"Ep {target_seq} was never discovered. Skipping.")
                                next_seq_idx += 1
                                continue
                        
                        try:
                            task = await asyncio.wait_for(upload_queue.get(), timeout=1.0)
                            if task is None:
                                upload_queue.task_done()
                                if not cancel_flags.get(uid):
                                    while next_seq_idx < len(t_episodes):
                                        target_seq = t_episodes[next_seq_idx]
                                        if target_seq in upload_buffer:
                                            t_data = upload_buffer.pop(target_seq)
                                            await perform_upload(t_data)
                                        next_seq_idx += 1
                                break
                            
                            if cancel_flags.get(uid):
                                upload_queue.task_done()
                                continue
                                
                            seq = task[0]
                            upload_buffer[seq] = task
                            upload_queue.task_done()
                        except asyncio.TimeoutError:
                            pass

                async def download_complete_callback(seq, filepath, duration, error_reason=None):
                    await upload_queue.put((seq, filepath, duration, error_reason))
                    if filepath:
                        pipeline_state["downloaded"] += 1

                async def discovery_callback(seq):
                    if not cancel_flags.get(uid):
                        await semaphore.acquire()
                    discovered_episodes.add(seq)
                    pipeline_state["discovered"] += 1
                    
                async def bg_send(seq, text):
                    """Send message and track it for later edit/delete (fire-and-forget)"""
                    try:
                        msg = await client.send_message(t_chat_id, text)
                        msg_objs[seq] = msg
                    except: pass

                async def bg_edit(seq, text):
                    """Edit tracked message (fire-and-forget)"""
                    if seq in msg_objs:
                        try:
                            await msg_objs[seq].edit(text)
                        except: pass

                async def bg_delete(seq):
                    """Delete tracked message (fire-and-forget)"""
                    if seq in msg_objs:
                        try:
                            await msg_objs[seq].delete()
                        except: pass
                        msg_objs.pop(seq, None)

                async def bg_send_plain(text):
                    """Send a standalone message, not tracked (fire-and-forget)"""
                    try:
                        await client.send_message(t_chat_id, text)
                    except: pass

                async def start_download_callback(seq, title):
                    await episode_lock.acquire()
                    locked_episodes.add(seq)
                    try:
                        import re
                        clean_title = re.sub(r'^(?:(?:Ep|Episode|E|Ch|Chapter|C)[\s\-.:,]*\d+[\s\-.:,]*)+', '', title, flags=re.IGNORECASE).strip()
                        clean_title = re.sub(r'^\d+[\s\-.:,]+', '', clean_title).strip()
                        display_title = f"Ep {seq} - {clean_title}" if clean_title else f"Ep {seq}"
                        episode_titles[seq] = display_title
                    except Exception as e:
                        episode_titles[seq] = f"Ep {seq}"
                        logger.error(f"Failed to process title for Ep {seq}: {e}")
                    # Send "Downloading..." in background (tracked for later edit/delete)
                    asyncio.create_task(bg_send(seq, f"Downloading...\n\n{episode_titles[seq]}"))

                async def download_retry_callback(seq, attempt_num):
                    """Called when download retries — edits the tracked message"""
                    ep_title = episode_titles.get(seq, f"Ep {seq}")
                    asyncio.create_task(bg_edit(seq, f"Downloading...\nTrying {attempt_num}\n\n{ep_title}"))

                up_task = asyncio.create_task(upload_worker())

                try:
                    pipeline_state["status"] = "Downloading episodes..."
                    # Initialize per-user process list for cancel tracking
                    if uid not in user_processes:
                        user_processes[uid] = []
                    user_dl_dir = os.path.join(Config.DOWNLOAD_DIR, str(uid))
                    dl_result = await downloader.download_episodes(
                        t_show_id, min(t_episodes), max(t_episodes), user_dl_dir,
                        progress_callback=discovery_callback, cancel_flag=lambda: cancel_flags.get(uid),
                        on_complete=download_complete_callback, on_start=start_download_callback,
                        discovery_done=discovery_done_event, info_level=get_user_info_level(uid, t_show_id),
                        process_tracker=user_processes[uid], on_retry=download_retry_callback
                    )
                    
                    await upload_queue.put(None)
                    try:
                        await asyncio.wait_for(up_task, timeout=15)
                    except asyncio.TimeoutError:
                        logger.warning(f"Upload worker timed out for user {uid}, force cancelling...")
                        up_task.cancel()
                        try:
                            await up_task
                        except (asyncio.CancelledError, Exception):
                            pass
                    
                    if t_chat_id == Config.ADMIN_GROUP:
                        if hasattr(downloader, 'last_debug_info') and t_show_id in downloader.last_debug_info:
                            if db.get_setting("debug_mode", "off") == "on":
                                file_name = f"debug_{t_show_id}_eps.json"
                                try:
                                    import json
                                    with open(file_name, "w", encoding="utf-8") as f:
                                        json.dump(downloader.last_debug_info[t_show_id], f, indent=4)
                                    await client.send_document(t_chat_id, file_name, caption="Server Request & Response (Episodes Data)")
                                except: pass
                                if os.path.exists(file_name): os.remove(file_name)
                            downloader.last_debug_info[t_show_id] = []
                    
                    if cancel_flags.get(uid) or not active_downloads.get(uid):
                        # cancel_cmd already cleaned up all state
                        _cleanup_done = True
                        break
                    elif dl_result and dl_result.get("abort_reason") == "many_not_found":
                        await t_msg.reply("Many episodes are not found for this show, check your episode number and try again...\n\nTask Completed...")
                    elif successful_uploads > 0 or successful_downloads > 0:
                        # Build detailed task summary
                        total_requested = len(t_episodes)
                        
                        dl_failed_count = len(download_failed_eps)
                        dl_failed_nums = f" ({', '.join(str(e) for e in sorted(download_failed_eps))})" if dl_failed_count > 0 else ""
                        
                        ul_failed_count = len(upload_failed_eps)
                        ul_failed_nums = f" ({', '.join(str(e) for e in sorted(upload_failed_eps))})" if ul_failed_count > 0 else ""
                        
                        summary = (
                            f"Task Completed...\n\n"
                            f"Total - {total_requested}\n\n"
                            f"Downloaded - {successful_downloads}\n"
                            f"Failed - {dl_failed_count}{dl_failed_nums}\n\n"
                            f"Uploaded - {successful_uploads}\n"
                            f"Failed - {ul_failed_count}{ul_failed_nums}"
                        )
                        
                        await t_msg.reply(summary)
                        if not user_queues.get(uid) and task_counter > 1:
                            await t_msg.reply("All Task Completed...")
                    else:
                        error_msg = getattr(downloader, "last_download_error", None)
                        user_name = t_msg.from_user.first_name if t_msg.from_user else "Unknown"
                        err_text = (
                            f"Download Failed...\n\n"
                            f"`{uid}`\n{user_name}\n\n"
                            f"`{t_show_id}`\n{story_title}\n\n"
                            f"Episodes: {ep_text}\n\n"
                            f"Reason...\n{error_msg or 'Unknown Error'}"
                        )
                        try:
                            await client.send_message(Config.ADMIN_GROUP, err_text)
                        except: pass
                        
                except Exception as e:
                    logger.error(f"Pipeline error: {e}", exc_info=True)
                    user_name = t_msg.from_user.first_name if t_msg.from_user else "Unknown"
                    err_text = (
                        f"Pipeline Error...\n\n"
                        f"`{uid}`\n{user_name}\n\n"
                        f"`{t_show_id}`\n{story_title}\n\n"
                        f"Episodes: {ep_text}\n\n"
                        f"Reason...\n{str(e)[:500]}"
                    )
                    try:
                        await client.send_message(Config.ADMIN_GROUP, err_text)
                    except: pass
                except BaseException as e:
                    # Catches CancelledError, KeyboardInterrupt, SystemExit etc.
                    logger.error(f"Pipeline killed: {type(e).__name__}: {e}")
                    user_name = t_msg.from_user.first_name if t_msg.from_user else "Unknown"
                    err_text = (
                        f"Pipeline Killed...\n\n"
                        f"`{uid}`\n{user_name}\n\n"
                        f"`{t_show_id}`\n{story_title}\n\n"
                        f"Episodes: {ep_text}\n\n"
                        f"Reason...\n{type(e).__name__}"
                    )
                    try:
                        await client.send_message(Config.ADMIN_GROUP, err_text)
                    except: pass
                    break
        except BaseException as e:
            logger.error(f"Task loop killed: {type(e).__name__}: {e}")
        finally:
            if not _cleanup_done:
                active_downloads.pop(uid, None)
                if chat_id != uid:
                    active_downloads.pop(chat_id, None)
                cancel_flags.pop(uid, None)
                user_processes.pop(uid, None)
                user_queues.pop(uid, None)
            logger.info(f"Cleanup complete for user {uid}")
    elif len(text) >= 3 and not text.startswith('/'):
        results = db.search_stories(text)
        if results:
            res_text = f"Search Results for '{text}':"
            buttons = []
            for show_id, title in results:
                buttons.append([InlineKeyboardButton(text=title, callback_data=f"show_{show_id}")])
            await message.reply(res_text, reply_markup=InlineKeyboardMarkup(buttons))
        else:
            return



@app.on_callback_query(filters.regex(r"^show_") & auth_filter)
async def show_callback(client, callback_query):
    show_id = callback_query.data.split("_")[1]
    chat_id = callback_query.message.chat.id
    
    await callback_query.message.delete()
    status_msg = await client.send_message(chat_id, "Getting show details...")
    
    show_info = await downloader.get_show_info(show_id, info_level=get_user_info_level(callback_query.from_user.id, show_id))
    if not show_info:
        return await status_msg.edit("Failed to get show details...\n\nPlease check the link and try again...")
    
    caption = (
        f"{show_info['title']}\n\n"
        f"Language - {show_info.get('language', 'Unknown')}\n\n"
        f"{show_info['total_episodes']} Episodes"
    )

    # Check authorization with show details (Language)
    allowed, msg = is_allowed(callback_query, show_id=show_id, language=show_info.get('language'))
    
    await status_msg.delete()

    if not allowed:
        if show_info.get("image"):
            await client.send_photo(chat_id, photo=show_info["image"], caption=caption)
        else:
            await client.send_message(chat_id, text=caption)
        return await client.send_message(chat_id, f"You are not allowed to download {show_info.get('language', 'Unknown')} stories...")

    db.save_story(show_id, show_info['title'], f"https://www.pocketfm.com/show/{show_id}")
    user_show[chat_id] = show_id
    
    markup = get_show_markup(callback_query.from_user.id, show_id)
    if show_info.get("image"):
        await client.send_photo(chat_id, photo=show_info["image"], caption=caption, reply_markup=markup)
    else:
        await client.send_message(chat_id, text=caption, reply_markup=markup)

@app.on_callback_query(filters.regex(r"^multiple_") & auth_filter)
async def multiple_callback(client, callback_query):
    show_id = callback_query.data.split("_")[1]
    chat_id = callback_query.message.chat.id
    try:
        await callback_query.edit_message_reply_markup(reply_markup=None)
    except:
        pass
    user_awaiting_range[chat_id] = True
    user_show[chat_id] = show_id
    await callback_query.answer()
    await client.send_message(chat_id, "Send episode number which you want to download...\n\nSingle 1\nMultiple 1 10")

@app.on_callback_query(filters.regex(r"^all_") & auth_filter)
async def all_callback(client, callback_query):
    show_id = callback_query.data.split("_")[1]
    chat_id = callback_query.message.chat.id
    try:
        await callback_query.edit_message_reply_markup(reply_markup=None)
    except:
        pass
    await callback_query.answer()
    
    show_info = await downloader.get_show_info(show_id, info_level=get_user_info_level(callback_query.from_user.id, show_id))
    if not show_info:
        return
    total = show_info.get("total_episodes", 1)
    
    user_show[chat_id] = show_id
    user_awaiting_range[chat_id] = True
    user_is_all_download[chat_id] = True
    
    fake_msg = callback_query.message
    fake_msg.from_user = callback_query.from_user
    fake_msg.text = f"1 {total}"
    
    await handle_messages(client, fake_msg)

@app.on_callback_query(filters.regex(r"^save_") & auth_filter)
async def save_callback(client, callback_query):
    show_id = callback_query.data.split("_")[1]
    uid = callback_query.from_user.id
    db.save_user_show(uid, show_id)
    try:
        await callback_query.edit_message_reply_markup(reply_markup=get_show_markup(uid, show_id))
        await callback_query.answer()
    except: pass

@app.on_callback_query(filters.regex(r"^unsave_") & auth_filter)
async def unsave_callback(client, callback_query):
    show_id = callback_query.data.split("_")[1]
    uid = callback_query.from_user.id
    db.remove_user_show(uid, show_id)
    try:
        await callback_query.edit_message_reply_markup(reply_markup=get_show_markup(uid, show_id))
        await callback_query.answer()
    except: pass

@app.on_callback_query(filters.regex(r"^delsave_") & auth_filter)
async def delsave_callback(client, callback_query):
    show_id = callback_query.data.split("_")[1]
    uid = callback_query.from_user.id
    db.remove_user_show(uid, show_id)
    
    shows = db.get_user_shows(uid)
    if not shows:
        try:
            await callback_query.message.edit("You have no saved stories.")
            return await callback_query.answer("Removed!", show_alert=False)
        except: pass
    
    buttons = []
    for s_id, title in shows:
        buttons.append([
            InlineKeyboardButton(text=f"{title}", callback_data=f"show_{s_id}"),
            InlineKeyboardButton(text="Remove", callback_data=f"delsave_{s_id}")
        ])
    try:
        await callback_query.message.edit_reply_markup(reply_markup=InlineKeyboardMarkup(buttons))
        await callback_query.answer("Removed from saved list.", show_alert=False)
    except: pass

async def main():
    await app.start()
    logger.info("Bots started!")
    
    restart_msg = None
    restart_action = "update"
    # Check for restart state
    if os.path.exists("restart.txt"):
        try:
            with open("restart.txt", "r") as f:
                content = f.read().strip().split("|")
                chat_id = int(content[0])
                msg_id = int(content[1]) if len(content) > 1 else None
                restart_action = content[2] if len(content) > 2 else "update"
                
            success_text = "Bot restarted successfully..." if restart_action == "restart" else "Bot is running and updated successfully..."
            
            if msg_id:
                try:
                    restart_msg = await app.edit_message_text(chat_id, msg_id, success_text)
                except:
                    restart_msg = await app.send_message(chat_id, success_text)
            else:
                restart_msg = await app.send_message(chat_id, success_text)
            os.remove("restart.txt")
        except Exception as e:
            logger.error(f"Failed to send restart message: {e}")

    # --- Startup Cleanup: Clear ALL user tasks on restart/update ---
    logger.info("Performing startup cleanup — clearing all user tasks...")
    user_queues.clear()
    active_downloads.clear()
    cancel_flags.clear()
    user_processes.clear()
    user_show.clear()
    user_awaiting_range.clear()
    user_is_all_download.clear()
    gen_state.clear()
    
    # Clean up any leftover download files from previous session
    download_dir = Config.DOWNLOAD_DIR
    if os.path.exists(download_dir):
        for item in os.listdir(download_dir):
            item_path = os.path.join(download_dir, item)
            if item.startswith('.'):
                continue
            try:
                if os.path.isdir(item_path):
                    shutil.rmtree(item_path, ignore_errors=True)
                elif os.path.isfile(item_path):
                    os.remove(item_path)
            except Exception as e:
                logger.error(f"Startup cleanup error for {item}: {e}")
    logger.info("Startup cleanup completed — all tasks and temp files cleared.")
    # --- End Startup Cleanup ---
            
    # Start Cloudflare Tunnel and Flask Dashboard
    global dashboard_port
    dashboard_port = get_free_port()
    threading.Thread(target=start_flask, args=(dashboard_port,), daemon=True).start()
    logger.info(f"Flask Dashboard running on port {dashboard_port}")
    await ensure_cloudflared()
    restart_tunnel()
    
    # Wait for tunnel URL
    global tunnel_url
    for _ in range(30):
        if tunnel_url:
            break
        await asyncio.sleep(0.5)
        
    if tunnel_url:
        dashboard_msg = f"Dashboard URL...\n\n`{dashboard.DASHBOARD_PASSWORD}`\n\n{tunnel_url}"
        if restart_msg:
            try: 
                success_text = "Bot restarted successfully..." if restart_action == "restart" else "Bot is running and updated successfully..."
                await restart_msg.edit(f"{success_text}\n\n{dashboard_msg}")
            except: pass
        else:
            try: await app.send_message(Config.ADMIN_GROUP, dashboard_msg)
            except Exception as e: logger.error(f"Failed to send dashboard URL: {e}")
            
    # Start auto delete task in background
    asyncio.create_task(auto_delete_task())
    asyncio.create_task(check_expired_task())
    await idle()
    await app.stop()
    await aio_bot.session.close()

if __name__ == "__main__":
    asyncio.get_event_loop().run_until_complete(main())


