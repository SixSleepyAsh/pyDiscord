import asyncio
from src.core.config import load_settings
from src.core.registry import Registry
from src.core.bot import LumiBot
import logging

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)

async def main() -> None:
    settings = load_settings()
    print(f" Token length: {len(settings.token)}")   # sanity check
    print(f" Guild IDs: {settings.guild_ids}")

    registry = Registry()
    bot = LumiBot(settings, registry)

    try:
        async with bot:
            print(" Starting bot...")
            await bot.start(settings.token)
    except Exception as e:
        print(" Bot crashed:", e)
        raise


if __name__ == "__main__":
    asyncio.run(main())
