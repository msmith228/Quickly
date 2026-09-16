"""OUTBOUND-SAFETY-0A — REST API for global suppression (STEP 10, 15).

Same httpx-direct-against-ASGI pattern as test_global_suppression_enrollment.py
and test_mcp_tools.py (no TestClient/session+engine fixture combo).
"""
from __future__ import annotations

import uuid

import httpx
import pytest

from app.database import init_db
from app.main import app as fastapi_app
from tests.conftest import authenticated_api_key_headers


@pytest.fixture
async def db_ready():
    await init_db()


_ADMIN_HEADERS_CACHE: dict[str, str] | None = None


async def _admin_headers() -> dict[str, str]:
    global _ADMIN_HEADERS_CACHE
    if _ADMIN_HEADERS_CACHE is None:
        _ADMIN_HEADERS_CACHE = await authenticated_api_key_headers("supprest")
    return _ADMIN_HEADERS_CACHE


async def _client():
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=fastapi_app), base_url="http://testserver")


class TestAuthRequired:
    @pytest.mark.asyncio
    async def test_list_requires_auth(self, db_ready):
        async with await _client() as client:
            r = await client.get("/api/suppressions")
        assert r.status_code == 401

    @pytest.mark.asyncio
    async def test_add_requires_auth(self, db_ready):
        async with await _client() as client:
            r = await client.post("/api/suppressions", json={"email": "nope@example.com"})
        assert r.status_code == 401

    @pytest.mark.asyncio
    async def test_check_requires_auth(self, db_ready):
        async with await _client() as client:
            r = await client.get("/api/suppressions/check", params={"email": "nope@example.com"})
        assert r.status_code == 401

    @pytest.mark.asyncio
    async def test_remove_requires_auth(self, db_ready):
        async with await _client() as client:
            r = await client.delete("/api/suppressions/nope@example.com")
        assert r.status_code == 401


class TestAddCheckListRemove:
    @pytest.mark.asyncio
    async def test_full_lifecycle(self, db_ready):
        headers = await _admin_headers()
        headers_json = {**headers, "Content-Type": "application/json"}
        email = f"lifecycle-{uuid.uuid4().hex[:8]}@Example.COM"
        normalized = email.strip().lower()

        async with await _client() as client:
            # not suppressed yet
            check1 = await client.get("/api/suppressions/check", headers=headers, params={"email": email})
            assert check1.status_code == 200
            assert check1.json()["suppressed"] is False

            # add
            add_resp = await client.post(
                "/api/suppressions", headers=headers_json,
                json={"email": email, "reason": "customer", "note": "test note"},
            )
            assert add_resp.status_code == 201, add_resp.text
            added = add_resp.json()
            assert added["email"] == normalized  # normalized on write
            assert added["reason"] == "customer"

            # check now true
            check2 = await client.get("/api/suppressions/check", headers=headers, params={"email": email.upper()})
            assert check2.status_code == 200
            assert check2.json()["suppressed"] is True

            # appears in list
            list_resp = await client.get("/api/suppressions", headers=headers)
            assert list_resp.status_code == 200
            assert any(row["email"] == normalized for row in list_resp.json())

            # remove
            del_resp = await client.delete(f"/api/suppressions/{email}", headers=headers)
            assert del_resp.status_code == 200, del_resp.text

            # gone
            check3 = await client.get("/api/suppressions/check", headers=headers, params={"email": email})
            assert check3.status_code == 200
            assert check3.json()["suppressed"] is False

    @pytest.mark.asyncio
    async def test_remove_nonexistent_is_404(self, db_ready):
        headers = await _admin_headers()
        async with await _client() as client:
            r = await client.delete(f"/api/suppressions/never-existed-{uuid.uuid4().hex[:8]}@example.com", headers=headers)
        assert r.status_code == 404

    @pytest.mark.asyncio
    async def test_add_is_idempotent_via_rest(self, db_ready):
        headers = await _admin_headers()
        headers_json = {**headers, "Content-Type": "application/json"}
        email = f"idem-rest-{uuid.uuid4().hex[:8]}@example.com"
        async with await _client() as client:
            r1 = await client.post("/api/suppressions", headers=headers_json, json={"email": email, "reason": "manual_block"})
            r2 = await client.post("/api/suppressions", headers=headers_json, json={"email": email, "reason": "complaint"})
        assert r1.status_code == 201
        assert r2.status_code == 201
        assert r1.json()["id"] == r2.json()["id"]
        assert r2.json()["reason"] == "manual_block"  # first reason wins


class TestResponseDoesNotLeakSensitiveData:
    @pytest.mark.asyncio
    async def test_suppression_response_has_no_credential_fields(self, db_ready):
        headers = await _admin_headers()
        headers_json = {**headers, "Content-Type": "application/json"}
        email = f"noleak-{uuid.uuid4().hex[:8]}@example.com"
        async with await _client() as client:
            r = await client.post("/api/suppressions", headers=headers_json, json={"email": email})
        assert r.status_code == 201
        keys = set(r.json().keys())
        leaky = {"access_token", "refresh_token", "password", "api_key", "smtp_password", "imap_password", "client_secret"}
        assert not (keys & leaky), f"unexpected sensitive-looking fields: {keys & leaky}"
        assert keys == {"id", "email", "reason", "note", "source", "created_at"}
