"""
Tests for repository.bulk_insert — the handful-of-calls flush path
app/detection/batch_scanner.py uses to persist a memory run to Supabase.
No real Supabase: get_client() is monkeypatched to a fake client that just
records what it was asked to insert.
"""
from __future__ import annotations

import pytest

import app.db.repository as repository


class _FakeTable:
    def __init__(self, name, client):
        self.name = name
        self._client = client

    def insert(self, rows):
        self._chunk_index = self._client.chunk_calls
        self._client.chunk_calls += 1
        self._client.calls.append((self.name, list(rows)))
        return self

    def execute(self):
        if self._chunk_index in self._client.fail_on_chunks:
            raise (self._client.error or Exception("duplicate key value violates unique constraint"))
        return self


class _FakeClient:
    def __init__(self, fail_on_chunks=frozenset(), error=None):
        self.calls: list[tuple[str, list[dict]]] = []
        self.chunk_calls = 0
        self.fail_on_chunks = fail_on_chunks
        self.error = error

    def table(self, name):
        return _FakeTable(name, self)


def test_bulk_insert_with_no_rows_makes_no_calls(monkeypatch):
    client = _FakeClient()
    monkeypatch.setattr(repository, "get_client", lambda: client)
    assert repository.bulk_insert("cases", []) == 0
    assert client.calls == []


def test_bulk_insert_under_the_chunk_size_is_a_single_call(monkeypatch):
    client = _FakeClient()
    monkeypatch.setattr(repository, "get_client", lambda: client)
    rows = [{"id": str(i)} for i in range(10)]
    n = repository.bulk_insert("cases", rows, chunk_size=500)
    assert n == 10
    assert len(client.calls) == 1
    assert client.calls[0][0] == "cases"
    assert len(client.calls[0][1]) == 10


def test_bulk_insert_chunks_at_the_given_size(monkeypatch):
    client = _FakeClient()
    monkeypatch.setattr(repository, "get_client", lambda: client)
    rows = [{"id": str(i)} for i in range(1201)]
    n = repository.bulk_insert("audit_log", rows, chunk_size=500)
    assert n == 1201
    assert len(client.calls) == 3  # 500 + 500 + 201
    assert [len(rows) for _, rows in client.calls] == [500, 500, 201]


def test_bulk_insert_default_chunk_size_is_500(monkeypatch):
    client = _FakeClient()
    monkeypatch.setattr(repository, "get_client", lambda: client)
    rows = [{"id": str(i)} for i in range(750)]
    repository.bulk_insert("outreach", rows)
    assert [len(rows) for _, rows in client.calls] == [500, 250]


# ---------------------------------------------------------------------------
# resilience — a duplicate-key chunk is skipped, not fatal to the whole flush
# ---------------------------------------------------------------------------

def test_bulk_insert_skips_a_duplicate_key_chunk_and_continues(monkeypatch, capsys):
    client = _FakeClient(fail_on_chunks={0})  # first chunk (offset 0) conflicts
    monkeypatch.setattr(repository, "get_client", lambda: client)
    rows = [{"id": str(i)} for i in range(1000)]  # two chunks of 500

    n = repository.bulk_insert("cases", rows, chunk_size=500)

    assert len(client.calls) == 2  # both chunks were attempted
    assert n == 500  # only the second (successful) chunk counted
    err = capsys.readouterr().err
    assert "WARNING" in err
    assert "cases" in err


def test_bulk_insert_reraises_non_duplicate_errors(monkeypatch):
    client = _FakeClient(fail_on_chunks={0}, error=ConnectionError("network is down"))
    monkeypatch.setattr(repository, "get_client", lambda: client)
    rows = [{"id": "1"}]

    with pytest.raises(ConnectionError):
        repository.bulk_insert("cases", rows)


def test_bulk_insert_all_duplicate_error_message_variants_are_caught(monkeypatch):
    for message in ("duplicate key value", "UNIQUE constraint failed", "error 23505"):
        client = _FakeClient(fail_on_chunks={0}, error=Exception(message))
        monkeypatch.setattr(repository, "get_client", lambda: client)
        n = repository.bulk_insert("cases", [{"id": "1"}])
        assert n == 0
