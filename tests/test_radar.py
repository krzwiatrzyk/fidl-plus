import importlib
import sys

from fastapi.testclient import TestClient

import db


def test_radar_is_off_by_default():
    import app
    client = TestClient(app.app)
    assert "/__radar" not in client.get("/").text
    assert client.get("/__radar/").status_code == 404


def test_radar_records_requests_and_sql_but_not_logins(tmp_path, monkeypatch):
    monkeypatch.setenv("RADAR_ENABLED", "1")
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    original = sys.modules.pop("app")
    try:
        radar_app = importlib.import_module("app")
        client = TestClient(radar_app.app)

        assert 'href="/__radar/"' in client.get("/accounts").text
        client.get("/login")
        assert client.get("/__radar/").status_code == 200
        assert (tmp_path / "radar.duckdb").exists()

        paths = {r["path"] for r in client.get("/__radar/api/requests").json()}
        assert "/accounts" in paths
        assert "/login" not in paths
        queries = client.get("/__radar/api/queries").json()
        assert any("FROM accounts" in q["sql"] for q in queries)
    finally:
        sys.modules["app"] = original
        db.init(str(tmp_path / "fidl.db"))
