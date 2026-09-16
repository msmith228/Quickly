"""OUTBOUND-SAFETY-0A — send-fire-time enforcement (STEP 4) and the
already-queued-then-suppressed acceptance test (STEP 6).

Postgres-only, same as tests/test_jobs.py and for the same reason: SQLite +
aiosqlite has threading issues under run_send_job()'s AsyncSessionLocal
usage. Point TEST_DATABASE_URL at a real (ideally isolated/throwaway)
Postgres database to run these.
"""
from __future__ import annotations

import pytest
from datetime import datetime, timedelta

from app.database import engine

pytestmark = pytest.mark.skipif(
    engine.dialect.name == "sqlite",
    reason="SQLite aiosqlite backend cannot reliably run these integration tests",
)

from sqlalchemy import select, func

from app.jobs import run_send_job
from app.sender import SendResult, SendFailure
from app.models import EmailLog, GlobalSuppression, GmailAccount, QueueSlot
from app.campaign_lead_status import campaign_lead_may_receive_sends
from app.suppression import add_suppression, get_all_suppressed_emails, is_globally_suppressed
from app import time as time_provider
from app.settings_manager import settings
from tests.conftest import (
    make_inbox,
    make_campaign,
    make_sequence,
    make_lead,
    make_campaign_lead,
    make_campaign_inbox,
    make_queue_slot,
)


class _SessionCtx:
    def __init__(self, session):
        self.session = session

    async def __aenter__(self):
        return self.session

    async def __aexit__(self, exc_type, exc, tb):
        return None


class TestCampaignLeadMayReceiveSendsSuppressionGate:
    @pytest.mark.asyncio
    async def test_suppressed_lead_is_blocked_regardless_of_otherwise_clean_state(self, session):
        campaign = await make_campaign(session)
        lead = await make_lead(session, email="clean-but-suppressed@example.com")
        cl = await make_campaign_lead(session, campaign.id, lead.id)
        await add_suppression(session, lead.email, reason="manual_block")

        suppressed = await get_all_suppressed_emails(session)
        assert campaign_lead_may_receive_sends(cl, lead, suppressed) is False

    @pytest.mark.asyncio
    async def test_non_suppressed_lead_still_follows_existing_rules(self, session):
        """Regression: an ordinary active enrollment must remain sendable —
        the new check must not affect leads that were never suppressed."""
        campaign = await make_campaign(session)
        lead = await make_lead(session, email="ordinary@example.com")
        cl = await make_campaign_lead(session, campaign.id, lead.id)

        suppressed = await get_all_suppressed_emails(session)
        assert campaign_lead_may_receive_sends(cl, lead, suppressed) is True

    @pytest.mark.asyncio
    async def test_existing_block_reasons_still_work_alongside_suppression(self, session):
        """Regression: bounced/unsubscribed/etc enrollment-status blocking,
        untouched by this task, must keep working."""
        campaign = await make_campaign(session)
        lead = await make_lead(session, email="bounced-elsewhere@example.com")
        cl = await make_campaign_lead(session, campaign.id, lead.id)
        cl.enrollment_status = "bounced"
        await session.flush()

        suppressed = await get_all_suppressed_emails(session)
        assert campaign_lead_may_receive_sends(cl, lead, suppressed) is False


class TestAlreadyQueuedSendBlockedBySuppression:
    """STEP 6 — the most important acceptance test in this task:
    1. lead is enrolled
    2. send is queued
    3. email is then globally suppressed
    4. send job fires
    5. no email is sent
    """

    @pytest.mark.asyncio
    async def test_queued_before_suppression_is_blocked_at_send_fire_time(self, session, monkeypatch):
        inbox = await make_inbox(session, email="sender@example.com")
        campaign = await make_campaign(session, sending_hours_start="00:00", sending_hours_end="23:59")
        await make_sequence(session, campaign.id)
        lead = await make_lead(session, email="queued-then-suppressed@example.com")
        cl = await make_campaign_lead(session, campaign.id, lead.id)
        await make_campaign_inbox(session, campaign.id, inbox.id)

        # 1 & 2: enrolled, and a queue slot exists (due now). run_send_job()
        # compares against time_provider.now() (LOCAL time — see its own
        # "Use local time so we match queue_logic" comment), not UTC.
        now = time_provider.now()
        await make_queue_slot(session, cl.id, inbox.id, sequence_index=0, scheduled_date=now - timedelta(minutes=5))
        await session.flush()

        sends: list[dict] = []

        def fake_send_email(**kwargs):
            # Defensive belt: if simulate_send were ever False here, this
            # stands in for the real network call so no real email can go
            # out — but the actual assertion below is on EmailLog, since
            # test_mode's simulate_send path (exercised here) never calls
            # send_email at all (it fabricates a SendResult directly).
            sends.append(kwargs)
            return SendResult(message_id="<should-not-happen>")

        monkeypatch.setattr("app.jobs.send_email", fake_send_email)
        monkeypatch.setattr("app.jobs.AsyncSessionLocal", lambda: _SessionCtx(session))
        # HARD RULE for this task: test_mode stays True. Also exercises the
        # real "no mailbox connected -> simulate send" path instead of
        # short-circuiting before ever reaching the suppression check.
        monkeypatch.setattr(settings, "test_mode", True)

        # 3: suppressed AFTER enrollment and AFTER the slot was queued.
        await add_suppression(session, lead.email, reason="manual_block", source="test")
        await session.flush()

        # 4: send job fires.
        await run_send_job()

        # 5: no email was sent — no real send attempt, and no EmailLog
        # (proving the suppression check ran before phase 3 / email_log_entry
        # creation, not just before the provider call).
        assert sends == [], "send_email must never be called for a globally suppressed lead"
        res = await session.execute(select(func.count(EmailLog.id)).where(EmailLog.lead_id == lead.id))
        assert res.scalar() == 0

        # ...and the now-unsendable slot was cleaned up rather than left to
        # retry forever (matches existing behavior for other block reasons).
        res2 = await session.execute(select(func.count(QueueSlot.id)).where(QueueSlot.campaign_lead_id == cl.id))
        assert res2.scalar() == 0

    @pytest.mark.asyncio
    async def test_non_suppressed_queued_lead_in_same_run_still_sends(self, session, monkeypatch):
        """Companion regression: the suppression check must not over-block —
        a normal queued lead in the same job run still gets its email."""
        inbox = await make_inbox(session, email="sender2@example.com")
        campaign = await make_campaign(session, sending_hours_start="00:00", sending_hours_end="23:59")
        await make_sequence(session, campaign.id)
        lead = await make_lead(session, email="normal-queued@example.com")
        cl = await make_campaign_lead(session, campaign.id, lead.id)
        await make_campaign_inbox(session, campaign.id, inbox.id)

        now = time_provider.now()
        await make_queue_slot(session, cl.id, inbox.id, sequence_index=0, scheduled_date=now - timedelta(minutes=5))
        await session.flush()

        monkeypatch.setattr("app.jobs.AsyncSessionLocal", lambda: _SessionCtx(session))
        monkeypatch.setattr(settings, "test_mode", True)

        await run_send_job()

        # test_mode's simulate_send path logs the send without a real
        # provider call — EmailLog existing IS "it sent" here.
        res = await session.execute(select(func.count(EmailLog.id)).where(EmailLog.lead_id == lead.id))
        assert res.scalar() == 1
        res2 = await session.execute(select(func.count(QueueSlot.id)).where(QueueSlot.campaign_lead_id == cl.id))
        assert res2.scalar() == 0


class TestHardBounceCreatesGlobalSuppression:
    """STEP 8: SendFailure(error_type in ("bounce", "invalid_recipient")) is
    the only path that reaches this branch — 429/5xx transient failures
    return None and are retried elsewhere, never reaching here (see the
    "transient" test below). So every bounce that lands in this branch is
    already permanent/hard by construction; no separate soft/hard signal
    exists upstream to guess at."""

    @pytest.mark.asyncio
    async def test_hard_bounce_adds_global_suppression(self, session, monkeypatch):
        inbox = await make_inbox(session, email="bouncer@example.com", provider="gmail")
        session.add(GmailAccount(inbox_id=inbox.id, google_email=inbox.email, access_token="fake-at", refresh_token="fake-rt"))
        await session.flush()
        campaign = await make_campaign(session, sending_hours_start="00:00", sending_hours_end="23:59")
        await make_sequence(session, campaign.id)
        lead = await make_lead(session, email="will-hard-bounce@example.com")
        cl = await make_campaign_lead(session, campaign.id, lead.id)
        await make_campaign_inbox(session, campaign.id, inbox.id)

        now = time_provider.now()
        await make_queue_slot(session, cl.id, inbox.id, sequence_index=0, scheduled_date=now - timedelta(minutes=5))
        await session.flush()

        def fake_send_email(**kwargs):
            # No real network call — a stand-in permanent-failure result,
            # matching what a real 400/404-class provider response maps to.
            return SendFailure(error_type="bounce", message="550 no such user")

        monkeypatch.setattr("app.jobs.send_email", fake_send_email)
        monkeypatch.setattr("app.jobs.AsyncSessionLocal", lambda: _SessionCtx(session))
        monkeypatch.setattr(settings, "test_mode", False)  # force the real (mocked) send_email path

        await run_send_job()

        assert await is_globally_suppressed(session, lead.email) is True
        from app.suppression import get_suppression

        row = await get_suppression(session, lead.email)
        assert row.reason == "hard_bounce"
        assert row.source == "send_failure"

        await session.refresh(cl)
        assert cl.enrollment_status == "bounced"

    @pytest.mark.asyncio
    async def test_transient_failure_does_not_create_suppression(self, session, monkeypatch):
        """Soft/transient failures (429/5xx -> None, retried later) must
        NOT become a permanent global suppression."""
        inbox = await make_inbox(session, email="transient@example.com", provider="gmail")
        session.add(GmailAccount(inbox_id=inbox.id, google_email=inbox.email, access_token="fake-at", refresh_token="fake-rt"))
        await session.flush()
        campaign = await make_campaign(session, sending_hours_start="00:00", sending_hours_end="23:59")
        await make_sequence(session, campaign.id)
        lead = await make_lead(session, email="just-rate-limited@example.com")
        cl = await make_campaign_lead(session, campaign.id, lead.id)
        await make_campaign_inbox(session, campaign.id, inbox.id)

        now = time_provider.now()
        await make_queue_slot(session, cl.id, inbox.id, sequence_index=0, scheduled_date=now - timedelta(minutes=5))
        await session.flush()

        def fake_send_email(**kwargs):
            return None  # transient — matches sender.py's documented 429/5xx behavior

        monkeypatch.setattr("app.jobs.send_email", fake_send_email)
        monkeypatch.setattr("app.jobs.AsyncSessionLocal", lambda: _SessionCtx(session))
        monkeypatch.setattr(settings, "test_mode", False)

        await run_send_job()

        assert await is_globally_suppressed(session, lead.email) is False
        await session.refresh(cl)
        assert cl.enrollment_status == "active"  # unchanged — still eligible for retry
        # slot kept for retry, not deleted
        res = await session.execute(select(func.count(QueueSlot.id)).where(QueueSlot.campaign_lead_id == cl.id))
        assert res.scalar() == 1
