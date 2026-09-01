"""
Tests for app.db.repository.verify_schema() -- the startup check that
catches an unapplied migration loudly, before a batch run silently flushes 0
rows for the affected table. No real Supabase: get_client() is monkeypatched
to a fake client that simulates PostgREST's PGRST204 ("column not found in
schema cache") for whichever columns a test marks missing.
"""
from __future__ import annotations

import pytest

import app.db.repository as repository


class _FakeTable:
    def __init__(self, name, client):
        self.name = name
        self._client = client
        self._column = None

    def select(self, column):
        self._column = column
        return self

    def limit(self, n):
        return self

    def execute(self):
        self._client.probed.append((self.name, self._column))
        if (self.name, self._column) in self._client.missing_columns:
            raise Exception(
                f'{{"code":"PGRST204","message":"Could not find the '
                f"'{self._column}' column of '{self.name}' in the schema cache\"}}"
            )
        return self


class _FakeClient:
    def __init__(self, missing_columns=frozenset()):
        self.probed: list[tuple[str, str]] = []
        self.missing_columns = missing_columns

    def table(self, name):
        return _FakeTable(name, self)


def test_verify_schema_passes_when_every_expected_column_exists(monkeypatch):
    client = _FakeClient()
    monkeypatch.setattr(repository, "get_client", lambda: client)

    repository.verify_schema()  # must not raise

    assert ("promises", "source") in client.probed


def test_verify_schema_raises_and_names_the_migration_when_a_column_is_missing(monkeypatch):
    client = _FakeClient(missing_columns={("promises", "source")})
    monkeypatch.setattr(repository, "get_client", lambda: client)

    with pytest.raises(RuntimeError) as exc_info:
        repository.verify_schema()

    message = str(exc_info.value)
    assert "promises.source" in message
    assert "002_promise_source.sql" in message


def test_verify_schema_reports_every_missing_column_not_just_the_first(monkeypatch):
    """Extend _EXPECTED_COLUMNS's table temporarily to prove multiple
    failures are all collected and reported together, not just the first."""
    extra = {"cases": {"fake_column": "supabase/migrations/999_fake.sql"}}
    monkeypatch.setitem(repository._EXPECTED_COLUMNS, "cases", extra["cases"])
    client = _FakeClient(missing_columns={("promises", "source"), ("cases", "fake_column")})
    monkeypatch.setattr(repository, "get_client", lambda: client)

    with pytest.raises(RuntimeError) as exc_info:
        repository.verify_schema()

    message = str(exc_info.value)
    assert "promises.source" in message
    assert "cases.fake_column" in message
    assert "999_fake.sql" in message
