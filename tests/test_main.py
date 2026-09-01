"""
Tests for app/main.py's HTTP surface. Only /batch/results has any real logic
yet -- everything else is a documented TODO stub. No real filesystem
dependency: each test chdir's into a fresh tmp_path and writes its own
data/results_{set}.json, since the endpoint resolves that path relative to
the process's working directory (same convention batch_scanner.py itself
uses for data/cases_{set}.json and data/results_{set}.json).
"""
from __future__ import annotations

import json

from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def test_batch_results_serves_the_snapshot_verbatim(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "data").mkdir()
    snapshot = {"recovered_count": 5, "recovery_by_reason": {"insufficient_funds": {"count": 3}}}
    (tmp_path / "data" / "results_dev.json").write_text(json.dumps(snapshot), encoding="utf-8")

    resp = client.get("/batch/results")

    assert resp.status_code == 200
    assert resp.json() == snapshot


def test_batch_results_defaults_to_the_dev_set(tmp_path, monkeypatch):
    """No ?set= at all -- same default app.detection.batch_scanner's own
    --set argparse default uses."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "results_dev.json").write_text(json.dumps({"ok": True}), encoding="utf-8")

    resp = client.get("/batch/results")  # no ?set= at all

    assert resp.status_code == 200
    assert resp.json() == {"ok": True}


def test_batch_results_respects_the_set_query_param(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "results_holdout.json").write_text(json.dumps({"total_cases": 300}), encoding="utf-8")

    resp = client.get("/batch/results", params={"set": "holdout"})

    assert resp.status_code == 200
    assert resp.json() == {"total_cases": 300}


def test_batch_results_404s_with_a_clear_message_when_the_snapshot_is_missing(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    resp = client.get("/batch/results", params={"set": "dev"})

    assert resp.status_code == 404
    detail = resp.json()["detail"]
    assert "dev" in detail
    assert "batch_scanner" in detail
