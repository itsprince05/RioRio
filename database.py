import sqlite3
import logging
import time
from datetime import datetime

logger = logging.getLogger(__name__)

class Database:
    def __init__(self):
        import threading
        self.conn = sqlite3.connect("pocketfm.db", check_same_thread=False, timeout=30.0)
        self._local = threading.local()
        
        # Use a temporary cursor for initial table setup
        cursor = self.conn.cursor()
        cursor.execute('''CREATE TABLE IF NOT EXISTS stories (
            show_id TEXT PRIMARY KEY,
            title TEXT,
            web_link TEXT
        )''')
        cursor.execute('''CREATE TABLE IF NOT EXISTS user_saves (
            user_id INTEGER,
            show_id TEXT,
            PRIMARY KEY (user_id, show_id)
        )''')

        cursor.execute('''CREATE TABLE IF NOT EXISTS subscriptions (
            user_id INTEGER,
            username TEXT,
            sub_type TEXT DEFAULT 'all', -- 'all', 'selected_story', 'language'
            sub_data TEXT DEFAULT '',    -- show_id or language name
            expiry DATETIME,
            is_trial BOOLEAN DEFAULT 0,
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (user_id, sub_type, sub_data)
        )''')
        cursor.execute('''CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT
        )''')
        cursor.execute('''CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            first_name TEXT,
            joined_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )''')
        cursor.execute('''CREATE TABLE IF NOT EXISTS approved_groups (
            chat_id INTEGER PRIMARY KEY,
            added_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )''')
        cursor.execute('''CREATE TABLE IF NOT EXISTS cached_files (
            show_id TEXT,
            seq INTEGER,
            file_id TEXT,
            title TEXT,
            duration INTEGER,
            artist TEXT,
            PRIMARY KEY (show_id, seq)
        )''')
        cursor.execute('''CREATE TABLE IF NOT EXISTS buyer_groups (
            chat_id INTEGER,
            chat_title TEXT,
            chat_username TEXT,
            user_id INTEGER,
            last_used DATETIME DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (chat_id, user_id)
        )''')
        try:
            cursor.execute("ALTER TABLE buyer_groups ADD COLUMN first_seen DATETIME DEFAULT CURRENT_TIMESTAMP")
        except sqlite3.OperationalError:
            pass
            
        self.conn.commit()
        cursor.close()
        logger.info("SQLite Database initialized")

    @property
    def cursor(self):
        if not hasattr(self._local, "cursor") or self._local.cursor is None:
            self._local.cursor = self.conn.cursor()
        return self._local.cursor

    def approve_group(self, chat_id):
        self.cursor.execute('INSERT OR IGNORE INTO approved_groups (chat_id) VALUES (?)', (chat_id,))
        self.conn.commit()

    def disapprove_group(self, chat_id):
        self.cursor.execute('DELETE FROM approved_groups WHERE chat_id = ?', (chat_id,))
        self.conn.commit()

    def is_group_approved(self, chat_id):
        self.cursor.execute('SELECT 1 FROM approved_groups WHERE chat_id = ?', (chat_id,))
        return bool(self.cursor.fetchone())

    def get_setting(self, key, default=None):
        self.cursor.execute('SELECT value FROM settings WHERE key = ?', (key,))
        res = self.cursor.fetchone()
        return res[0] if res else default

    def set_setting(self, key, value):
        self.cursor.execute('INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)', (key, str(value)))
        self.conn.commit()

    def add_user(self, user_id, username, first_name):
        self.cursor.execute('SELECT 1 FROM users WHERE user_id = ?', (user_id,))
        if not self.cursor.fetchone():
            self.cursor.execute('INSERT INTO users (user_id, username, first_name) VALUES (?, ?, ?)', (user_id, username, first_name))
            self.conn.commit()
            return True # is new
        else:
            self.cursor.execute('UPDATE users SET username = ?, first_name = ? WHERE user_id = ?', (username, first_name, user_id))
            self.conn.commit()
            return False
        
    def get_user(self, user_id):
        self.cursor.execute('SELECT username, first_name FROM users WHERE user_id = ?', (user_id,))
        return self.cursor.fetchone()
        
    def get_user_stats(self):
        self.cursor.execute('SELECT COUNT(*) FROM users')
        total = self.cursor.fetchone()[0]
        
        today_start = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
        self.cursor.execute('SELECT COUNT(*) FROM users WHERE joined_at >= ?', (today_start,))
        today = self.cursor.fetchone()[0]
        
        from datetime import timedelta
        week_start = (datetime.now() - timedelta(days=datetime.now().weekday())).replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
        self.cursor.execute('SELECT COUNT(*) FROM users WHERE joined_at >= ?', (week_start,))
        week = self.cursor.fetchone()[0]
        
        return total, today, week

    def save_story(self, show_id, title, web_link):
        try:
            self.cursor.execute('''INSERT OR REPLACE INTO stories
                                   (show_id, title, web_link)
                                   VALUES (?, ?, ?)''', (show_id, title, web_link))
            self.conn.commit()
        except Exception as e:
            logger.error(f"DB Save Error: {e}")

    def search_stories(self, query):
        self.cursor.execute('''SELECT show_id, title FROM stories
                               WHERE title LIKE ? LIMIT 10''', (f"%{query}%",))
        return self.cursor.fetchall()

    def get_story_title(self, show_id):
        self.cursor.execute('''SELECT title FROM stories WHERE show_id = ?''', (show_id,))
        res = self.cursor.fetchone()
        return res[0] if res else "Unknown Story"

    def save_user_show(self, user_id, show_id):
        self.cursor.execute('''INSERT OR IGNORE INTO user_saves (user_id, show_id)
                               VALUES (?, ?)''', (user_id, show_id))
        self.conn.commit()

    def remove_user_show(self, user_id, show_id):
        self.cursor.execute('''DELETE FROM user_saves WHERE user_id = ? AND show_id = ?''', (user_id, show_id))
        self.conn.commit()

    def check_user_show(self, user_id, show_id):
        self.cursor.execute('''SELECT 1 FROM user_saves WHERE user_id = ? AND show_id = ?''', (user_id, show_id))
        return bool(self.cursor.fetchone())

    def get_user_shows(self, user_id):
        self.cursor.execute('''
            SELECT s.show_id, s.title 
            FROM user_saves u
            JOIN stories s ON u.show_id = s.show_id
            WHERE u.user_id = ? AND s.show_id NOT IN (
                SELECT sub_data FROM subscriptions WHERE user_id = ? AND sub_type = 'blocked_story'
            )
            UNION
            SELECT s.show_id, s.title
            FROM subscriptions sub
            JOIN stories s ON sub.sub_data = s.show_id
            WHERE sub.user_id = ? AND sub.sub_type = 'selected_story' AND s.show_id NOT IN (
                SELECT sub_data FROM subscriptions WHERE user_id = ? AND sub_type = 'blocked_story'
            )
            ORDER BY 2 ASC
        ''', (user_id, user_id, user_id, user_id))
        return self.cursor.fetchall()



    # Cache Management
    def get_cached_file(self, show_id, seq):
        self.cursor.execute('SELECT file_id, title, duration, artist FROM cached_files WHERE show_id = ? AND seq = ?', (show_id, seq))
        return self.cursor.fetchone()

    def add_cached_file(self, show_id, seq, file_id, title, duration, artist):
        self.cursor.execute('''INSERT OR REPLACE INTO cached_files (show_id, seq, file_id, title, duration, artist) 
                               VALUES (?, ?, ?, ?, ?, ?)''', (show_id, seq, file_id, title, duration, artist))
        self.conn.commit()

    # Subscription Management
    def add_subscription(self, user_id, username=None, sub_type='all', sub_data=None, expiry=None, is_trial=False):
        sub_data = sub_data or ""
        self.cursor.execute('''INSERT OR REPLACE INTO subscriptions 
                               (user_id, username, sub_type, sub_data, expiry, is_trial) 
                               VALUES (?, ?, ?, ?, ?, ?)''', 
                            (user_id, username, sub_type, sub_data, expiry, is_trial))
        self.cursor.execute('DELETE FROM settings WHERE key = ?', (f"notified_expired_{user_id}",))
        self.conn.commit()

    def remove_subscription(self, user_id, sub_type=None, sub_data=None):
        if sub_type and sub_data is not None:
            self.cursor.execute('DELETE FROM subscriptions WHERE user_id = ? AND sub_type = ? AND sub_data = ?', (user_id, sub_type, sub_data))
        elif sub_type:
             self.cursor.execute('DELETE FROM subscriptions WHERE user_id = ? AND sub_type = ?', (user_id, sub_type))
        else:
            self.cursor.execute('DELETE FROM subscriptions WHERE user_id = ?', (user_id,))
        self.conn.commit()

    def delete_user_data(self, user_id):
        self.cursor.execute('DELETE FROM users WHERE id = ?', (user_id,))
        self.cursor.execute('DELETE FROM subscriptions WHERE user_id = ?', (user_id,))
        self.cursor.execute('DELETE FROM user_saves WHERE user_id = ?', (user_id,))
        self.conn.commit()


    def get_all_subscriptions(self):
        self.cursor.execute('SELECT user_id, username, sub_type, sub_data, expiry, is_trial, timestamp FROM subscriptions')
        return self.cursor.fetchall()

    def get_subscription(self, user_id):
        self.cursor.execute('SELECT sub_type, sub_data, expiry, is_trial FROM subscriptions WHERE user_id = ?', (user_id,))
        return self.cursor.fetchone()
        
    def get_user_validity(self, user_id):
        self.cursor.execute("SELECT expiry FROM subscriptions WHERE user_id = ? AND sub_type IN ('validity', 'all', 'language', 'custom_story', 'extra_episode')", (user_id,))
        subs = self.cursor.fetchall()
        if not subs:
            return "No Active Subscription"
        
        best_expiry = None
        has_lifetime = False
        for (expiry,) in subs:
            if not expiry:
                has_lifetime = True
                break
            if not best_expiry or expiry > best_expiry:
                best_expiry = expiry
                
        if has_lifetime:
            return "Lifetime"
        return best_expiry

    def is_subscribed(self, user_id, show_id=None, language=None):
        from datetime import datetime
        self.cursor.execute('SELECT sub_type, sub_data, expiry FROM subscriptions WHERE user_id = ?', (user_id,))
        subs = self.cursor.fetchall()
        if not subs:
            return False
        
        from datetime import timezone, timedelta
        ist = timezone(timedelta(hours=5, minutes=30))
        now = datetime.now(ist)
        
        general_validity_active = False
        
        for sub_type, sub_data, expiry in subs:
            if sub_type in ('validity', 'all', 'language', 'custom_story', 'extra_episode'):
                if expiry:
                    try:
                        expiry_dt = datetime.fromisoformat(expiry)
                        if now <= expiry_dt:
                            general_validity_active = True
                            break
                    except:
                        general_validity_active = True
                        break
                else:
                    general_validity_active = True
                    break
                    
        if not general_validity_active:
            return False
            
        for sub_type, sub_data, expiry in subs:
            if sub_type == 'all':
                return True
            elif sub_type == 'selected_story':
                if show_id == sub_data:
                    return True
            elif sub_type == 'language':
                if language and language.lower() == sub_data.lower():
                    return True
        
        return False

    # JSON Backup Helpers
    def export_users(self):
        self.cursor.execute('SELECT user_id, username, first_name, joined_at FROM users')
        rows = self.cursor.fetchall()
        return [{"user_id": r[0], "username": r[1], "first_name": r[2], "joined_at": r[3]} for r in rows]

    def export_settings(self):
        self.cursor.execute('SELECT key, value FROM settings')
        rows = self.cursor.fetchall()
        return [{"key": r[0], "value": r[1]} for r in rows]

    def export_subscriptions(self):
        self.cursor.execute('SELECT user_id, username, sub_type, sub_data, expiry, is_trial, timestamp FROM subscriptions')
        rows = self.cursor.fetchall()
        return [{
            "user_id": r[0], "username": r[1], "sub_type": r[2], 
            "sub_data": r[3], "expiry": r[4], "is_trial": r[5], "timestamp": r[6]
        } for r in rows]

    def export_stories(self):
        self.cursor.execute('SELECT show_id, title, web_link FROM stories')
        rows = self.cursor.fetchall()
        return [{"show_id": r[0], "title": r[1], "web_link": r[2]} for r in rows]

    def import_users(self, data):
        for u in data:
            self.cursor.execute('INSERT OR REPLACE INTO users (user_id, username, first_name, joined_at) VALUES (?, ?, ?, ?)',
                               (u['user_id'], u.get('username'), u.get('first_name'), u.get('joined_at')))
        self.conn.commit()

    def import_settings(self, data):
        for s in data:
            self.cursor.execute('INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)',
                               (s['key'], s['value']))
        self.conn.commit()

    def import_subscriptions(self, data):
        for s in data:
            self.cursor.execute('''INSERT OR REPLACE INTO subscriptions 
                                   (user_id, username, sub_type, sub_data, expiry, is_trial, timestamp) 
                                   VALUES (?, ?, ?, ?, ?, ?, ?)''',
                               (s['user_id'], s.get('username'), s.get('sub_type'), s.get('sub_data'), 
                                s.get('expiry'), s.get('is_trial'), s.get('timestamp')))
        self.conn.commit()

    def import_stories(self, data):
        for s in data:
            self.cursor.execute('INSERT OR REPLACE INTO stories (show_id, title, web_link) VALUES (?, ?, ?)',
                               (s['show_id'], s['title'], s['web_link']))
        self.conn.commit()

    def update_buyer_group(self, chat_id, chat_title, chat_username, user_id):
        self.cursor.execute('''INSERT INTO buyer_groups (chat_id, chat_title, chat_username, user_id)
                               VALUES (?, ?, ?, ?)
                               ON CONFLICT(chat_id, user_id) DO UPDATE SET 
                               chat_title=excluded.chat_title, 
                               chat_username=excluded.chat_username''', 
                            (chat_id, chat_title, chat_username, user_id))
        self.conn.commit()

    def get_all_buyer_groups(self):
        self.cursor.execute('''SELECT chat_id, chat_title, chat_username, user_id, last_used FROM buyer_groups ORDER BY last_used DESC''')
        rows = self.cursor.fetchall()
        groups = {}
        for r in rows:
            cid, title, uname, uid, fseen = r
            if cid not in groups:
                groups[cid] = {"title": title, "username": uname, "buyers": [], "first_seen": fseen}
            groups[cid]["buyers"].append(uid)
            
        return groups

db = Database()
