"""Remote MCP (Streamable HTTP) for Quickly — mounted at ``/api/mcp``.

Originally "leads only" (module/server name kept as ``quickly-leads`` for
URL/identity stability with existing mcp-remote configs); OUTBOUND-QUICKLY-0B
added a small set of campaign-lifecycle, reply/Unibox, and analytics tools —
see the instructions string below for the full, deliberately-short list.
Every tool is a thin wrapper that proxies to the same REST endpoints the web
UI calls (same auth, same validation, same business logic) — no tool talks
to the database directly, and there is intentionally no send-email tool:
an agent can prepare and control campaigns through these tools, but actual
sending stays gated behind Quickly's own campaign engine and test-mode
switch, never bypassed here.
"""

from __future__ import annotations

import contextlib
import json
import logging
from typing import Any

import httpx
from mcp.server.fastmcp import Context, FastMCP
from mcp.server.fastmcp.server import StreamableHTTPASGIApp
from mcp.server.transport_security import TransportSecuritySettings
from starlette.requests import Request
from starlette.responses import JSONResponse

from app.database import AsyncSessionLocal
from app.auth import try_resolve_user_for_mcp
from app.settings_manager import settings as app_settings

log = logging.getLogger("quickly.mcp_leads")

leads_mcp = FastMCP(
    "quickly-leads",
    instructions=(
        "Quickly automation tools — leads, campaign lifecycle (list/get/create/"
        "pause/resume), replies/Unibox (list/get thread), and per-campaign "
        "analytics. No tool sends email; campaigns you create/resume here are "
        "still governed by Quickly's own test-mode switch and send-time "
        "eligibility checks. Authenticate MCP HTTP requests with X-API-Key "
        "(Settings → API Keys) or Authorization: Bearer (JWT)."
    ),
    # Default FastMCP host is 127.0.0.1, which enables MCP DNS-rebinding checks with
    # localhost-only allowed Host headers — breaks real Hosts (e.g. quickly.example.com)
    # behind Caddy. Disable here; Quickly already terminates TLS and validates access.
    host="0.0.0.0",
    transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
    stateless_http=True,
    streamable_http_path="/",
)

# Force session manager creation (used by StreamableHTTPASGIApp and app lifespan).
leads_mcp.streamable_http_app()
_mcp_http_handler = StreamableHTTPASGIApp(leads_mcp.session_manager)


def _api_base() -> str:
    return (app_settings.base_url or "http://localhost:8000").rstrip("/")


def _outbound_headers(ctx: Context) -> dict[str, str]:
    rc = ctx.request_context
    req = rc.request
    if not isinstance(req, Request):
        raise RuntimeError("MCP tools require an HTTP request context")
    h: dict[str, str] = {"Accept": "application/json"}
    ak = req.headers.get("x-api-key")
    auth = req.headers.get("authorization")
    if ak:
        h["X-API-Key"] = ak
    elif auth:
        h["Authorization"] = auth
    else:
        raise RuntimeError("Missing X-API-Key or Authorization on MCP request")
    return h


def _json_response(r: httpx.Response) -> str:
    try:
        data = r.json()
    except Exception:
        data = {"status_code": r.status_code, "text": r.text[:8000]}
    if r.is_error:
        data = {"error": True, "status_code": r.status_code, "detail": data}
    return json.dumps(data, indent=2, default=str)


@leads_mcp.tool()
async def list_leads(
    ctx: Context,
    q: str = "",
    status: str = "",
    bad_only: bool = False,
    interest: str = "",
) -> str:
    """List leads with optional filters (search, enrollment status, bad_only, interest); filters stack with AND."""
    headers = _outbound_headers(ctx)
    headers["Content-Type"] = "application/json"
    params: dict[str, str] = {}
    if q.strip():
        params["q"] = q.strip()
    if status.strip():
        params["status"] = status.strip()
    if bad_only:
        params["bad_only"] = "true"
    if interest.strip():
        params["interest"] = interest.strip()
    url = f"{_api_base()}/api/leads"
    async with httpx.AsyncClient(timeout=120.0) as client:
        r = await client.get(url, headers=headers, params=params)
    return _json_response(r)


@leads_mcp.tool()
async def get_lead(ctx: Context, lead_id: int) -> str:
    """Get one lead by id, including campaign enrollments."""
    headers = _outbound_headers(ctx)
    url = f"{_api_base()}/api/leads/{lead_id}"
    async with httpx.AsyncClient(timeout=60.0) as client:
        r = await client.get(url, headers=headers)
    return _json_response(r)


@leads_mcp.tool()
async def update_lead(
    ctx: Context,
    lead_id: int,
    name: str | None = None,
    enrollment_status: str | None = None,
    custom_data: dict[str, Any] | None = None,
) -> str:
    """Patch lead fields (name, enrollment_status on all campaigns, custom_data). Enrollment changes recalculate the queue."""
    headers = _outbound_headers(ctx)
    body: dict[str, Any] = {}
    if name is not None:
        body["name"] = name
    if enrollment_status is not None:
        body["enrollment_status"] = enrollment_status
    if custom_data is not None:
        body["custom_data"] = custom_data
    if not body:
        return json.dumps({"error": "Provide at least one of: name, enrollment_status, custom_data"})
    url = f"{_api_base()}/api/leads/{lead_id}"
    async with httpx.AsyncClient(timeout=120.0) as client:
        r = await client.patch(url, headers={**headers, "Content-Type": "application/json"}, json=body)
    return _json_response(r)


@leads_mcp.tool()
async def delete_lead(ctx: Context, lead_id: int) -> str:
    """Delete a lead and associated logs; recalculates queue if enrolled."""
    headers = _outbound_headers(ctx)
    url = f"{_api_base()}/api/leads/{lead_id}"
    async with httpx.AsyncClient(timeout=120.0) as client:
        r = await client.delete(url, headers=headers)
    return _json_response(r)


@leads_mcp.tool()
async def add_campaign_leads(
    ctx: Context,
    campaign_id: int,
    leads: list[dict[str, Any]],
    skip_duplicates: bool = True,
    verify_emails: bool = False,
) -> str:
    """Add leads to a campaign. Each item should include at least email; optional name and custom_data object."""
    headers = _outbound_headers(ctx)
    if not leads:
        return json.dumps({"error": "leads array must not be empty"})
    url = f"{_api_base()}/api/campaigns/{campaign_id}/leads"
    params = {
        "skip_duplicates": "true" if skip_duplicates else "false",
        "verify_emails": "true" if verify_emails else "false",
    }
    async with httpx.AsyncClient(timeout=300.0) as client:
        r = await client.post(
            url,
            headers={**headers, "Content-Type": "application/json"},
            params=params,
            json=leads,
        )
    return _json_response(r)


# ---------------------------------------------------------------------------
# Campaign lifecycle (OUTBOUND-QUICKLY-0B)
# ---------------------------------------------------------------------------


@leads_mcp.tool()
async def list_campaigns(ctx: Context) -> str:
    """List every campaign with its aggregated stats (leads, sent, replies, open/click rate)."""
    headers = _outbound_headers(ctx)
    url = f"{_api_base()}/api/campaigns"
    async with httpx.AsyncClient(timeout=60.0) as client:
        r = await client.get(url, headers=headers)
    return _json_response(r)


@leads_mcp.tool()
async def get_campaign(ctx: Context, campaign_id: int) -> str:
    """Get one campaign by id, including its aggregated stats."""
    headers = _outbound_headers(ctx)
    url = f"{_api_base()}/api/campaigns/{campaign_id}"
    async with httpx.AsyncClient(timeout=60.0) as client:
        r = await client.get(url, headers=headers)
    return _json_response(r)


@leads_mcp.tool()
async def create_campaign(ctx: Context, name: str, inbox_ids: list[int] | None = None) -> str:
    """Create a new campaign. inbox_ids may be empty (add inboxes later from
    the UI/API before starting real sends) — a campaign created here always
    starts as a Draft (not paused=False does not mean "sending"; it still
    needs sequences and leads before the queue engine schedules anything)."""
    headers = _outbound_headers(ctx)
    if not name.strip():
        return json.dumps({"error": "name must not be empty"})
    url = f"{_api_base()}/api/campaigns"
    async with httpx.AsyncClient(timeout=60.0) as client:
        r = await client.post(
            url,
            headers={**headers, "Content-Type": "application/json"},
            json={"name": name, "inbox_ids": inbox_ids or []},
        )
    return _json_response(r)


async def _set_campaign_paused(ctx: Context, campaign_id: int, paused: bool) -> str:
    headers = _outbound_headers(ctx)
    url = f"{_api_base()}/api/campaigns/{campaign_id}"
    async with httpx.AsyncClient(timeout=60.0) as client:
        r = await client.patch(
            url,
            headers={**headers, "Content-Type": "application/json"},
            json={"paused": paused},
        )
    return _json_response(r)


@leads_mcp.tool()
async def pause_campaign(ctx: Context, campaign_id: int) -> str:
    """Pause a campaign — the send queue skips it entirely until resumed. Does not send anything."""
    return await _set_campaign_paused(ctx, campaign_id, True)


@leads_mcp.tool()
async def resume_campaign(ctx: Context, campaign_id: int) -> str:
    """Resume a paused campaign. Sending still only happens through Quickly's
    own queue engine, sending windows, and test-mode switch — resuming a
    campaign never sends anything immediately by itself."""
    return await _set_campaign_paused(ctx, campaign_id, False)


@leads_mcp.tool()
async def get_campaign_analytics(ctx: Context, campaign_id: int) -> str:
    """Per-step analytics for a campaign: sent/opens/clicks/replies/opportunities, including any A/B variant breakdown."""
    headers = _outbound_headers(ctx)
    url = f"{_api_base()}/api/campaigns/{campaign_id}/analytics/steps"
    async with httpx.AsyncClient(timeout=60.0) as client:
        r = await client.get(url, headers=headers)
    return _json_response(r)


# ---------------------------------------------------------------------------
# Replies / Unibox (OUTBOUND-QUICKLY-0B) — read/sync only, no send tool.
# ---------------------------------------------------------------------------


@leads_mcp.tool()
async def list_replies(
    ctx: Context,
    page: int = 1,
    page_size: int = 20,
    leads_only: bool = True,
) -> str:
    """List Unibox conversation threads, newest first. leads_only=True (default)
    shows only threads matched to a known lead — set False to see every
    connected inbox's conversations, not just outbound-campaign replies."""
    headers = _outbound_headers(ctx)
    url = f"{_api_base()}/api/unibox"
    params = {
        "page": str(page),
        "page_size": str(page_size),
        "leads_only": "true" if leads_only else "false",
    }
    async with httpx.AsyncClient(timeout=60.0) as client:
        r = await client.get(url, headers=headers, params=params)
    return _json_response(r)


@leads_mcp.tool()
async def get_reply_thread(ctx: Context, thread_id: str, inbox_id: int | None = None) -> str:
    """Get every message in one Unibox thread (hydrates content on demand). Read-only — does not mark anything as read or send a reply."""
    headers = _outbound_headers(ctx)
    url = f"{_api_base()}/api/unibox/threads/{thread_id}"
    params = {"inbox_id": str(inbox_id)} if inbox_id is not None else {}
    async with httpx.AsyncClient(timeout=60.0) as client:
        r = await client.get(url, headers=headers, params=params)
    return _json_response(r)


class _MCPAuthASGI:
    """Require the same auth as the REST API before handling MCP Streamable HTTP."""

    __slots__ = ("_inner",)

    def __init__(self, inner):
        self._inner = inner

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self._inner(scope, receive, send)
            return
        if scope["method"] == "OPTIONS":
            await self._inner(scope, receive, send)
            return
        raw = scope.get("headers") or []
        hdrs = {k.decode().lower(): v.decode() for k, v in raw}
        x_key = hdrs.get("x-api-key")
        auth = hdrs.get("authorization")
        user = None
        try:
            async with AsyncSessionLocal() as db:
                user = await try_resolve_user_for_mcp(db, x_api_key=x_key, authorization=auth)
                if user is not None:
                    await db.commit()
                else:
                    await db.rollback()
        except Exception:
            log.exception("mcp auth session failed")
            resp = JSONResponse({"detail": "Authentication failed"}, status_code=500)
            await resp(scope, receive, send)
            return
        if user is None:
            resp = JSONResponse({"detail": "Not authenticated"}, status_code=401)
            await resp(scope, receive, send)
            return
        await self._inner(scope, receive, send)


# Single ASGI stack for top-level Starlette Route (see main — cannot use Mount("/api/mcp"): it only
# matches /api/mcp/... with an extra segment, so /api/mcp would fall through to the SPA catch-all).
leads_mcp_http_asgi = _MCPAuthASGI(_mcp_http_handler)


@contextlib.asynccontextmanager
async def leads_mcp_lifespan():
    async with leads_mcp.session_manager.run():
        yield
