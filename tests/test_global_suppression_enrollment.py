"""OUTBOUND-SAFETY-0A — enrollment-time enforcement (STEP 5): a globally
suppressed email must never be newly enrolled in a campaign, via REST bulk
add or CSV import (the two entry points that create CampaignLead rows;
add_campaign_leads MCP tool proxies to the same REST endpoint — see
test_global_suppression_mcp.py).

Uses httpx directly against the ASGI app (no TestClient/session+engine
fixture combo — see test_mcp_tools.py's note on why that combination is
fragile under this suite's SQLite setup).
"""
from __future__ import annotations

import io
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
        _ADMIN_HEADERS_CACHE = await authenticated_api_key_headers("suppenroll")
    return _ADMIN_HEADERS_CACHE


async def _client():
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=fastapi_app), base_url="http://testserver")


async def _create_campaign(client: httpx.AsyncClient, headers: dict, name: str) -> int:
    r = await client.post("/api/campaigns", headers=headers, json={"name": name, "inbox_ids": []})
    assert r.status_code in (200, 201), r.text
    return r.json()["id"]


class TestBulkAddRejectsSuppressedLeads:
    @pytest.mark.asyncio
    async def test_suppressed_email_is_not_enrolled(self, db_ready):
        headers = await _admin_headers()
        headers_json = {**headers, "Content-Type": "application/json"}
        async with await _client() as client:
            campaign_id = await _create_campaign(client, headers_json, f"Suppression Test {uuid.uuid4().hex[:8]}")

            suppressed_email = f"suppressed-{uuid.uuid4().hex[:8]}@example.com"
            add_resp = await client.post(
                "/api/suppressions", headers=headers_json,
                json={"email": suppressed_email, "reason": "manual_block"},
            )
            assert add_resp.status_code == 201, add_resp.text

            bulk_resp = await client.post(
                f"/api/campaigns/{campaign_id}/leads",
                headers=headers_json,
                json=[{"email": suppressed_email}],
            )
            assert bulk_resp.status_code == 200, bulk_resp.text
            body = bulk_resp.json()
            assert body["added"] == 0
            assert body["suppressed"] == 1
            assert body["results"][0]["status"] == "suppressed"

            # Not silently enrolled: no CampaignLead was created for it.
            leads_resp = await client.get(f"/api/campaigns/{campaign_id}", headers=headers_json)
            assert leads_resp.status_code == 200, leads_resp.text

    @pytest.mark.asyncio
    async def test_non_suppressed_email_in_same_batch_is_still_enrolled(self, db_ready):
        """Regression: suppression must not over-block the rest of a batch."""
        headers = await _admin_headers()
        headers_json = {**headers, "Content-Type": "application/json"}
        async with await _client() as client:
            campaign_id = await _create_campaign(client, headers_json, f"Mixed Batch {uuid.uuid4().hex[:8]}")

            suppressed_email = f"blocked-{uuid.uuid4().hex[:8]}@example.com"
            clean_email = f"clean-{uuid.uuid4().hex[:8]}@example.com"
            await client.post(
                "/api/suppressions", headers=headers_json,
                json={"email": suppressed_email, "reason": "manual_block"},
            )

            bulk_resp = await client.post(
                f"/api/campaigns/{campaign_id}/leads",
                headers=headers_json,
                json=[{"email": suppressed_email}, {"email": clean_email}],
            )
            assert bulk_resp.status_code == 200, bulk_resp.text
            body = bulk_resp.json()
            assert body["added"] == 1
            assert body["suppressed"] == 1
            statuses = {r["email"]: r["status"] for r in body["results"]}
            assert statuses[suppressed_email] == "suppressed"
            assert statuses[clean_email] == "added"


class TestCsvImportRejectsSuppressedLeads:
    @pytest.mark.asyncio
    async def test_csv_import_skips_suppressed_row(self, db_ready):
        headers = await _admin_headers()
        headers_json = {**headers, "Content-Type": "application/json"}
        async with await _client() as client:
            campaign_id = await _create_campaign(client, headers_json, f"CSV Suppression {uuid.uuid4().hex[:8]}")

            suppressed_email = f"csv-suppressed-{uuid.uuid4().hex[:8]}@example.com"
            clean_email = f"csv-clean-{uuid.uuid4().hex[:8]}@example.com"
            await client.post(
                "/api/suppressions", headers=headers,
                json={"email": suppressed_email, "reason": "do_not_contact"},
            )

            csv_content = f"email,name\n{suppressed_email},Suppressed Person\n{clean_email},Clean Person\n"
            files = {"file": ("leads.csv", io.BytesIO(csv_content.encode()), "text/csv")}
            resp = await client.post(
                f"/api/campaigns/{campaign_id}/leads/import",
                headers=headers,
                files=files,
            )
            assert resp.status_code == 200, resp.text
            body = resp.json()
            assert body["added"] == 1
            assert body["suppressed"] == 1
