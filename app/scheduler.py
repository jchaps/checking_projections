import logging

from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger

from app import db
from app.config import load_config, load_recurring
from app.sync import sync_all
from app.digest import build_and_send_digest

log = logging.getLogger(__name__)


def _run_sync():
    config = load_config()
    recurring = load_recurring()
    conn = db.get_db(config["data_dir"])
    try:
        log.info("Starting scheduled sync")
        sync_all(config, conn, recurring)
        log.info("Scheduled sync complete")
    except Exception:
        log.exception("Scheduled sync failed")
    finally:
        conn.close()


def _run_digest():
    config = load_config()
    recurring = load_recurring()
    conn = db.get_db(config["data_dir"])
    try:
        log.info("Starting Saturday digest: syncing first")
        sync_all(config, conn, recurring)
        log.info("Sync complete, building digest")
        build_and_send_digest(config, conn, recurring)
        log.info("Digest sent successfully")
    except Exception:
        log.exception("Digest job failed")
    finally:
        conn.close()


def start_scheduler():
    """Start the blocking scheduler with sync and digest jobs."""
    config = load_config()
    sched_cfg = config["schedule"]
    sync_cfg = sched_cfg["sync"]
    digest_cfg = sched_cfg["digest"]

    scheduler = BlockingScheduler()

    scheduler.add_job(
        _run_sync,
        trigger=CronTrigger(day_of_week=sync_cfg["days"], hour=sync_cfg["hour"], minute=sync_cfg.get("minute", 0)),
        id="sync",
        name="Plaid Sync",
        misfire_grace_time=3600,
    )

    scheduler.add_job(
        _run_digest,
        trigger=CronTrigger(day_of_week=digest_cfg["days"], hour=digest_cfg["hour"], minute=digest_cfg.get("minute", 0)),
        id="digest",
        name="Weekly Digest",
        misfire_grace_time=3600,
    )

    log.info("Scheduler started: sync %s at %d:%02d, digest %s at %d:%02d",
             sync_cfg["days"], sync_cfg["hour"], sync_cfg.get("minute", 0),
             digest_cfg["days"], digest_cfg["hour"], digest_cfg.get("minute", 0))
    scheduler.start()
