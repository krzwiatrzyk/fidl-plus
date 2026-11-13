import json

import pytest

import app
from lidl_api import LidlPlusAPI


class FakeAPI:
    def __init__(self, pages=None, fail_listing=False, fail_refresh=False):
        self.pages = pages or []
        self.fail_listing = fail_listing
        self.fail_refresh = fail_refresh
        self.detail_calls = []

    def refresh_access_token(self):
        if self.fail_refresh:
            raise RuntimeError("invalid_grant")

    def save_tokens(self, path):
        pass

    def get_all_tickets(self, max_pages=10, known_ids=None):
        if self.fail_listing:
            raise RuntimeError("503 Service Unavailable")
        return [t for page in self.pages for t in page]

    def get_ticket_details(self, tid):
        self.detail_calls.append(tid)
        return {"id": tid, "date": "2026-08-01T10:00:00", "itemsLine": []}


@pytest.fixture
def dataset(tmp_path, monkeypatch):
    path = tmp_path / "receipts.json"
    path.write_text(json.dumps([{"id": "OLD-1"}, {"id": "OLD-2"}]), encoding="utf-8")
    monkeypatch.setattr(app, "DATA_FILE", str(path))
    return path


def _ids(path):
    return {r["id"] for r in json.loads(path.read_text(encoding="utf-8"))}


def test_listing_failure_keeps_stored_receipts(dataset, monkeypatch):
    monkeypatch.setattr(app, "load_api", lambda: FakeAPI(fail_listing=True))
    app.run_sync()
    assert _ids(dataset) == {"OLD-1", "OLD-2"}
    assert app.sync_state["error"]


def test_refresh_failure_is_reported(dataset, monkeypatch):
    monkeypatch.setattr(app, "load_api", lambda: FakeAPI(fail_refresh=True))
    app.run_sync()
    assert "zaloguj" in app.sync_state["error"]
    assert _ids(dataset) == {"OLD-1", "OLD-2"}


def test_sync_adds_new_receipts_and_keeps_old_ones(dataset, monkeypatch):
    api = FakeAPI(pages=[[{"id": "NEW-1"}, {"id": "OLD-1"}]])
    monkeypatch.setattr(app, "load_api", lambda: api)
    app.run_sync()
    assert _ids(dataset) == {"OLD-1", "OLD-2", "NEW-1"}
    assert api.detail_calls == ["NEW-1"]
    assert app.sync_state["done"]


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
