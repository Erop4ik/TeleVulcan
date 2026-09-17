import os
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv()


@dataclass(frozen=True)
class Config:
    bot_token: str = os.environ.get("BOT_TOKEN", "")
    public_url: str = os.environ.get("PUBLIC_URL", "http://localhost:8080").rstrip("/")
    web_port: int = int(os.environ.get("WEB_PORT", "8080"))
    secret_key: str = os.environ.get("SECRET_KEY", "")
    symbol: str = os.environ.get("VULCAN_SYMBOL", "poznan")
    poll_interval: int = int(os.environ.get("POLL_INTERVAL", "600"))
    db_path: str = os.environ.get("DB_PATH", "vulcanbot.sqlite3")
    link_ttl: int = 15 * 60


config = Config()
