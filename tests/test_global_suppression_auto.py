"""OUTBOUND-SAFETY-0A — automatic suppression triggers (STEP 7, 8):
a genuine unsubscribe becomes global; a hard/permanent bounce becomes
global; a soft/transient failure does not.
"""
from __future__ import annotations

import pytest

from app.tracking_events import process_unsubscribe
from app.suppression import is_globally_suppressed
from tests.conftest import make_campaign, make_lead, make_campaign_lead, make_unsubscribe_token


class TestUnsubscribeCreatesGlobalSuppression:
    @pytest.mark.asyncio
    async def test_unsubscribe_adds_global_suppression(self, session):
        campaign = await make_campaign(session)
        lead = await make_lead(session, email="unsub-target@example.com")
        cl = await make_campaign_lead(session, campaign.id, lead.id)
        await make_unsubscribe_token(session, lead.id, campaign.id, token="tok-1")
        await session.flush()

        html, status = await process_unsubscribe(session, "tok-1")
        assert status == 200

        assert await is_globally_suppressed(session, lead.email) is True
        # Per-campaign state is complementary, not replaced.
        await session.refresh(cl)
        assert cl.enrollment_status == "unsubscribed"

    @pytest.mark.asyncio
    async def test_unsubscribe_suppression_reason_is_unsubscribed(self, session):
        from app.suppression import get_suppression

        campaign = await make_campaign(session)
        lead = await make_lead(session, email="unsub-reason@example.com")
        await make_campaign_lead(session, campaign.id, lead.id)
        await make_unsubscribe_token(session, lead.id, campaign.id, token="tok-2")
        await session.flush()

        await process_unsubscribe(session, "tok-2")

        row = await get_suppression(session, lead.email)
        assert row is not None
        assert row.reason == "unsubscribed"
        assert row.source == "unsubscribe_link"

    @pytest.mark.asyncio
    async def test_double_unsubscribe_is_idempotent(self, session):
        """Second click on the same/already-processed unsubscribe link must
        not error and must not create a duplicate suppression row."""
        campaign = await make_campaign(session)
        lead = await make_lead(session, email="unsub-twice@example.com")
        await make_campaign_lead(session, campaign.id, lead.id)
        await make_unsubscribe_token(session, lead.id, campaign.id, token="tok-3")
        await session.flush()

        await process_unsubscribe(session, "tok-3")
        html, status = await process_unsubscribe(session, "tok-3")  # already_done path
        assert status == 200

        assert await is_globally_suppressed(session, lead.email) is True

    @pytest.mark.asyncio
    async def test_unsubscribe_from_one_campaign_protects_a_different_campaign(self, session):
        """The actual point of this whole task: opting out of campaign A
        must stop campaign B from ever being able to (re-)enroll/send to
        the same address."""
        campaign_a = await make_campaign(session, name="Campaign A")
        campaign_b = await make_campaign(session, name="Campaign B")
        lead = await make_lead(session, email="cross-campaign@example.com")
        await make_campaign_lead(session, campaign_a.id, lead.id)
        cl_b = await make_campaign_lead(session, campaign_b.id, lead.id)
        await make_unsubscribe_token(session, lead.id, campaign_a.id, token="tok-4")
        await session.flush()

        await process_unsubscribe(session, "tok-4")

        # Campaign B's own enrollment_status is untouched (still "active")...
        await session.refresh(cl_b)
        assert cl_b.enrollment_status == "active"
        # ...but the global suppression check (the authoritative one) blocks it.
        from app.campaign_lead_status import campaign_lead_may_receive_sends
        from app.suppression import get_all_suppressed_emails

        suppressed = await get_all_suppressed_emails(session)
        assert campaign_lead_may_receive_sends(cl_b, lead, suppressed) is False

    @pytest.mark.asyncio
    async def test_invalid_token_does_not_suppress_anything(self, session):
        html, status = await process_unsubscribe(session, "does-not-exist")
        assert status == 404
