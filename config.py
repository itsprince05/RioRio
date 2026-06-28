import os
from dotenv import load_dotenv

load_dotenv()

class Config:

    API_ID = int(os.getenv("API_ID", ""))
    API_HASH = os.getenv("API_HASH", "")
    BOT_TOKEN = os.getenv("BOT_TOKEN", "")

    ADMIN_GROUP = int(os.getenv("ADMIN_GROUP", ""))
    
    PFM_API_BASE = os.getenv("PFM_API_BASE", "https://api.pocketfm.com")
    PFM_WEB_BASE = os.getenv("PFM_WEB_BASE", "https://pocketfm.com")
    PFM_CLIENT_TS = int(os.getenv("PFM_CLIENT_TS", "1770000000"))
    PFM_DEVICE_ID = os.getenv("PFM_DEVICE_ID", "mobile-web")
    PFM_APP_VERSION = os.getenv("PFM_APP_VERSION", "1115")
    PFM_VERSION_NAME = os.getenv("PFM_VERSION_NAME", "9.1.3")
    PFM_USER_AGENT = os.getenv("PFM_USER_AGENT", "com.radio.pocketfm")
    PFM_PLATFORM = os.getenv("PFM_PLATFORM", "android")


    DOWNLOAD_DIR = os.getenv("DOWNLOAD_DIR", "downloads")
    os.makedirs(DOWNLOAD_DIR, exist_ok=True)
    
    AVATAR_DIR = os.getenv("AVATAR_DIR", "avatars")
    os.makedirs(AVATAR_DIR, exist_ok=True)

    WVD_FILE = os.getenv("WVD_FILE", "l3.wvd")

    THREAD_COUNT = int(os.getenv("THREAD_COUNT", "16"))

    MAX_DOWNLOAD_CONCURRENCY = int(os.getenv("MAX_DOWNLOAD_CONCURRENCY", "10"))
    MAX_UPLOAD_CONCURRENCY = int(os.getenv("MAX_UPLOAD_CONCURRENCY", "10"))

    MAX_EPISODES_LIMIT = int(os.getenv("MAX_EPISODES_LIMIT", "50"))

    OWNER_IDS = [
        int(x) for x in os.getenv("OWNER_IDS", "").replace(" ", ",").split(",") if x
    ]

    ALLOWED_USERS = [
        int(x) for x in os.getenv("ALLOWED_USERS", "").replace(" ", "").split(",") if x
    ]

    ALLOWED_GROUPS = [
        int(x) for x in os.getenv(
            "ALLOWED_GROUPS",
            ""
        ).replace(" ", "").split(",") if x
    ]

    USE_PROXY = os.getenv("USE_PROXY", "false").lower() == "true"
    PROXY = os.getenv("PROXY", "")
