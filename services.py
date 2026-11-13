"""Token refresh and receipt sync for a single account, shared by the UI and the scheduler."""
import threading
from collections import defaultdict
from datetime import UTC, datetime, timedelta

import db
from lidl_api import LidlPlusAPI

_locks: dict[int, threading.Lock] = defaultdict(threading.Lock)

# account id -> {"running", "count", "total", "error"} for the progress bar
progress: dict[int, dict] = {}


class SyncInProgress(Exception):
    pass


def refresh_account(account_id: int) -> LidlPlusAPI:
    account = db.get_account(account_id)
    if not account or not account.get("refresh_token"):
        db.update_account(account_id, last_refresh_ok=0, last_error="Brak tokenów - zaloguj się.")
        raise ValueError("Brak tokenów - zaloguj się.")

    api = LidlPlusAPI.from_tokens(account)
    try:
        api.refresh_access_token()
    except Exception as e:
        db.update_account(account_id, last_refresh_at=db.now(), last_refresh_ok=0, last_error=str(e))
        raise
    db.save_tokens(account_id, api.token_dict())
    db.update_account(account_id, last_refresh_at=db.now(), last_refresh_ok=1, last_error=None)
    return api


def _api_for(account_id: int) -> LidlPlusAPI:
    account = db.get_account(account_id)
    expires = account.get("access_expires_at") if account else None
    if expires and datetime.fromisoformat(expires) > datetime.now(UTC) + timedelta(minutes=5):
        return LidlPlusAPI.from_tokens(account)
    return refresh_account(account_id)


def sync_account(account_id: int) -> int:
    """Download receipts the account does not have yet. Returns how many were added."""
    lock = _locks[account_id]
    if not lock.acquire(blocking=False):
        raise SyncInProgress("Synchronizacja tego konta już trwa.")

    state = progress[account_id] = {"running": True, "count": 0, "total": 0, "error": None}
    try:
        api = _api_for(account_id)
        known = db.receipt_ids(account_id)
        tickets = api.get_all_tickets(max_pages=100, known_ids=known)
        new_tickets = [t for t in tickets if t.get("id") and t["id"] not in known]
        state["total"] = len(new_tickets)

        batch = []
        for ticket in new_tickets:
            try:
                batch.append(api.get_ticket_details(ticket["id"]))
            except Exception:
                batch.append(ticket)
            state["count"] += 1
            if len(batch) == 10:
                db.upsert_receipts(account_id, batch)
                batch = []
        db.upsert_receipts(account_id, batch)

        db.update_account(account_id, last_sync_at=db.now())
        return len(new_tickets)
    except Exception as e:
        state["error"] = str(e)
        raise
    finally:
        state["running"] = False
        lock.release()


# Lidl does not document it; the old login page assumed 30 days. A refresh
# that rotates the token resets refresh_obtained_at, so a healthy account
# never gets near this.
REFRESH_TOKEN_DAYS = 30


def account_status(account: dict) -> dict:
    """Traffic-light state of an account's credentials for the UI."""
    now = datetime.now(UTC)
    expires = account.get("access_expires_at")
    access_valid = bool(expires) and datetime.fromisoformat(expires) > now

    if not account.get("refresh_token"):
        return {"level": "error", "text": "Brak tokenów - zaloguj się", "access_valid": False}
    if account.get("last_refresh_ok") == 0:
        return {"level": "error", "text": "Wymaga ponownego logowania", "access_valid": access_valid}

    obtained = account.get("refresh_obtained_at")
    if obtained:
        age = now - datetime.fromisoformat(obtained)
        if age > timedelta(days=REFRESH_TOKEN_DAYS):
            return {"level": "error", "text": "Refresh token prawdopodobnie wygasł",
                    "access_valid": access_valid}
        if age > timedelta(days=REFRESH_TOKEN_DAYS - 5):
            return {"level": "warn", "text": "Refresh token wkrótce wygaśnie",
                    "access_valid": access_valid}
    return {"level": "ok", "text": "Zalogowano", "access_valid": access_valid}
