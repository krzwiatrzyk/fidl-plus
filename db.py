"""SQLite storage for accounts, receipts and scheduler runs."""
import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import UTC, datetime

from lidl_api import decode_jwt

SCHEMA = """
CREATE TABLE IF NOT EXISTS accounts (
    id                  INTEGER PRIMARY KEY,
    label               TEXT NOT NULL,
    sub                 TEXT UNIQUE,
    email               TEXT,
    access_token        TEXT,
    refresh_token       TEXT,
    id_token            TEXT,
    device_id           TEXT,
    access_expires_at   TEXT,
    refresh_obtained_at TEXT,
    last_refresh_at     TEXT,
    last_refresh_ok     INTEGER,
    last_error          TEXT,
    last_sync_at        TEXT,
    created_at          TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS receipts (
    id           TEXT PRIMARY KEY,
    account_id   INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    date         TEXT,
    store_name   TEXT,
    total_amount TEXT,
    raw          TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS receipts_account_date ON receipts(account_id, date);
"""

_path: str | None = None
# Bumped on every receipt write so callers can cache parsed receipts. Only the
# web process writes receipts, so an in-process counter is enough.
receipts_version = 0


def now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def init(path: str) -> None:
    global _path, receipts_version
    _path = path
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with connect() as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript(SCHEMA)
    receipts_version += 1


@contextmanager
def connect():
    conn = sqlite3.connect(_path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        with conn:
            yield conn
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Accounts
# ---------------------------------------------------------------------------

def list_accounts() -> list[dict]:
    with connect() as conn:
        rows = conn.execute("""
            SELECT a.*, COUNT(r.id) AS receipt_count
            FROM accounts a LEFT JOIN receipts r ON r.account_id = a.id
            GROUP BY a.id ORDER BY a.id
        """).fetchall()
    return [dict(r) for r in rows]


def get_account(account_id: int) -> dict | None:
    with connect() as conn:
        row = conn.execute("SELECT * FROM accounts WHERE id = ?", (account_id,)).fetchone()
    return dict(row) if row else None


def update_account(account_id: int, **fields) -> None:
    if not fields:
        return
    columns = ", ".join(f"{name} = ?" for name in fields)
    with connect() as conn:
        conn.execute(f"UPDATE accounts SET {columns} WHERE id = ?", (*fields.values(), account_id))


def delete_account(account_id: int) -> None:
    global receipts_version
    with connect() as conn:
        conn.execute("DELETE FROM accounts WHERE id = ?", (account_id,))
    receipts_version += 1


def _token_fields(tokens: dict, previous: dict | None) -> dict:
    claims = decode_jwt(tokens.get("access_token")) or {}
    identity = decode_jwt(tokens.get("id_token")) or claims
    exp = claims.get("exp")
    fields = {
        "access_token": tokens.get("access_token"),
        "refresh_token": tokens.get("refresh_token"),
        "id_token": tokens.get("id_token") or (previous or {}).get("id_token"),
        "device_id": tokens.get("device_id") or (previous or {}).get("device_id"),
        "access_expires_at": datetime.fromtimestamp(exp, UTC).isoformat(timespec="seconds") if exp else None,
    }
    if identity.get("sub"):
        fields["sub"] = identity["sub"]
    if identity.get("email"):
        fields["email"] = identity["email"]
    if not previous or previous.get("refresh_token") != fields["refresh_token"]:
        fields["refresh_obtained_at"] = now()
    return fields


def save_tokens(account_id: int, tokens: dict) -> None:
    update_account(account_id, **_token_fields(tokens, get_account(account_id)))


def store_login(tokens: dict, account_id: int | None = None) -> int:
    """
    Save tokens from a fresh login and return the account id.

    Without an explicit account the Lidl user id (`sub`) decides: logging in
    to an account that is already known updates it instead of adding a copy.
    """
    fields = _token_fields(tokens, None)
    fields.update(last_refresh_at=now(), last_refresh_ok=1, last_error=None)

    if account_id is None and fields.get("sub"):
        with connect() as conn:
            row = conn.execute("SELECT id FROM accounts WHERE sub = ?", (fields["sub"],)).fetchone()
        account_id = row["id"] if row else None

    if account_id is not None:
        if fields.get("sub"):
            # The same Lidl account may already sit in another row, e.g. after
            # "log in again" was used with a different user than before.
            with connect() as conn:
                conn.execute("UPDATE accounts SET sub = NULL WHERE sub = ? AND id != ?",
                             (fields["sub"], account_id))
        update_account(account_id, **fields)
        return account_id

    with connect() as conn:
        count = conn.execute("SELECT COUNT(*) FROM accounts").fetchone()[0]
        label = fields.get("email") or f"Konto {count + 1}"
        columns = ["label", "created_at", *fields]
        cur = conn.execute(
            f"INSERT INTO accounts ({', '.join(columns)}) VALUES ({', '.join('?' * len(columns))})",
            (label, now(), *fields.values()),
        )
        return cur.lastrowid


# ---------------------------------------------------------------------------
# Receipts
# ---------------------------------------------------------------------------

def upsert_receipts(account_id: int, receipts: list[dict]) -> None:
    global receipts_version
    rows = [
        (
            r["id"],
            account_id,
            r.get("date"),
            (r.get("store") or {}).get("name"),
            str(r.get("totalAmount", "")),
            json.dumps(r, ensure_ascii=False),
        )
        for r in receipts if r.get("id")
    ]
    with connect() as conn:
        conn.executemany("""
            INSERT INTO receipts (id, account_id, date, store_name, total_amount, raw)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                account_id = excluded.account_id, date = excluded.date,
                store_name = excluded.store_name, total_amount = excluded.total_amount,
                raw = excluded.raw
        """, rows)
    receipts_version += 1


def receipt_ids(account_id: int) -> set[str]:
    with connect() as conn:
        rows = conn.execute("SELECT id FROM receipts WHERE account_id = ?", (account_id,)).fetchall()
    return {r["id"] for r in rows}


def all_receipts() -> list[dict]:
    """Every receipt as the original API dict, tagged with its account."""
    with connect() as conn:
        rows = conn.execute("""
            SELECT r.raw, r.account_id, a.label
            FROM receipts r JOIN accounts a ON a.id = r.account_id
        """).fetchall()
    receipts = []
    for row in rows:
        receipt = json.loads(row["raw"])
        receipt["_account_id"] = row["account_id"]
        receipt["_account_label"] = row["label"]
        receipts.append(receipt)
    return receipts


# ---------------------------------------------------------------------------
# Migration from the JSON files used before SQLite
# ---------------------------------------------------------------------------

def migrate_legacy(tokens_file: str, receipts_file: str) -> int | None:
    """
    Import lidl_tokens.json and receipts.json into an empty database.

    The files are renamed to *.migrated afterwards, never deleted, so a
    downgrade or a botched import can still be recovered by hand.
    """
    has_tokens = os.path.exists(tokens_file)
    has_receipts = os.path.exists(receipts_file)
    if not (has_tokens or has_receipts):
        return None
    with connect() as conn:
        if conn.execute("SELECT COUNT(*) FROM accounts").fetchone()[0]:
            return None

    tokens = {}
    if has_tokens:
        with open(tokens_file, encoding="utf-8") as f:
            tokens = json.load(f)
    account_id = store_login(tokens)
    update_account(account_id, last_refresh_at=None, last_refresh_ok=None)

    if has_receipts:
        with open(receipts_file, encoding="utf-8") as f:
            upsert_receipts(account_id, json.load(f))

    for path in (tokens_file, receipts_file):
        if os.path.exists(path):
            os.replace(path, f"{path}.migrated")
    return account_id
