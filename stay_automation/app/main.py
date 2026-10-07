"""Entry point: web dashboard, periodic sync loops, and the stdin webhook reader."""
import asyncio
import logging
import sys
import threading
from contextlib import asynccontextmanager
from typing import Awaitable, Callable

import uvicorn

from .climate import ClimateManager
from .config import load_settings
from .db import DB
from .ha import HAClient
from .hostaway import HostawayClient
from .locks import LockManager
from .sync import Syncer
from .web import create_app

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("stay")

LOCK_TICK_MINUTES = 5
CLIMATE_TICK_MINUTES = 5
BATTERY_CHECK_MINUTES = 60


async def run_logged(name: str, job: Callable[[], Awaitable[object]], db: DB) -> None:
    try:
        await job()
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        log.exception("%s failed", name)
        db.log(f"{name}.failed", f"{name} failed: {exc}", level="error")


async def every(minutes: int, name: str, job: Callable[[], Awaitable[object]], db: DB) -> None:
    while True:
        await asyncio.sleep(minutes * 60)
        await run_logged(name, job, db)


def start_stdin_reader(loop: asyncio.AbstractEventLoop, queue: asyncio.Queue) -> None:
    def read() -> None:
        for line in sys.stdin:
            if line.strip():
                loop.call_soon_threadsafe(queue.put_nowait, line.strip())

    threading.Thread(target=read, name="stdin", daemon=True).start()


async def consume_webhooks(queue: asyncio.Queue, syncer: Syncer) -> None:
    while True:
        line = await queue.get()
        try:
            await syncer.handle_webhook(line)
        except Exception as exc:
            log.exception("webhook failed")
            syncer.db.log("webhook.failed", f"Webhook could not be processed: {exc}", level="error")


def build():
    settings = load_settings()
    db = DB(settings.db_path)
    if not db.one("SELECT 1 FROM events LIMIT 1"):
        db.log("db.fresh", "Started with an EMPTY database: lock automation, backup codes and staff all start "
                           "from scratch. If you expected existing settings, this is a new install or a second "
                           "copy of the add-on (check Home Assistant for two 'Stays' add-ons).", level="warning")
    hostaway = HostawayClient(
        settings.hostaway_account_id, settings.hostaway_api_key,
        load_token=lambda: db.get_setting("hostaway_token"),
        save_token=lambda token: db.set_setting("hostaway_token", token),
    )
    ha = HAClient(settings.ha_url, settings.ha_token)
    syncer = Syncer(db, settings, hostaway, ha)
    locks = LockManager(syncer)
    climate = ClimateManager(syncer, locks.alert)
    syncer.on_thermostats = climate.tick_property

    discover = _then(syncer.import_listings, syncer.discover_locks)

    async def startup() -> None:
        try:
            await syncer.fix_addon_slug()
        except Exception as exc:  # never block startup over the webhook wiring
            db.log("setup.webhook", f"Could not check the add-on slug: {exc}", level="warning")
        await discover()
        await syncer.sync_reservations()
        await climate.refresh()
        await climate.check_batteries()

    async def scheduler() -> None:
        # One chain so the startup sync finishes before the periodic loops begin.
        await run_logged("startup", startup, db)
        await asyncio.gather(
            every(settings.sync_minutes, "sync", syncer.sync_reservations, db),
            every(settings.lock_discovery_minutes, "lock_discovery", discover, db),
            every(LOCK_TICK_MINUTES, "lock_automation", locks.tick, db),
            every(CLIMATE_TICK_MINUTES, "thermostats", climate.tick, db),
            every(BATTERY_CHECK_MINUTES, "batteries", climate.check_batteries, db),
        )

    @asynccontextmanager
    async def lifespan(app):
        tasks = [asyncio.create_task(scheduler())]
        if settings.is_addon:
            queue: asyncio.Queue = asyncio.Queue()
            start_stdin_reader(asyncio.get_running_loop(), queue)
            tasks.append(asyncio.create_task(consume_webhooks(queue, syncer)))
        yield
        for task in tasks:
            task.cancel()
        await hostaway.close()
        await ha.close()

    return create_app(syncer, locks, lifespan=lifespan)


def _then(*jobs: Callable[[], Awaitable[object]]) -> Callable[[], Awaitable[None]]:
    async def run() -> None:
        for job in jobs:
            await job()
    return run


if __name__ == "__main__":
    uvicorn.run(build(), host="0.0.0.0", port=8099)
