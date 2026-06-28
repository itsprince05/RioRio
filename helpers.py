import time
import asyncio
from config import Config


def progress_bar(percent, width=12):
    percent = max(0, min(100, percent))
    filled = int(width * percent / 100)
    bar = "⬢" * filled + "⬡" * (width - filled)
    return f"[{bar}]"


def format_bytes(size):
    if size <= 0:
        return "0B"

    power = 1024
    n = 0
    units = ["B","KB","MB","GB","TB"]

    while size >= power and n < 4:
        size /= power
        n += 1

    return f"{size:.2f}{units[n]}"


def format_time(seconds):
    if seconds <= 0:
        return "0s"

    seconds = int(seconds)
    m, s = divmod(seconds, 60)
    h, m = divmod(m, 60)

    if h:
        return f"{h}h {m}m {s}s"

    return f"{m}m {s}s"


last_edit_time = {}

async def safe_edit(message, text, force=False):
    msg_id = message.id
    now = time.time()
    if not force and now - last_edit_time.get(msg_id, 0) < 1.5:
        return
    try:
        await message.edit(text)
        last_edit_time[msg_id] = now
    except:
        pass


DOWNLOAD_FRAMES = ["Downloading from server.", "Downloading from server..", "Downloading from server..."]
UPLOAD_FRAMES = ["Uploading to Telegram.", "Uploading to Telegram..", "Uploading to Telegram..."]

async def download_progress(percent, processed, total, speed, eta, elapsed, message):
    frame = DOWNLOAD_FRAMES[int(time.time()) % len(DOWNLOAD_FRAMES)]
    ep_num = min(processed + 1, total) if total > 0 else 1
    text = (
        f"Starting to send {total} episode(s)...\n\n"
        f"Sending episode {ep_num}...\n"
        f"{frame}"
    )
    await safe_edit(message, text)

async def upload_progress(current, total, message, start, current_ep=0, total_ep=0):
    frame = UPLOAD_FRAMES[int(time.time()) % len(UPLOAD_FRAMES)]
    text = (
        f"Starting to send {total_ep} episode(s)...\n\n"
        f"Sending episode {current_ep}...\n"
        f"{frame}"
    )
    force = (current == total and current > 0)
    await safe_edit(message, text, force=force)


def parse_range(text):
    text = text.strip()
    # Support space separated range "1 10"
    if " " in text and "-" not in text and "," not in text:
        parts = text.split()
        if len(parts) == 2:
            try:
                start, end = int(parts[0]), int(parts[1])
                if start > end:
                    raise ValueError("INVALID_RANGE")
                return list(range(start, end + 1))
            except ValueError as e:
                if str(e) == "INVALID_RANGE":
                    raise e
                pass

    episodes = set()
    for part in text.split(","):
        part = part.strip()
        if not part: continue
        if "-" in part:
            try:
                start, end = part.split("-")
                start, end = int(start), int(end)
                if start > end:
                    raise ValueError("INVALID_RANGE")
                for i in range(start, end + 1):
                    episodes.add(i)
            except ValueError as e:
                if str(e) == "INVALID_RANGE":
                    raise e
                continue
        else:
            try:
                episodes.add(int(part))
            except ValueError:
                continue
    return sorted(list(episodes))


def is_allowed(update, show_id=None, language=None):
    from database import db
    from datetime import datetime
    user_id = update.from_user.id if update.from_user else getattr(update.sender_chat, "id", 0)
    
    # Extract chat_id from Message or CallbackQuery.message
    if hasattr(update, "chat") and update.chat:
        chat = update.chat
        chat_id = chat.id
    elif hasattr(update, "message") and update.message and update.message.chat:
        chat = update.message.chat
        chat_id = chat.id
    else:
        chat = None
        chat_id = 0
        
    if chat and hasattr(chat, "type") and getattr(chat.type, "name", "") == "CHANNEL":
        return False, "Channels are not supported."
        
    is_group = str(chat_id).startswith('-')
    if is_group and chat_id == getattr(Config, "ADMIN_GROUP", 0):
        return True, None

    if user_id in Config.OWNER_IDS:
        return True, None

    # Check Subscriptions
    subs = db.cursor.execute('SELECT sub_type, sub_data, expiry FROM subscriptions WHERE user_id = ?', (user_id,)).fetchall()
    if not subs:
        return False, "You are not authorized to use this bot."

    from datetime import timezone, timedelta
    ist = timezone(timedelta(hours=5, minutes=30))
    now = datetime.now(ist)
    
    general_validity_active = False
    has_expired = False
    
    for sub_type, sub_data, expiry in subs:
        if sub_type in ('validity', 'all', 'language', 'custom_story', 'extra_episode'):
            if expiry:
                try:
                    expiry_dt = datetime.fromisoformat(expiry)
                    if now <= expiry_dt:
                        general_validity_active = True
                        break
                    else:
                        has_expired = True
                except:
                    general_validity_active = True
                    break
            else:
                general_validity_active = True
                break

    if not general_validity_active:
        return False, "Subscription Expired Renew it" if has_expired else "You are not authorized to use this bot."
    
    is_general_check = (show_id is None and language is None)

    is_blocked = False
    for sub_type, sub_data, expiry in subs:
        if sub_type == 'blocked_story' and show_id and sub_data and show_id == sub_data:
            is_blocked = True
            break

    if is_blocked:
        return False, "This story is blocked for your account."

    final_allow = False
    for sub_type, sub_data, expiry in subs:
        if is_general_check:
            final_allow = True
            break
        elif sub_type == 'all':
            final_allow = True
            break
        elif sub_type == 'selected_story':
            if show_id and sub_data and show_id == sub_data:
                final_allow = True
                break
        elif sub_type == 'language':
            if language and sub_data and language.lower() == sub_data.lower():
                final_allow = True
                break
                
    if final_allow:
        if is_group and chat:
            chat_title = chat.title or "Unknown Group"
            chat_username = chat.username or ""
            db.update_buyer_group(chat_id, chat_title, chat_username, user_id)
        return True, None
    
    return False, "You are not authorized to access this story."
