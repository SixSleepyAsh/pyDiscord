from dataclasses import dataclass
import os
from dotenv import load_dotenv

load_dotenv()

@dataclass(frozen=True)
class Settings:
    token: str
    guild_ids: tuple[int, ...] = ()  # optional for faster command sync

def load_settings() -> Settings:
    token = os.getenv("DISCORD_TOKEN", "")
    if not token:
        raise RuntimeError("DISCORD_TOKEN missing in environment or .env")
    guilds = tuple(int(x) for x in os.getenv("GUILD_IDS", "").split(",") if x.strip())
    return Settings(token=token, guild_ids=guilds)
