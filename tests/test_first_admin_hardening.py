"""OUTBOUND-QUICKLY-0B — first-admin bootstrap race hardening.

Both the classic register() (routers/auth.py) and the OAuth first-user
path (routers/app_oauth.py) do "count existing users, if zero create this
one as role='admin'" — a check-then-act race where two near-simultaneous
requests could both pass the count check before either commits. The fix is
a partial unique index (uq_single_admin, app/models.py) making "at most one
admin" an atomic DB-level invariant, plus a clean IntegrityError -> 403
translation at both call sites instead of a raw 500.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import IntegrityError

from app.main import app
from app.models import User


class TestUqSingleAdminConstraint:
    """The atomic backstop itself — proven directly at the DB level, which
    is the reliable way to verify a race-safety constraint (simulating true
    simultaneous requests in a single-process async test is not)."""

    @pytest.mark.asyncio
    async def test_second_admin_row_violates_the_partial_unique_index(self, session):
        session.add(User(username="admin1", email="admin1@test.com", password_hash="x", role="admin"))
        await session.flush()

        session.add(User(username="admin2", email="admin2@test.com", password_hash="x", role="admin"))
        with pytest.raises(IntegrityError):
            await session.flush()
        await session.rollback()

    @pytest.mark.asyncio
    async def test_multiple_non_admin_users_are_unaffected(self, session):
        """The partial index only constrains role='admin' rows — plain
        users must still be able to coexist freely."""
        session.add(User(username="u1", email="u1@test.com", password_hash="x", role="user"))
        session.add(User(username="u2", email="u2@test.com", password_hash="x", role="user"))
        await session.flush()  # must not raise

    @pytest.mark.asyncio
    async def test_one_admin_plus_many_users_is_fine(self, session):
        session.add(User(username="theadmin", email="theadmin@test.com", password_hash="x", role="admin"))
        session.add(User(username="u1", email="u1b@test.com", password_hash="x", role="user"))
        session.add(User(username="u2", email="u2b@test.com", password_hash="x", role="user"))
        await session.flush()  # must not raise


class TestRegisterEndpointRaceHandling:
    """Router-level: simulate the race by making is_setup_complete() report
    False (as it would for two requests that both checked before either
    committed) while an admin already exists in the DB — this exercises the
    new try/except IntegrityError path directly, not just the DB constraint."""

    @pytest.mark.asyncio
    async def test_register_returns_clean_403_when_admin_already_exists_despite_stale_check(
        self, session, monkeypatch
    ):
        # An admin "just committed by the other concurrent request".
        session.add(User(username="winner", email="winner@test.com", password_hash="x", role="admin"))
        await session.flush()
        await session.commit()

        # Simulate this request having read is_setup_complete() BEFORE that
        # commit landed (the actual race window).
        async def _stale_false(db):
            return False

        monkeypatch.setattr("app.routers.auth.is_setup_complete", _stale_false)

        with TestClient(app) as client:
            resp = client.post(
                "/api/auth/register",
                json={"username": "loser", "email": "loser@test.com", "password": "TestPass123"},
            )
        assert resp.status_code == 403
        assert "closed" in resp.json()["detail"].lower()

    @pytest.mark.asyncio
    async def test_only_one_admin_exists_after_the_race(self, session, monkeypatch):
        session.add(User(username="winner2", email="winner2@test.com", password_hash="x", role="admin"))
        await session.flush()
        await session.commit()

        async def _stale_false(db):
            return False

        monkeypatch.setattr("app.routers.auth.is_setup_complete", _stale_false)

        with TestClient(app) as client:
            client.post(
                "/api/auth/register",
                json={"username": "loser2", "email": "loser2@test.com", "password": "TestPass123"},
            )

        from sqlalchemy import select

        admins = (await session.execute(select(User).where(User.role == "admin"))).scalars().all()
        assert len(admins) == 1
        assert admins[0].username == "winner2"

