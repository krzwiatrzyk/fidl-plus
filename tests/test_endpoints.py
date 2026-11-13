"""
HTTP endpoint tests.

These mostly cover what unit tests cannot: whether the Jinja2 templates
render without blowing up on realistic data.
"""
import json

import pytest
from fastapi.testclient import TestClient

import app
import db


@pytest.fixture
def client(api_receipt, html_receipt, tmp_path, monkeypatch):
    """HTTP client backed by a stubbed receipt dataset."""
    printed_receipt = {
        "id": "TEST-HTML-1",
        "date": "2026-07-30T12:00:00",
        "store": {"name": "Lidl Testowa 2"},
        "totalAmount": 202.69,
        "htmlPrintedReceipt": html_receipt,
    }

    account_id = db.store_login({"access_token": "a", "refresh_token": "r"})
    db.upsert_receipts(account_id, [api_receipt, printed_receipt])

    return TestClient(app.app)


@pytest.mark.parametrize("path", [
    "/",
    "/receipts",
    "/insights",
    "/top-products",
    "/products",
    "/products?q=kajzerka",
    "/products?q=nic-takiego",
    "/products?sort=spend&page=2",
    "/products?sort=bogus",
    "/product?name=Bu%C5%82ka%20kajzerka",
    "/product?name=",
    "/login",
    "/login?account_id=1",
    "/accounts",
    "/receipt/TEST-API-1",
    "/receipt/TEST-HTML-1",
    "/receipt/TEST-HTML-1/print",
])
def test_page_renders(client, path):
    response = client.get(path)
    assert response.status_code == 200, response.text[:400]


@pytest.mark.parametrize("path", [
    "/receipt/DOES-NOT-EXIST",
    "/receipt/DOES-NOT-EXIST/print",
    "/receipt/TEST-API-1/print",  # an API receipt has no printable version
])
def test_missing_resource_returns_404(client, path):
    assert client.get(path).status_code == 404


def test_printable_receipt_makes_no_external_requests(client):
    """
    The receipt number is rendered as text. Generating a barcode through an
    external API would leak purchase identifiers off the server.
    """
    body = client.get("/receipt/TEST-HTML-1/print").text
    assert "http://" not in body
    assert "https://" not in body


def test_receipt_details_show_promotional_price(client):
    body = client.get("/receipt/TEST-API-1").text
    assert "Cena promocyjna" in body
    assert "0.23 zł" in body


def test_login_page_without_novnc_hides_preview_button(client, monkeypatch):
    monkeypatch.setattr(app, "NOVNC_PORT", "")
    monkeypatch.setattr(app, "NOVNC_URL", "")
    assert "novncUrl" not in client.get("/login").text


def test_login_page_with_novnc_shows_preview_button(client, monkeypatch):
    monkeypatch.setattr(app, "NOVNC_PORT", "6080")
    assert ":6080/vnc.html" in client.get("/login").text


def test_amounts_render_when_api_returns_a_string(client, api_receipt):
    """
    REGRESSION: templates used "%.2f"|format(totalAmount), which crashed the
    page whenever the API returned the amount as a string.
    """
    api_receipt["totalAmount"] = "49,80"
    db.upsert_receipts(1, [api_receipt])

    for path in ("/", "/receipts", "/insights", "/receipt/TEST-API-1"):
        response = client.get(path)
        assert response.status_code == 200, f"{path}: {response.text[:300]}"
    assert "49.80 zł" in client.get("/receipt/TEST-API-1").text


def test_token_upload_accepts_a_valid_file(client):
    """Fallback login path: uploading a lidl_tokens.json produced elsewhere."""
    tokens = {"access_token": "abc", "refresh_token": "def"}
    response = client.post(
        "/login/token",
        files={"token_file": ("lidl_tokens.json", json.dumps(tokens), "application/json")},
    )

    assert response.status_code == 200
    assert [a["refresh_token"] for a in db.list_accounts()][-1] == "def"


@pytest.mark.parametrize("payload", ["not json at all", '{"foo": "bar"}'])
def test_token_upload_rejects_a_bad_file(client, payload):
    """A malformed file must not touch stored accounts."""
    before = db.list_accounts()

    response = client.post(
        "/login/token",
        files={"token_file": ("lidl_tokens.json", payload, "application/json")},
    )

    assert response.status_code == 200
    assert "Błąd" in response.text
    assert db.list_accounts() == before


@pytest.fixture
def two_accounts(client, api_receipt):
    second = db.store_login({"access_token": "b", "refresh_token": "s"})
    db.update_account(second, label="Żona")
    db.upsert_receipts(second, [{**api_receipt, "id": "SPOUSE-1", "store": {"name": "Lidl Żony"}}])
    return second


def test_account_filter_limits_receipts_and_is_remembered(client, two_accounts):
    everything = client.get("/receipts?account=all").text
    assert "Lidl Żony" in everything and "Lidl Testowa 1" in everything
    assert "Konto</th>" in everything

    only_spouse = client.get(f"/receipts?account={two_accounts}").text
    assert "Lidl Żony" in only_spouse and "Lidl Testowa 1" not in only_spouse

    assert "Lidl Testowa 1" not in client.get("/receipts").text


def test_account_pills_reload_the_current_page_for_that_account(client, two_accounts):
    body = client.get("/insights?account=all&page=3").text
    assert f'href="/insights?account={two_accounts}"' in body
    assert 'href="/insights?account=all" class="acct-pill active"' in body


def test_single_account_pill_links_to_accounts(client):
    body = client.get("/insights").text
    assert 'href="/accounts"\n           class="acct-pill' in body
    assert "Wszystkie</a>" not in body


def test_dashboard_shows_spend_per_account(client, two_accounts):
    body = client.get("/?account=all").text
    assert "Żona" in body
    assert "Wydatki" in body


def test_rename_and_delete_account(client, two_accounts):
    client.post(f"/accounts/{two_accounts}/rename", data={"label": "Anna"})
    assert db.get_account(two_accounts)["label"] == "Anna"
    assert "Anna" in client.get("/receipts?account=all").text

    client.post(f"/accounts/{two_accounts}/delete")
    assert db.get_account(two_accounts) is None
    assert "SPOUSE-1" not in client.get("/receipts?account=all").text


def test_login_status_reports_the_new_login(client, monkeypatch):
    db.update_account(1, refresh_obtained_at="2026-01-01T00:00:00+00:00")
    monkeypatch.setitem(app.login_state, "started_at", db.now())
    monkeypatch.setitem(app.login_state, "running", True)
    assert "Czekam" in client.get("/login/status").text

    db.store_login({"access_token": "x", "refresh_token": "fresh"})
    assert "Zalogowano" in client.get("/login/status").text


def test_search_redirects_to_products(client):
    response = client.get("/search?q=mleko", follow_redirects=False)
    assert response.status_code == 301
    assert response.headers["location"] == "/products?q=mleko"


def test_products_lists_everything_without_a_query(client):
    body = client.get("/products").text
    assert "Bułka kajzerka" in body and "Mleko" in body


def test_without_a_date_range_everything_is_shown(client):
    body = client.get("/receipts").text
    assert "Lidl Testowa 1" in body and "Lidl Testowa 2" in body
    assert "wszystkie dane" in body


def test_date_range_limits_every_page_and_follows_you(client):
    body = client.get("/receipts?date_from=2026-07-31&date_to=2026-07-31").text
    assert "Lidl Testowa 1" in body and "Lidl Testowa 2" not in body

    body = client.get("/").text
    assert "Lidl Testowa 2" not in body
    assert 'value="2026-07-31"' in body

    body = client.get("/receipts?date_from=&date_to=").text
    assert "Lidl Testowa 2" in body
    assert "Lidl Testowa 2" in client.get("/receipts").text


def test_date_range_ignores_garbage(client):
    body = client.get("/receipts?date_from=yesterday").text
    assert "Lidl Testowa 1" in body and "Lidl Testowa 2" in body


def test_date_filter_keeps_other_params(client):
    body = client.get("/products?q=kajzerka&sort=spend&page=2").text
    assert '<input type="hidden" name="q" value="kajzerka">' in body
    assert '<input type="hidden" name="sort" value="spend">' in body
    assert 'name="page"' not in body
