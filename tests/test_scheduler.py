import pytest
from fastapi.testclient import TestClient

import app
import db
import scheduler
import services


@pytest.fixture
def accounts():
    first = db.store_login({"access_token": "a", "refresh_token": "r1"})
    second = db.store_login({"access_token": "b", "refresh_token": "r2"})
    db.update_account(second, label="Żona")
    return first, second


def test_sync_job_runs_every_account_and_records_partial_failure(accounts, monkeypatch):
    first, second = accounts

    def fake_sync(account_id):
        if account_id == second:
            raise RuntimeError("invalid_grant")
        return 3

    monkeypatch.setattr(services, "sync_account", fake_sync)

    assert scheduler.run_job("sync_receipts") is True

    run = db.last_run("sync_receipts")
    assert run["status"] == "error"
    assert run["trigger"] == "cron"
    assert "Konto 1: 3 nowych" in run["message"]
    assert "Żona: invalid_grant" in run["message"]
    assert run["finished_at"]


def test_job_does_not_overlap_itself(accounts):
    scheduler._running["refresh_tokens"].acquire()
    try:
        assert scheduler.run_job("refresh_tokens") is False
        assert scheduler.run_now("refresh_tokens") is False
    finally:
        scheduler._running["refresh_tokens"].release()
    assert db.last_run("refresh_tokens") is None


def test_unfinished_runs_are_closed_on_startup():
    run_id = db.start_run("sync_receipts", "cron")
    db.abandon_unfinished_runs()
    run = db.recent_runs()[0]
    assert run["id"] == run_id and run["status"] == "error"


def test_jobs_page_and_run_now(accounts, monkeypatch):
    calls = []
    monkeypatch.setattr(scheduler, "run_now", lambda job_id: calls.append(job_id) or True)
    client = TestClient(app.app)

    assert client.get("/jobs").status_code == 200
    response = client.post("/jobs/refresh_tokens/run", follow_redirects=False)
    assert response.status_code == 303
    assert calls == ["refresh_tokens"]

    client.post("/jobs/not-a-job/run")
    assert calls == ["refresh_tokens"]


def test_scheduler_exposes_next_run_times():
    scheduler.start()
    try:
        jobs = {j["id"]: j for j in scheduler.jobs_overview()}
        assert jobs["sync_receipts"]["next_run"]
        assert jobs["refresh_tokens"]["next_run"]
    finally:
        scheduler.shutdown()
