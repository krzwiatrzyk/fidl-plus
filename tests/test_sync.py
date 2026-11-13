import pytest

import db
import services
from lidl_api import LidlPlusAPI


class FakeAPI:
    def __init__(self, pages=None, fail_listing=False, fail_refresh=False):
        self.pages = pages or []
        self.fail_listing = fail_listing
        self.fail_refresh = fail_refresh
        self.detail_calls = []
        self.access_token = "new-access"
        self.refresh_token = "new-refresh"

    def refresh_access_token(self):
        if self.fail_refresh:
            raise RuntimeError("400 invalid_grant")

    def token_dict(self):
        return {"access_token": self.access_token, "refresh_token": self.refresh_token}

    def get_all_tickets(self, max_pages=10, known_ids=None):
        if self.fail_listing:
            raise RuntimeError("503 Service Unavailable")
        return [t for page in self.pages for t in page]

    def get_ticket_details(self, tid):
        self.detail_calls.append(tid)
        return {"id": tid, "date": "2026-08-01T10:00:00", "itemsLine": []}


@pytest.fixture
def account():
    account_id = db.store_login({"access_token": "old-access", "refresh_token": "old-refresh"})
    db.upsert_receipts(account_id, [{"id": "OLD-1"}, {"id": "OLD-2"}])
    return account_id


def use_api(monkeypatch, api):
    monkeypatch.setattr(services.LidlPlusAPI, "from_tokens", classmethod(lambda cls, tokens: api))


def test_listing_failure_keeps_stored_receipts(account, monkeypatch):
    use_api(monkeypatch, FakeAPI(fail_listing=True))
    with pytest.raises(RuntimeError):
        services.sync_account(account)
    assert db.receipt_ids(account) == {"OLD-1", "OLD-2"}
    assert services.progress[account]["error"]


def test_refresh_failure_is_recorded_on_the_account(account, monkeypatch):
    use_api(monkeypatch, FakeAPI(fail_refresh=True))
    with pytest.raises(RuntimeError):
        services.sync_account(account)
    stored = db.get_account(account)
    assert stored["last_refresh_ok"] == 0
    assert "invalid_grant" in stored["last_error"]
    assert db.receipt_ids(account) == {"OLD-1", "OLD-2"}


def test_sync_adds_new_receipts_and_keeps_old_ones(account, monkeypatch):
    api = FakeAPI(pages=[[{"id": "NEW-1"}, {"id": "OLD-1"}]])
    use_api(monkeypatch, api)

    assert services.sync_account(account) == 1

    assert db.receipt_ids(account) == {"OLD-1", "OLD-2", "NEW-1"}
    assert api.detail_calls == ["NEW-1"]
    stored = db.get_account(account)
    assert stored["refresh_token"] == "new-refresh"
    assert stored["last_refresh_ok"] == 1
    assert stored["last_sync_at"]


def test_parallel_sync_of_the_same_account_is_refused(account):
    services._locks[account].acquire()
    try:
        with pytest.raises(services.SyncInProgress):
            services.sync_account(account)
    finally:
        services._locks[account].release()


def test_get_all_tickets_stops_at_a_fully_known_page(monkeypatch):
    pages = {
        1: {"tickets": [{"id": "N1"}, {"id": "K1"}], "totalCount": 6},
        2: {"tickets": [{"id": "K2"}, {"id": "K3"}], "totalCount": 6},
        3: {"tickets": [{"id": "K4"}, {"id": "K5"}], "totalCount": 6},
    }
    api = LidlPlusAPI()
    requested = []
    monkeypatch.setattr(api, "get_tickets", lambda page: requested.append(page) or pages[page])

    tickets = api.get_all_tickets(max_pages=10, known_ids={"K1", "K2", "K3", "K4", "K5"})

    assert requested == [1, 2]
    assert [t["id"] for t in tickets] == ["N1", "K1", "K2", "K3"]


def test_get_all_tickets_propagates_errors(monkeypatch):
    api = LidlPlusAPI()

    def boom(page):
        raise RuntimeError("timeout")

    monkeypatch.setattr(api, "get_tickets", boom)
    with pytest.raises(RuntimeError):
        api.get_all_tickets()
