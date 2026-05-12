import asyncio
import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

from bot import ScrobblerBot
from config import Config


def setup_logging():
    Path("logs").mkdir(exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)-8s %(name)s: %(message)s")
    root = logging.getLogger()
    root.setLevel(logging.DEBUG)

    fh = RotatingFileHandler("logs/scrobbler.log", maxBytes=10 * 1024 * 1024, backupCount=5)
    fh.setFormatter(fmt)
    fh.setLevel(logging.DEBUG)
    root.addHandler(fh)

    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    sh.setLevel(logging.INFO)
    root.addHandler(sh)

    # Quiet down noisy libraries
    logging.getLogger("nio").setLevel(logging.WARNING)
    logging.getLogger("aiohttp").setLevel(logging.WARNING)


async def main():
    setup_logging()
    config = Config()
    bot = ScrobblerBot(config)
    await bot.run()


if __name__ == "__main__":
    asyncio.run(main())
