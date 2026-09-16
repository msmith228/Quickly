"""OUTBOUND-SAFETY-0A — MCP tools for global suppression (STEP 12, 13).

Same approach as tests/test_mcp_tools.py: call the tool functions directly
as plain coroutines with a fake Context carrying a real auth header, and
route their internal httpx.AsyncClient calls to the in-process ASGI app —
see that file's module docstring for why (FastMCP's session manager is
single-use per process; TestClient restarts the app's lifespan per test).
"""
from __future__ import annotations

import json
import uuid

import httpx
import pytest
from starlette.requests import Request

from app.database import init_db
from app.main import app as fastapi_app
import app.mcp_leads as mcp_leads_module
from app.mcp_leads import (
    add_global_suppression,
    check_global_suppression,
    leads_mcp,
    list_global_suppressions,
    remove_global_suppression,
)
from tests.conftest import authenticated_api_key_headers


@pytest.fixture(autouse=True)
def _route_mcp_tool_httpx_to_asgi(monkeypatch):
    class _ASGIAsyncClient(httpx.AsyncClient):
        def __init__(self, *args, **kwargs):
            kwargs["transport"] = httpx.ASGITransport(app=fastapi_app)
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(mcp_leads_module.httpx, "AsyncClient", _ASGIAsyncClient)


@pytest.fixture
async def db_ready():
    await init_db()


class _FakeContext:
    def __init__(self, headers: dict[str, str]):
        raw_headers = [(k.lower().encode(), v.encode()) for k, v in headers.items()]
        request = Request(scope={"type": "http", "headers": raw_headers, "method": "GET", "path": "/api/mcp"})

        class _RC:
            pass

        rc = _RC()
        rc.request = request
        self.request_context = rc


_ADMIN_HEADERS_CACHE: dict[str, str] | None = None


async def _shared_admin_headers() -> dict[str, str]:
    global _ADMIN_HEADERS_CACHE
    if _ADMIN_HEADERS_CACHE is None:
        _ADMIN_HEADERS_CACHE = await authenticated_api_key_headers("suppmcp")
    return _ADMIN_HEADERS_CACHE


def _json(tool_result: str):
    return json.loads(tool_result)


class TestToolRegistration:
    @pytest.mark.asyncio
    async def test_suppression_tools_are_registered(self):
        tools = await leads_mcp.list_tools()
        names = {t.name for t in tools}
        expected = {
            "check_global_suppression", "add_global_suppression",
            "remove_global_suppression", "list_global_suppressions",
        }
        assert expected <= names, names


class TestSuppressionToolsCallSameRestLayer:
    @pytest.mark.asyncio
    async def test_add_check_list_remove_round_trip(self, db_ready):
        headers = await _shared_admin_headers()
        ctx = _FakeContext(headers)
        email = f"mcp-supp-{uuid.uuid4().hex[:8]}@example.com"

        check_before = _json(await check_global_suppression(ctx, email=email))
        assert check_before["suppressed"] is False

        added = _json(await add_global_suppression(ctx, email=email, reason="do_not_contact"))
        assert added["email"] == email.lower()
        assert added["reason"] == "do_not_contact"

        check_after = _json(await check_global_suppression(ctx, email=email))
        assert check_after["suppressed"] is True

        listed = _json(await list_global_suppressions(ctx))
        assert any(row["email"] == email.lower() for row in listed)

        removed = _json(await remove_global_suppression(ctx, email=email))
        assert removed.get("ok") is True

        check_final = _json(await check_global_suppression(ctx, email=email))
        assert check_final["suppressed"] is False

    @pytest.mark.asyncio
    async def test_add_via_mcp_is_visible_to_rest_and_vice_versa(self, db_ready):
        """No duplicated business logic — MCP and REST must see the same data."""
        headers = await _shared_admin_headers()
        ctx = _FakeContext(headers)
        email = f"mcp-rest-shared-{uuid.uuid4().hex[:8]}@example.com"

        await add_global_suppression(ctx, email=email)

        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=fastapi_app), base_url="http://testserver") as client:
            r = await client.get("/api/suppressions/check", headers=headers, params={"email": email})
        assert r.status_code == 200
        assert r.json()["suppressed"] is True


class TestToolAuthEnforcement:
    @pytest.mark.asyncio
    async def test_missing_auth_header_raises_before_any_network_call(self):
        ctx = _FakeContext({})
        with pytest.raises(RuntimeError):
            await check_global_suppression(ctx, email="whoever@example.com")

    @pytest.mark.asyncio
    async def test_add_missing_auth_header_raises(self):
        ctx = _FakeContext({})
        with pytest.raises(RuntimeError):
            await add_global_suppression(ctx, email="whoever@example.com")
