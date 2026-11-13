import json

from conftest import make_jwt

import db


def test_login_to_a_known_lidl_account_updates_it():
    first = db.store_login({"access_token": make_jwt(sub="u1", email="a@x.pl", exp=2_000_000_000),
                            "refresh_token": "r1"})
    second = db.store_login({"access_token": make_jwt(sub="u1", exp=2_000_000_000), "refresh_token": "r2"})
    other = db.store_login({"access_token": make_jwt(sub="u2"), "refresh_token": "r3"})

    assert first == second != other
    accounts = {a["id"]: a for a in db.list_accounts()}
    assert accounts[first]["refresh_token"] == "r2"
    assert accounts[first]["label"] == "a@x.pl"
    assert accounts[first]["access_expires_at"].startswith("2033-05-18")
    assert accounts[other]["label"] == "Konto 2"


def test_upsert_does_not_duplicate_receipts():
    account_id = db.store_login({"refresh_token": "r"})
    db.upsert_receipts(account_id, [{"id": "A", "totalAmount": 1}])
    db.upsert_receipts(account_id, [{"id": "A", "totalAmount": 2}, {"id": "B"}])

    receipts = {r["id"]: r for r in db.all_receipts()}
    assert set(receipts) == {"A", "B"}
    assert receipts["A"]["totalAmount"] == 2
    assert receipts["A"]["_account_id"] == account_id


def test_legacy_files_are_imported_once(tmp_path):
    tokens = tmp_path / "lidl_tokens.json"
    receipts = tmp_path / "receipts.json"
    tokens.write_text(json.dumps({"access_token": "a", "refresh_token": "r"}), encoding="utf-8")
    receipts.write_text(json.dumps([{"id": "X"}, {"id": "Y"}]), encoding="utf-8")

    account_id = db.migrate_legacy(str(tokens), str(receipts))

    assert db.receipt_ids(account_id) == {"X", "Y"}
    assert db.get_account(account_id)["refresh_token"] == "r"
    assert db.get_account(account_id)["label"] == "Konto 1"
    assert not tokens.exists() and (tmp_path / "lidl_tokens.json.migrated").exists()
    assert db.migrate_legacy(str(tokens), str(receipts)) is None


def test_migration_skips_a_database_that_already_has_accounts(tmp_path):
    db.store_login({"refresh_token": "r"})
    receipts = tmp_path / "receipts.json"
    receipts.write_text("[]", encoding="utf-8")

    assert db.migrate_legacy(str(tmp_path / "none.json"), str(receipts)) is None
    assert receipts.exists()
