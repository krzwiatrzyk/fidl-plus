"""Background jobs: keep tokens alive and pull new receipts without clicking."""
import os
import threading
from collections import defaultdict

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

import db
import services

SYNC_CRON = os.getenv("SYNC_CRON", "0 6 * * *")
TOKEN_REFRESH_HOURS = float(os.getenv("TOKEN_REFRESH_HOURS", "6"))
ENABLED = os.getenv("SCHEDULER_ENABLED", "1") != "0"


def refresh_tokens() -> tuple[bool, str]:
    ok, lines = True, []
    for account in db.list_accounts():
        try:
            services.refresh_account(account["id"])
            lines.append(f"{account['label']}: OK")
        except Exception as e:
            ok = False
            lines.append(f"{account['label']}: {e}")
    return ok, "; ".join(lines) or "Brak kont"


def sync_receipts() -> tuple[bool, str]:
    ok, lines = True, []
    for account in db.list_accounts():
        try:
            lines.append(f"{account['label']}: {services.sync_account(account['id'])} nowych")
        except Exception as e:
            ok = False
            lines.append(f"{account['label']}: {e}")
    return ok, "; ".join(lines) or "Brak kont"


JOBS = {
    "sync_receipts": {
        "name": "Pobieranie nowych paragonów",
        "func": sync_receipts,
        "trigger": CronTrigger.from_crontab(SYNC_CRON),
        "schedule": f"cron: {SYNC_CRON}",
    },
    "refresh_tokens": {
        "name": "Odświeżanie tokenów",
        "func": refresh_tokens,
        "trigger": IntervalTrigger(hours=TOKEN_REFRESH_HOURS),
        "schedule": f"co {TOKEN_REFRESH_HOURS:g} godz.",
    },
}

_running: dict[str, threading.Lock] = defaultdict(threading.Lock)

# coalesce + grace time: a NAS that slept through a run catches up once on
# wake instead of firing every missed run.
scheduler = BackgroundScheduler(job_defaults={"coalesce": True, "max_instances": 1,
                                              "misfire_grace_time": 6 * 3600})


def run_job(job_id: str, trigger: str = "cron") -> bool:
    """Run a job and record it in job_runs. Returns False if it was already running."""
    lock = _running[job_id]
    if not lock.acquire(blocking=False):
        return False
    run_id = db.start_run(job_id, trigger)
    try:
        ok, message = JOBS[job_id]["func"]()
        db.finish_run(run_id, "ok" if ok else "error", message)
    except Exception as e:
        db.finish_run(run_id, "error", str(e))
    finally:
        lock.release()
    return True


def run_now(job_id: str) -> bool:
    if _running[job_id].locked():
        return False
    threading.Thread(target=run_job, args=(job_id, "manual"), daemon=True).start()
    return True


def is_running(job_id: str) -> bool:
    return _running[job_id].locked()


def start() -> None:
    db.abandon_unfinished_runs()
    for job_id, job in JOBS.items():
        scheduler.add_job(run_job, job["trigger"], args=[job_id], id=job_id,
                          name=job["name"], replace_existing=True)
    scheduler.start()


def shutdown() -> None:
    if scheduler.running:
        scheduler.shutdown(wait=False)


def jobs_overview() -> list[dict]:
    overview = []
    for job_id, job in JOBS.items():
        scheduled = scheduler.get_job(job_id) if scheduler.running else None
        next_run = scheduled.next_run_time if scheduled else None
        overview.append({
            "id": job_id,
            "name": job["name"],
            "schedule": job["schedule"],
            "running": is_running(job_id),
            "next_run": next_run.isoformat() if next_run else None,
            "last_run": db.last_run(job_id),
        })
    return overview
