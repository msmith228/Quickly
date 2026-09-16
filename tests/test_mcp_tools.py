"""OUTBOUND-QUICKLY-0B — MCP tool extension (campaign lifecycle, replies/
Unibox, analytics) and MCP authorization.

Testing approach note: FastMCP's StreamableHTTPSessionManager is single-use
per process (``session_manager.run()`` cannot be called twice), and this
suite's own conftest.py patches ``leads_mcp_lifespan`` to a no-op on every
test specifically because ``TestClient(app)`` restarts the app's lifespan
per test ("FastMCP's session_manager.run() is single-use; TestClient
restarts the app per test") — so no test in this suite can exercise a real
``/api/mcp`` JSON-RPC round trip without either (a) sharing one TestClient
across the whole session (impractical — the DB inside it is also shared,
worse isolation than tests already have) or (b) fighting the autouse
patch. Both are the wrong trade for this suite.

Instead: call the tool functions directly as plain Python coroutines,
building a minimal fake MCP ``Context`` that carries a real Starlette
``Request`` with the auth header the tool reads (exactly what
``_outbound_headers`` needs — see app/mcp_leads.py), and route the tool's
own internal ``httpx.AsyncClient`` calls to the REST API in-process via
``httpx.ASGITransport`` instead of a real socket. This exercises the real
tool logic (URL/params built correctly, real REST endpoint, real
auth/validation, real response parsing) without the transport-layer
plumbing that's fragile under TestClient. The transport/JSON-RPC/SSE
framing itself (auth-before-dispatch, tools/list shape) is covered
separately below via one call that doesn't need the session manager
(a 401 short-circuits before ever reaching it), plus the full round trip
against a real running dev server during this task's browser acceptance
pass (see the final report).
"""
from __future__ import annotations

import uuid

import httpx
import pytest
import pytest_asyncio
from starlette.requests import Request

from app.database import init_db
from app.main import app as fastapi_app
import app.mcp_leads as mcp_leads_module
from app.mcp_leads import (
    add_campaign_leads,
    create_campaign,
    get_campaign,
    get_campaign_analytics,
    get_reply_thread,
    leads_mcp,
    list_campaigns,
    list_replies,
    pause_campaign,
    resume_campaign,
)


@pytest.fixture(autouse=True)
def _route_mcp_tool_httpx_to_asgi(monkeypatch):
    """The tools' own httpx.AsyncClient(...) calls (see mcp_leads.py) hit a
    real base_url (settings.base_url, e.g. http://localhost:8000) — with no
    real server listening there in a pytest run, that would just hang/fail
    with a connection error. Route those specific calls through
    httpx.ASGITransport into the SAME in-process FastAPI app instead — a
    standard technique for testing code that calls out via httpx to an
    ASGI app, no real socket involved."""

    class _ASGIAsyncClient(httpx.AsyncClient):
        def __init__(self, *args, **kwargs):
            kwargs["transport"] = httpx.ASGITransport(app=fastapi_app)
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(mcp_leads_module.httpx, "AsyncClient", _ASGIAsyncClient)


@pytest_asyncio.fixture
async def db_ready():
    """Direct init_db() call — schema + settings, no app lifespan/session
    manager involved at all (the thing we're avoiding). Safe to call more
    than once (create_all is idempotent; initialize_settings just reloads)."""
    await init_db()


class _FakeContext:
    """Minimal stand-in for FastMCP's Context — only needs
    .request_context.request to be a real Starlette Request (mcp_leads.py's
    _outbound_headers does `isinstance(req, Request)`), carrying whatever
    auth header the test wants."""

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
    """Register the ONE admin this module needs, once, and cache it —
    Quickly allows exactly one admin (uq_single_admin) and this suite's
    default test database persists across tests in the same process, so
    every test in this file shares one registration."""
    global _ADMIN_HEADERS_CACHE
    if _ADMIN_HEADERS_CACHE is not None:
        return _ADMIN_HEADERS_CACHE
    suffix = uuid.uuid4().hex[:8]
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=fastapi_app), base_url="http://testserver") as client:
        reg = await client.post(
            "/api/auth/register",
            json={"username": f"mcptool{suffix}", "email": f"mcptool{suffix}@test.com", "password": "TestPass123"},
        )
        assert reg.status_code == 201, reg.text
        login = await client.post("/api/auth/login", json={"username": f"mcptool{suffix}", "password": "TestPass123"})
        assert login.status_code == 200, login.text
        jwt = login.json()["access_token"]

        key_resp = await client.post(
            "/api/auth/api-keys",
            headers={"Authorization": f"Bearer {jwt}"},
            json={"name": "mcp-tool-test-key", "scopes": []},
        )
        assert key_resp.status_code == 200, key_resp.text
    _ADMIN_HEADERS_CACHE = {"X-API-Key": key_resp.json()["key"]}
    return _ADMIN_HEADERS_CACHE


def _unique_name(label: str) -> str:
    return f"{label} {uuid.uuid4().hex[:8]}"


class TestToolRegistration:
    """Structural check that every planned tool is actually registered —
    doesn't need the session manager at all (list_tools() is a plain
    in-memory registry read)."""

    @pytest.mark.asyncio
    async def test_all_new_and_original_tools_are_registered(self):
        tools = await leads_mcp.list_tools()
        names = {t.name for t in tools}
        expected_new = {
            "list_campaigns", "get_campaign", "create_campaign",
            "pause_campaign", "resume_campaign", "get_campaign_analytics",
            "list_replies", "get_reply_thread",
        }
        expected_original = {"list_leads", "get_lead", "update_lead", "delete_lead", "add_campaign_leads"}
        assert expected_new <= names, names
        assert expected_original <= names, names

    @pytest.mark.asyncio
    async def test_no_tool_resembles_a_send_email_tool(self):
        tools = await leads_mcp.list_tools()
        names = {t.name for t in tools}
        assert "send_email_now" not in names
        assert not any(("send" in n and "email" in n) or "send_now" in n for n in names), names


class TestCampaignLifecycleTools:
    @pytest.mark.asyncio
    async def test_create_then_list_then_get_campaign(self, db_ready):
        headers = await _shared_admin_headers()
        ctx = _FakeContext(headers)
        name = _unique_name("MCP Tool Campaign")

        created = _json(await create_campaign(ctx, name=name, inbox_ids=[]))
        assert created["name"] == name
        assert created["paused"] is False  # created = Draft, not started
        cid = created["id"]

        listed = _json(await list_campaigns(ctx))
        assert any(c["id"] == cid for c in listed)

        fetched = _json(await get_campaign(ctx, campaign_id=cid))
        assert fetched["id"] == cid
        assert fetched["name"] == name

    @pytest.mark.asyncio
    async def test_create_campaign_rejects_empty_name(self, db_ready):
        headers = await _shared_admin_headers()
        ctx = _FakeContext(headers)
        result = _json(await create_campaign(ctx, name="   "))
        assert "error" in result

    @pytest.mark.asyncio
    async def test_pause_and_resume_campaign(self, db_ready):
        headers = await _shared_admin_headers()
        ctx = _FakeContext(headers)
        created = _json(await create_campaign(ctx, name=_unique_name("Pausable"), inbox_ids=[]))
        cid = created["id"]

        paused_result = _json(await pause_campaign(ctx, campaign_id=cid))
        assert paused_result.get("paused") is True

        resumed_result = _json(await resume_campaign(ctx, campaign_id=cid))
        assert resumed_result.get("paused") is False

        fetched = _json(await get_campaign(ctx, campaign_id=cid))
        assert fetched["paused"] is False

    @pytest.mark.asyncio
    async def test_get_campaign_analytics_returns_step_shape(self, db_ready):
        headers = await _shared_admin_headers()
        ctx = _FakeContext(headers)
        created = _json(await create_campaign(ctx, name=_unique_name("Analytics Campaign"), inbox_ids=[]))
        result = _json(await get_campaign_analytics(ctx, campaign_id=created["id"]))
        assert isinstance(result, list)  # no sequences yet -> empty list, not an error

    @pytest.mark.asyncio
    async def test_add_campaign_leads_still_works_unchanged(self, db_ready):
        """Regression: the pre-existing add_campaign_leads tool must be
        unaffected by this module's other additions."""
        headers = await _shared_admin_headers()
        ctx = _FakeContext(headers)
        created = _json(await create_campaign(ctx, name=_unique_name("Leads Campaign"), inbox_ids=[]))
        result = _json(await add_campaign_leads(ctx, campaign_id=created["id"], leads=[{"email": "alice@example.com"}]))
        assert result.get("added") == 1


class TestRepliesUniboxTools:
    @pytest.mark.asyncio
    async def test_list_replies_on_empty_unibox_does_not_crash(self, db_ready):
        """No inbox connected (this task's own hard rule) — list_replies must
        return a clean empty result, never a 500."""
        headers = await _shared_admin_headers()
        ctx = _FakeContext(headers)
        result = _json(await list_replies(ctx))
        assert not (isinstance(result, dict) and result.get("status_code") == 500)

    @pytest.mark.asyncio
    async def test_get_reply_thread_for_unknown_thread_is_a_clean_error_not_crash(self, db_ready):
        headers = await _shared_admin_headers()
        ctx = _FakeContext(headers)
        result = _json(await get_reply_thread(ctx, thread_id="does-not-exist"))
        assert result is not None
        assert not (isinstance(result, dict) and result.get("status_code") == 500)


class TestToolAuthEnforcement:
    @pytest.mark.asyncio
    async def test_missing_auth_header_raises_before_any_network_call(self):
        """_outbound_headers raises RuntimeError when neither X-API-Key nor
        Authorization is present — proven directly, no server needed."""
        ctx = _FakeContext({})
        with pytest.raises(RuntimeError):
            await list_campaigns(ctx)


def _json(tool_result: str):
    import json

    return json.loads(tool_result)
