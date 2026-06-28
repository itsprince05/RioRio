import os
import random
import string
import time
from flask import Flask, request, jsonify, render_template, send_file, session, redirect, url_for
import logging
import urllib.request
import json
from config import Config
from database import db

BOT_DIR = os.path.dirname(os.path.abspath(__file__))
AVATARS_DIR = os.path.join(BOT_DIR, "avatars")
os.makedirs(AVATARS_DIR, exist_ok=True)

flask_app = Flask(__name__, template_folder=BOT_DIR)
flask_app.secret_key = os.urandom(24)
log = logging.getLogger('werkzeug')
log.setLevel(logging.ERROR)

DASHBOARD_PASSWORD = ''.join(random.choices(string.ascii_letters + string.digits, k=20))

def update_password():
    global DASHBOARD_PASSWORD
    DASHBOARD_PASSWORD = ''.join(random.choices(string.ascii_letters + string.digits, k=20))
    return DASHBOARD_PASSWORD

def get_avatar_v(uid):
    path = os.path.join(AVATARS_DIR, f"{uid}.jpg")
    try:
        return int(os.path.getmtime(path))
    except:
        return 0

@flask_app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        if request.is_json:
            pwd = request.json.get('password')
        else:
            pwd = request.form.get('password')
            
        if pwd == DASHBOARD_PASSWORD:
            session['logged_in'] = True
            session['login_time'] = time.time()
            if request.is_json:
                return jsonify({"success": True})
            return redirect(url_for('index'))
        else:
            if request.is_json:
                return jsonify({"success": False, "error": "Invalid Password"})
            return render_template('login.html', error="Invalid Password")
    return render_template('login.html')

@flask_app.before_request
def require_login():
    if request.endpoint in ['login', 'static']:
        return
    if not session.get('logged_in'):
        return redirect(url_for('login'))
    if time.time() - session.get('login_time', 0) > 3600:
        session.clear()
        return redirect(url_for('login'))

@flask_app.route('/')
def index():
    return render_template('dashboard.html')

@flask_app.route('/user/<userid>')
def user_page(userid):
    userid = int(userid)
    db.cursor.execute('SELECT first_name, username FROM users WHERE user_id = ?', (userid,))
    u_row = db.cursor.fetchone()
    real_name = u_row[0] if u_row else "Unknown"
    
    db.cursor.execute('SELECT value FROM settings WHERE key = ?', (f"buyer_name_{userid}",))
    c_row = db.cursor.fetchone()
    name = c_row[0] if c_row else ""
    
    db.cursor.execute('SELECT sub_data FROM subscriptions WHERE user_id = ? AND sub_type = "language"', (userid,))
    allowed_langs = [r[0] for r in db.cursor.fetchall()]
    
    db.cursor.execute('SELECT 1 FROM subscriptions WHERE user_id = ? AND sub_type = "all"', (userid,))
    has_all = bool(db.cursor.fetchone())
    
    db.cursor.execute('SELECT 1 FROM subscriptions WHERE user_id = ? AND sub_type = "extra_episode"', (userid,))
    extra_episode = bool(db.cursor.fetchone())
    
    db.cursor.execute('SELECT 1 FROM subscriptions WHERE user_id = ? AND sub_type = "custom_story"', (userid,))
    custom_story = bool(db.cursor.fetchone())
    
    if not allowed_langs and not has_all:
        custom_story = True
        
    db.cursor.execute('SELECT value FROM settings WHERE key = ?', (f"set_cover_{userid}",))
    c_row = db.cursor.fetchone()
    set_cover = (c_row[0] == "1") if c_row else False

    db.cursor.execute('SELECT value FROM settings WHERE key = ?', (f"set_artist_{userid}",))
    a_row = db.cursor.fetchone()
    set_artist = (a_row[0] == "1") if a_row else False
    
    username = u_row[1] if u_row and u_row[1] else ""
    
    db.cursor.execute('''
        SELECT us.show_id, s.title 
        FROM user_saves us 
        LEFT JOIN stories s ON us.show_id = s.show_id 
        WHERE us.user_id = ?
    ''', (userid,))
    saved_shows = [{"id": r[0], "title": r[1] or str(r[0])} for r in db.cursor.fetchall()]
    saved_shows.sort(key=lambda x: str(x['title']).lower())
    
    db.cursor.execute('SELECT expiry FROM subscriptions WHERE user_id = ? AND sub_type IN ("validity", "all", "language", "custom_story", "extra_episode") LIMIT 1', (userid,))
    exp_row = db.cursor.fetchone()
    
    expiry_text = "No active validity"
    expiry_color = "#666"
    
    db.cursor.execute('SELECT 1 FROM subscriptions WHERE user_id = ? LIMIT 1', (userid,))
    if db.cursor.fetchone():
        if exp_row and exp_row[0]:
            from datetime import datetime, timezone, timedelta
            try:
                ist = timezone(timedelta(hours=5, minutes=30))
                now = datetime.now(ist)
                exp_dt = datetime.fromisoformat(exp_row[0])
                if exp_dt.tzinfo is None:
                    exp_dt = exp_dt.replace(tzinfo=ist)
                
                fmt = exp_dt.strftime("%I:%M %p %d/%m/%Y")
                if fmt.startswith("0"):
                    fmt = fmt[1:]
                
                if exp_dt > now:
                    expiry_text = f"Validity until {fmt}"
                    expiry_color = "#2b8a3e"
                else:
                    expiry_text = f"Validity expired on {fmt}"
                    expiry_color = "#fa5252"
            except:
                expiry_text = "Lifetime validity"
                expiry_color = "#2b8a3e"
        else:
            expiry_text = "Lifetime validity"
            expiry_color = "#2b8a3e"
    
    return render_template('user_shows.html', userid=str(userid), name=name, real_name=real_name, username=username, allowed_langs=allowed_langs, has_all=has_all, extra_episode=extra_episode, custom_story=custom_story, saved_shows=saved_shows, expiry_text=expiry_text, expiry_color=expiry_color, set_cover=set_cover, set_artist=set_artist)

@flask_app.route('/new_buyer/<userid>')
def new_buyer_page(userid):
    userid = int(userid)
    db.cursor.execute('SELECT first_name, username FROM users WHERE user_id = ?', (userid,))
    u_row = db.cursor.fetchone()
    real_name = u_row[0] if u_row else "Unknown"
    
    db.cursor.execute('SELECT value FROM settings WHERE key = ?', (f"buyer_name_{userid}",))
    c_row = db.cursor.fetchone()
    name = c_row[0] if c_row else ""
    
    username = u_row[1] if u_row and u_row[1] else ""
    
    expiry_text = "No active validity"
    expiry_color = "#666"
    
    return render_template('new_buyer.html', userid=str(userid), name=name, real_name=real_name, username=username, allowed_langs=[], has_all=False, extra_episode=False, saved_shows=[], expiry_text=expiry_text, expiry_color=expiry_color, set_cover=False, set_artist=False)

@flask_app.route('/show/<path:name>')
def show_page(name):
    # Get all users who have this show
    db.cursor.execute('SELECT user_id, username FROM subscriptions WHERE sub_data = ? AND sub_type = "selected_story"', (name,))
    subscribers = db.cursor.fetchall()
    
    filtered_buyers = {}
    for uid, uname in subscribers:
        if uid not in Config.OWNER_IDS:
            db.cursor.execute('SELECT first_name FROM users WHERE user_id = ?', (uid,))
            u_row = db.cursor.fetchone()
            u_name = u_row[0] if u_row else "Unknown"
            
            filtered_buyers[str(uid)] = {
                "name": u_name,
                "username": uname or ""
            }
            
    filtered_buyers = dict(sorted(filtered_buyers.items(), key=lambda item: item[1].get("name", "").lower()))
    return render_template('show_users.html', show_name=name, buyers=filtered_buyers)

@flask_app.route('/api/fetch_show_name', methods=['GET'])
def api_fetch_show_name():
    show_id = request.args.get('show_id')
    if not show_id:
        return jsonify({"success": False})
        
    show_id = str(show_id).strip()
    if '/' in show_id:
        show_id = [p for p in show_id.split('/') if p][-1]
    if '?' in show_id:
        show_id = show_id.split('?')[0]
        
    db.cursor.execute('SELECT title FROM stories WHERE show_id = ?', (show_id,))
    s_row = db.cursor.fetchone()
    if s_row:
        return jsonify({"success": True, "title": "Show Already Added"})
        
    return jsonify({"success": False, "error": "Show not found in database. Please use the bot to fetch it first."})

@flask_app.route('/api/custom_show_info', methods=['GET'])
def api_custom_show_info():
    url = request.args.get('url', '')
    import re
    show_id = None
    patterns = [
        r'/show/[^/]+/([a-f0-9]{32,})',
        r'/show/([a-f0-9]{32,})',
        r'/story/[^/]+/([a-f0-9]{32,})',
        r'/story/([a-f0-9]{32,})',
        r'pocketfm\.com/([a-f0-9]{32,})'
    ]
    for p in patterns:
        m = re.search(p, url)
        if m:
            show_id = m.group(1)
            break
            
    if not show_id and "pocketfm.onelink.me" in url:
        import requests
        try:
            r = requests.get(url, allow_redirects=True, timeout=5)
            for p in patterns:
                m = re.search(p, r.url)
                if m:
                    show_id = m.group(1)
                    break
        except:
            pass

    if not show_id:
        # Maybe it's just an ID
        if re.match(r'^[a-f0-9]{32,}$', url.strip()):
            show_id = url.strip()
        else:
            return jsonify({"success": False, "error": "Invalid URL"})
            
    db.cursor.execute('SELECT title FROM stories WHERE show_id = ?', (show_id,))
    s_row = db.cursor.fetchone()
    if s_row:
        return jsonify({"success": True, "title": s_row[0], "show_id": show_id})
    
    # Direct synchronous API call - same as bot's get_show_info but without async
    import requests as req_lib
    try:
        # Get auth token
        head_res = req_lib.head(Config.PFM_WEB_BASE, headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}, timeout=10)
        
        # Use cookies dict directly for reliability
        auth_token = head_res.cookies.get('auth-token', '')
        
        if not auth_token:
            cookie_str = head_res.headers.get("set-cookie", "")
            if cookie_str:
                for part in cookie_str.split(","):
                    if "auth-token" in part:
                        try:
                            auth_token = part.strip().split(";")[0].split("=", 1)[1]
                        except:
                            pass
                        break
        
        headers = {
            "version-name": "9.1.3",
            "platform-version": "29",
            "app-version": "2013",
            "authorization": f"Bearer {auth_token}"
        }
        
        api_url = f"{Config.PFM_API_BASE}/v2/content_api/show.get_details?show_id={show_id}&curr_ptr=0"
        res = req_lib.get(api_url, headers=headers, timeout=15)
        data = res.json()
        
        if data and data.get("status") == 1:
            res_list = data.get("result", [])
            if res_list:
                item = res_list[0]
                title = item.get("show_title")
                if title:
                    db.cursor.execute('INSERT OR REPLACE INTO stories (show_id, title, web_link) VALUES (?, ?, ?)', 
                                      (show_id, title, f"https://www.pocketfm.com/show/{show_id}"))
                    db.conn.commit()
                    return jsonify({"success": True, "title": title, "show_id": show_id})
        return jsonify({"success": False, "error": "Show details not found.", "raw_response": data})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)})
        
    return jsonify({"success": False, "error": "Show not found"})

@flask_app.route('/api/shows', methods=['GET'])
def api_get_shows():
    db.cursor.execute('SELECT show_id, title FROM stories')
    all_shows = db.cursor.fetchall()
    
    sanitized = {}
    for sid, title in all_shows:
        db.cursor.execute('SELECT COUNT(user_id) FROM subscriptions WHERE sub_type = "selected_story" AND sub_data = ?', (title,))
        c = db.cursor.fetchone()[0]
        sanitized[title] = {"allowed_count": c}
        
    return jsonify(sanitized)

@flask_app.route('/api/shows', methods=['POST'])
def api_add_show():
    # We no longer add shows via dashboard, they are added via bot automatically
    return jsonify({"success": False, "error": "Add shows by sending link to bot"})

@flask_app.route('/api/shows/<path:name>', methods=['DELETE'])
def api_delete_show(name):
    # Find show_id by title
    db.cursor.execute('SELECT show_id FROM stories WHERE title = ?', (name,))
    row = db.cursor.fetchone()
    if row:
        show_id = row[0]
        db.cursor.execute('DELETE FROM stories WHERE show_id = ?', (show_id,))
        db.cursor.execute('DELETE FROM user_saves WHERE show_id = ?', (show_id,))
        db.conn.commit()
        
    # Delete from subscriptions
    db.cursor.execute('DELETE FROM subscriptions WHERE sub_type = "selected_story" AND sub_data = ?', (name,))
    db.conn.commit()
    return jsonify({"success": True})

@flask_app.route('/api/shows/<path:name>/users', methods=['POST'])
def api_update_show_users(name):
    allowed_users = request.json or []
    allowed_users = [int(u) for u in allowed_users]
    
    # First remove all for this show
    db.cursor.execute('DELETE FROM subscriptions WHERE sub_type = "selected_story" AND sub_data = ?', (name,))
    
    # Add back the provided ones
    for uid in allowed_users:
        db.cursor.execute('SELECT username FROM users WHERE user_id = ?', (uid,))
        urow = db.cursor.fetchone()
        uname = urow[0] if urow else ""
        db.add_subscription(uid, uname, "selected_story", name, None, False)
        
    return jsonify({"success": True})

@flask_app.route('/api/buyers', methods=['GET'])
def api_get_buyers():
    # Buyers are users who have any subscriptions
    db.cursor.execute('SELECT DISTINCT user_id FROM subscriptions')
    subscribers = [r[0] for r in db.cursor.fetchall() if r[0] not in Config.OWNER_IDS]
    
    buyers = {}
    from datetime import datetime, timedelta, timezone
    ist = timezone(timedelta(hours=5, minutes=30))
    now = datetime.now(ist)
    
    for uid in subscribers:
        db.cursor.execute('SELECT first_name, username, joined_at FROM users WHERE user_id = ?', (uid,))
        urow = db.cursor.fetchone()
        
        db.cursor.execute('SELECT sub_data FROM subscriptions WHERE user_id = ? AND sub_type = "selected_story"', (uid,))
        shows = [r[0] for r in db.cursor.fetchall()]
        
        db.cursor.execute('SELECT value FROM settings WHERE key = ?', (f"buyer_name_{uid}",))
        c_row = db.cursor.fetchone()
        buyer_name = c_row[0] if c_row else (urow[0] if urow else "Unknown")
        joined_at = urow[2] if urow else ""
        
        db.cursor.execute('SELECT expiry FROM subscriptions WHERE user_id = ? AND sub_type IN ("validity", "all", "language", "custom_story", "extra_episode") LIMIT 1', (uid,))
        exp_row = db.cursor.fetchone()
        is_expired = False
        if exp_row and exp_row[0]:
            try:
                if datetime.fromisoformat(exp_row[0]) < now:
                    is_expired = True
            except: pass
            
        buyers[str(uid)] = {
            "name": buyer_name,
            "username": urow[1] if urow else "",
            "joined_at": joined_at,
            "allowed_shows": shows,
            "is_expired": is_expired,
            "status": "active",
            "avatar_v": get_avatar_v(uid)
        }
    return jsonify(buyers)

@flask_app.route('/api/buyers/<userid>/shows', methods=['POST'])
def api_update_buyer_shows(userid):
    userid = int(userid)
    allowed_shows = request.json
    if not isinstance(allowed_shows, list):
        return jsonify({"success": False})
        
    db.cursor.execute('DELETE FROM subscriptions WHERE user_id = ? AND sub_type = "selected_story"', (userid,))
    
    db.cursor.execute('SELECT username FROM users WHERE user_id = ?', (userid,))
    urow = db.cursor.fetchone()
    uname = urow[0] if urow else ""
    
    for show in allowed_shows:
        db.add_subscription(userid, uname, "selected_story", show, None, False)
        
    return jsonify({"success": True, "shows": allowed_shows})

@flask_app.route('/api/buyers/<userid>/update_all', methods=['POST'])
def api_update_buyer_all(userid):
    userid = int(userid)
    data = request.json
    if not isinstance(data, dict):
        return jsonify({"success": False})
        
    langs = data.get("langs", [])
    has_all = data.get("has_all", False)
    extra_episode = data.get("extra_episode", False)
    custom_story = data.get("custom_story", False)
    custom_name = data.get("name", "").strip()
    
    if custom_name:
        db.set_setting(f"buyer_name_{userid}", custom_name)
    else:
        db.cursor.execute('DELETE FROM settings WHERE key = ?', (f"buyer_name_{userid}",))
        db.conn.commit()

    set_cover = data.get("set_cover", False)
    if set_cover:
        db.set_setting(f"set_cover_{userid}", "1")
    else:
        db.cursor.execute('DELETE FROM settings WHERE key = ?', (f"set_cover_{userid}",))
        db.conn.commit()
        
    set_artist = data.get("set_artist", False)
    if set_artist:
        db.set_setting(f"set_artist_{userid}", "1")
    else:
        db.cursor.execute('DELETE FROM settings WHERE key = ?', (f"set_artist_{userid}",))
        db.conn.commit()
        
    saved_shows = data.get("saved_shows", None)
    if saved_shows is not None:
        if saved_shows:
            placeholders = ','.join(['?']*len(saved_shows))
            db.cursor.execute(f'DELETE FROM user_saves WHERE user_id = ? AND show_id NOT IN ({placeholders})', [userid] + saved_shows)
        else:
            db.cursor.execute('DELETE FROM user_saves WHERE user_id = ?', (userid,))
        db.conn.commit()
    
    db.cursor.execute('SELECT expiry FROM subscriptions WHERE user_id = ? AND sub_type IN ("validity", "all", "language", "custom_story", "extra_episode") LIMIT 1', (userid,))
    old_exp = db.cursor.fetchone()
    current_expiry = old_exp[0] if old_exp else None

    db.cursor.execute('DELETE FROM subscriptions WHERE user_id = ? AND sub_type IN ("language", "all", "validity", "extra_episode", "custom_story")', (userid,))
    
    db.cursor.execute('SELECT username FROM users WHERE user_id = ?', (userid,))
    urow = db.cursor.fetchone()
    uname = urow[0] if urow else ""
    
    duration = data.get("duration", "").strip()
    if duration:
        if duration == "Lifetime":
            expiry = None
        else:
            from datetime import datetime, timedelta, timezone
            ist = timezone(timedelta(hours=5, minutes=30))
            now = datetime.now(ist)
            if duration == "1 Hour":
                expiry = (now + timedelta(hours=1)).isoformat()
            elif duration == "1 Day":
                expiry = (now + timedelta(days=1)).isoformat()
            elif duration == "1 Week":
                expiry = (now + timedelta(weeks=1)).isoformat()
            elif duration == "1 Month":
                expiry = (now + timedelta(days=30)).isoformat()
            elif duration == "1 Year":
                expiry = (now + timedelta(days=365)).isoformat()
            elif duration.endswith(" Days"):
                try:
                    num_days = int(duration.replace(" Days", ""))
                    expiry = (now + timedelta(days=num_days)).isoformat()
                except ValueError:
                    expiry = current_expiry
            else:
                expiry = current_expiry
    else:
        expiry = current_expiry
    
    if has_all:
        db.add_subscription(userid, uname, "all", "", expiry, False)
    if extra_episode:
        db.add_subscription(userid, uname, "extra_episode", "", expiry, False)
    if custom_story:
        db.add_subscription(userid, uname, "custom_story", "", expiry, False)
    for lang in langs:
        db.add_subscription(userid, uname, "language", lang, expiry, False)
    db.add_subscription(userid, uname, "validity", "", expiry, False)
        
    expiry_text = "Lifetime validity"
    expiry_color = "#2b8a3e"
    
    if expiry:
        from datetime import datetime, timezone, timedelta
        ist = timezone(timedelta(hours=5, minutes=30))
        now = datetime.now(ist)
        try:
            exp_dt = datetime.fromisoformat(expiry)
            if exp_dt.tzinfo is None:
                exp_dt = exp_dt.replace(tzinfo=ist)
            
            fmt = exp_dt.strftime("%I:%M %p %d/%m/%Y")
            if fmt.startswith("0"):
                fmt = fmt[1:]
            
            # The dashboard expects uppercase PM/AM for standard look
            if exp_dt > now:
                expiry_text = f"Validity until {fmt}"
                expiry_color = "#2b8a3e"
            else:
                expiry_text = f"Validity expired on {fmt}"
                expiry_color = "#fa5252"
        except:
            pass

    return jsonify({"success": True, "expiry_text": expiry_text, "expiry_color": expiry_color})

@flask_app.route('/api/buyers/<userid>/custom_rules', methods=['GET'])
def get_custom_rules(userid):
    userid = int(userid)
    db.cursor.execute('SELECT 1 FROM subscriptions WHERE user_id = ? AND sub_type = "extra_episode"', (userid,))
    global_extra_ep = bool(db.cursor.fetchone())

    db.cursor.execute('''
        SELECT sub.sub_data, s.title, sub.sub_type 
        FROM subscriptions sub 
        LEFT JOIN stories s ON sub.sub_data = s.show_id 
        WHERE sub.user_id = ? AND sub.sub_type IN ('selected_story', 'blocked_story', 'extra_ep_story', 'extra_ep_remove_story')
    ''', (userid,))
    shows = {}
    for r in db.cursor.fetchall():
        show_id = r[0]
        title = r[1] or str(show_id)
        sub_type = r[2]
        if show_id not in shows:
            shows[show_id] = {"show_id": show_id, "title": title, "type": "allow", "extra_ep": global_extra_ep}
        if sub_type == "blocked_story":
            shows[show_id]["type"] = "block"
        elif sub_type == "selected_story":
            shows[show_id]["type"] = "allow"
        elif sub_type == "extra_ep_story":
            shows[show_id]["extra_ep"] = True
        elif sub_type == "extra_ep_remove_story":
            shows[show_id]["extra_ep"] = False
    
    rules = list(shows.values())
    rules.sort(key=lambda x: str(x['title']).lower())
    return jsonify(rules)

@flask_app.route('/api/buyers/<userid>/custom_rules', methods=['POST'])
def manage_custom_rule(userid):
    userid = int(userid)
    data = request.json
    show_id = data.get("show_id")
    action = data.get("action")
    if not show_id or action not in ["allow", "block", "remove", "set_extra_ep"]:
        return jsonify({"success": False})
        
    db.cursor.execute('SELECT username FROM users WHERE user_id = ?', (userid,))
    urow = db.cursor.fetchone()
    uname = urow[0] if urow else ""
        
    if action in ["allow", "block", "remove"]:
        db.cursor.execute('DELETE FROM subscriptions WHERE user_id = ? AND sub_type IN ("selected_story", "blocked_story") AND sub_data = ?', (userid, show_id))
        if action in ["allow", "block"]:
            sub_type = "selected_story" if action == "allow" else "blocked_story"
            db.add_subscription(userid, uname, sub_type, show_id, None, False)
        
        # If removing rule entirely, also remove extra_ep overrides
        if action == "remove":
            db.cursor.execute('DELETE FROM subscriptions WHERE user_id = ? AND sub_type IN ("extra_ep_story", "extra_ep_remove_story") AND sub_data = ?', (userid, show_id))
            
    elif action == "set_extra_ep":
        extra_ep = data.get("extra_ep", False)
        db.cursor.execute('SELECT 1 FROM subscriptions WHERE user_id = ? AND sub_type = "extra_episode"', (userid,))
        global_extra_ep = bool(db.cursor.fetchone())
        
        db.cursor.execute('DELETE FROM subscriptions WHERE user_id = ? AND sub_type IN ("extra_ep_story", "extra_ep_remove_story") AND sub_data = ?', (userid, show_id))
        
        if extra_ep and not global_extra_ep:
            db.add_subscription(userid, uname, "extra_ep_story", show_id, None, False)
        elif not extra_ep and global_extra_ep:
            db.add_subscription(userid, uname, "extra_ep_remove_story", show_id, None, False)
        
    db.conn.commit()
    return jsonify({"success": True})

@flask_app.route('/api/buyers/<userid>/toggle', methods=['POST'])
def api_toggle_buyer(userid):
    return jsonify({"success": True}) # Stub since status is derived from expiry/sub_type

@flask_app.route('/api/buyers/<userid>/add_show', methods=['POST'])
def api_buyer_add_show(userid):
    return jsonify({"success": False, "error": "Use bot to add shows"})

@flask_app.route('/api/buyers/<userid>', methods=['DELETE'])
def api_delete_buyer(userid):
    userid = int(userid)
    db.remove_subscription(userid)
    return jsonify({"success": True})

@flask_app.route('/api/buyers/<userid>/info', methods=['GET'])
def get_buyer_info(userid):
    userid = int(userid)
    db.cursor.execute('SELECT first_name, username FROM users WHERE user_id = ?', (userid,))
    u_row = db.cursor.fetchone()
    
    db.cursor.execute('SELECT value FROM settings WHERE key = ?', (f"buyer_name_{userid}",))
    c_row = db.cursor.fetchone()
    name = c_row[0] if c_row else ""
    
    db.cursor.execute('SELECT sub_data FROM subscriptions WHERE user_id = ? AND sub_type = "language"', (userid,))
    allowed_langs = [r[0] for r in db.cursor.fetchall()]
    
    db.cursor.execute('SELECT 1 FROM subscriptions WHERE user_id = ? AND sub_type = "all"', (userid,))
    has_all = bool(db.cursor.fetchone())
    
    db.cursor.execute('SELECT 1 FROM subscriptions WHERE user_id = ? AND sub_type = "extra_episode"', (userid,))
    extra_episode = bool(db.cursor.fetchone())
    
    db.cursor.execute('SELECT 1 FROM subscriptions WHERE user_id = ? AND sub_type = "custom_story"', (userid,))
    custom_story = bool(db.cursor.fetchone())
    
    if not allowed_langs and not has_all:
        custom_story = True
    
    db.cursor.execute('SELECT value FROM settings WHERE key = ?', (f"set_cover_{userid}",))
    c_row = db.cursor.fetchone()
    set_cover = (c_row[0] == "1") if c_row else False
    
    db.cursor.execute('SELECT value FROM settings WHERE key = ?', (f"set_artist_{userid}",))
    c_row = db.cursor.fetchone()
    set_artist = (c_row[0] == "1") if c_row else False
    
    db.cursor.execute('SELECT expiry FROM subscriptions WHERE user_id = ? AND sub_type IN ("validity", "all", "language", "custom_story", "extra_episode") LIMIT 1', (userid,))
    exp_row = db.cursor.fetchone()
    
    expiry_text = "No active validity"
    expiry_color = "#666"
    
    db.cursor.execute('SELECT 1 FROM subscriptions WHERE user_id = ? LIMIT 1', (userid,))
    if db.cursor.fetchone():
        if exp_row and exp_row[0]:
            from datetime import datetime, timezone, timedelta
            try:
                ist = timezone(timedelta(hours=5, minutes=30))
                now = datetime.now(ist)
                exp_dt = datetime.fromisoformat(exp_row[0])
                if exp_dt.tzinfo is None:
                    exp_dt = exp_dt.replace(tzinfo=ist)
                
                fmt = exp_dt.strftime("%I:%M %p %d/%m/%Y")
                if fmt.startswith("0"):
                    fmt = fmt[1:]
                
                if exp_dt > now:
                    expiry_text = f"Validity until {fmt}"
                    expiry_color = "#2b8a3e"
                else:
                    expiry_text = f"Validity expired on {fmt}"
                    expiry_color = "#fa5252"
            except:
                expiry_text = "Lifetime validity"
                expiry_color = "#2b8a3e"
        else:
            expiry_text = "Lifetime validity"
            expiry_color = "#2b8a3e"
            
    return jsonify({
        "name": name,
        "allowed_langs": allowed_langs,
        "has_all": has_all,
        "extra_episode": extra_episode,
        "custom_story": custom_story,
        "set_cover": set_cover,
        "set_artist": set_artist,
        "expiry_text": expiry_text,
        "expiry_color": expiry_color
    })

@flask_app.route('/api/buyers/<userid>/saved_shows', methods=['GET'])
def get_buyer_saved_shows(userid):
    userid = int(userid)
    db.cursor.execute('''
        SELECT us.show_id, s.title 
        FROM user_saves us 
        LEFT JOIN stories s ON us.show_id = s.show_id 
        WHERE us.user_id = ?
        ORDER BY s.title ASC
    ''', (userid,))
    shows = [{"id": r[0], "title": r[1] or r[0]} for r in db.cursor.fetchall()]
    
    # Sort in python as well to handle missing titles using show_id
    shows.sort(key=lambda x: str(x['title']).lower())
    return jsonify(shows)

@flask_app.route('/api/groups', methods=['GET'])
def api_get_groups():
    groups_data = db.get_all_buyer_groups()
    result = {}
    from datetime import datetime, timedelta, timezone
    ist = timezone(timedelta(hours=5, minutes=30))
    now = datetime.now(ist)
    
    for cid, data in groups_data.items():
        buyers = []
        for uid in data["buyers"]:
            db.cursor.execute('SELECT first_name, username FROM users WHERE user_id = ?', (uid,))
            urow = db.cursor.fetchone()
            
            db.cursor.execute('SELECT value FROM settings WHERE key = ?', (f"buyer_name_{uid}",))
            crow = db.cursor.fetchone()
            
            name = crow[0] if crow else (urow[0] if urow else "Unknown")
            username = urow[1] if urow else ""
            
            db.cursor.execute('SELECT 1 FROM subscriptions WHERE user_id = ? LIMIT 1', (uid,))
            is_buyer = bool(db.cursor.fetchone())

            db.cursor.execute('SELECT expiry FROM subscriptions WHERE user_id = ? AND sub_type IN ("validity", "all", "language", "custom_story", "extra_episode") LIMIT 1', (uid,))
            exp_row = db.cursor.fetchone()
            is_expired = False
            if exp_row and exp_row[0]:
                try:
                    if datetime.fromisoformat(exp_row[0]) < now:
                        is_expired = True
                except: pass
                
            buyers.append({"user_id": uid, "name": name, "username": username, "is_expired": is_expired, "is_buyer": is_buyer, "avatar_v": get_avatar_v(uid)})
            
        result[str(cid)] = {
            "title": data["title"],
            "username": data["username"],
            "buyers": buyers,
            "avatar_v": get_avatar_v(cid)
        }
    return jsonify(result)

@flask_app.route('/api/users', methods=['GET'])
def api_get_users():
    db.cursor.execute('SELECT user_id, first_name, username, joined_at FROM users')
    all_users = db.cursor.fetchall()
    
    filtered_users = {}
    for uid, name, uname, joined_at in all_users:
        if uid not in Config.OWNER_IDS:
            filtered_users[str(uid)] = {
                "name": name or "Unknown",
                "username": uname,
                "joined_at": joined_at,
                "avatar_v": get_avatar_v(uid)
            }
            
    return jsonify(filtered_users)

@flask_app.route('/api/users/<userid>', methods=['DELETE'])
def api_delete_user(userid):
    userid = int(userid)
    db.cursor.execute('DELETE FROM users WHERE user_id = ?', (userid,))
    db.remove_subscription(userid)
    db.conn.commit()
    
    avatar_path = os.path.join(AVATARS_DIR, f"{userid}.jpg")
    if os.path.exists(avatar_path):
        os.remove(avatar_path)
    return jsonify({"success": True})

@flask_app.route('/api/avatars/<uid>')
def api_get_avatar(uid):
    avatar_path = os.path.join(AVATARS_DIR, f"{uid}.jpg")
    if os.path.exists(avatar_path):
        return send_file(avatar_path, mimetype='image/jpeg')
    return "", 404

def start_flask(port):
    flask_app.run(host='0.0.0.0', port=port, debug=False, use_reloader=False)
