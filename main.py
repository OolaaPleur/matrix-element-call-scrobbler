import asyncio
import logging
import signal
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

from bot import ScrobblerBot
from config import Config


def setup_logging():
    Path("logs").mkdir(exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)-8s %(name)s: %(message)s")
    root = logging.getLogger()
    root.setLevel(logging.INFO)

    fh = RotatingFileHandler("logs/scrobbler.log", maxBytes=10 * 1024 * 1024, backupCount=5)
    fh.setFormatter(fmt)
    fh.setLevel(logging.INFO)
    root.addHandler(fh)

    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    sh.setLevel(logging.INFO)
    root.addHandler(sh)

    # Quiet down noisy libraries
    logging.getLogger("nio").setLevel(logging.WARNING)
    logging.getLogger("aiohttp").setLevel(logging.WARNING)
    logging.getLogger("aiosqlite").setLevel(logging.WARNING)
    logging.getLogger("peewee").setLevel(logging.WARNING)


async def main():
    setup_logging()
    config = Config()
    bot = ScrobblerBot(config)
    # As PID 1 in a container, SIGTERM has no default action, so `docker stop`
    # would wait and then SIGKILL. Cancel the bot instead: ScrobblerBot.run
    # stops the sync loop and closes storage and the client.
    task = asyncio.current_task()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, task.cancel)
        except NotImplementedError:
            # Windows' event loop has no signal handlers; Ctrl+C still raises
            # KeyboardInterrupt, which ends up in the same cleanup.
            break
    try:
        await bot.run()
    except asyncio.CancelledError:
        pass
    logging.getLogger(__name__).info("Shut down")


if __name__ == "__main__":
    asyncio.run(main())
