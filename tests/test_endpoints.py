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
    "/search?q=kajzerka",
    "/search?q=",
    "/product?name=Bu%C5%82ka%20kajzerka",
    "/product?name=",
    "/login",
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
