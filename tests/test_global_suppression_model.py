"""OUTBOUND-SAFETY-0A — GlobalSuppression model, normalization, and the
app.suppression service-layer helpers everything else is built on.
"""
from __future__ import annotations

import pytest
from sqlalchemy.exc import IntegrityError

from app.models import GlobalSuppression
from app.suppression import (
    add_suppression,
    get_all_suppressed_emails,
    get_suppression,
    is_globally_suppressed,
    list_suppressions,
    normalize_email,
    remove_suppression,
)


class TestNormalizeEmail:
    def test_lowercases(self):
        assert normalize_email("Person@Example.com") == "person@example.com"

    def test_strips_whitespace(self):
        assert normalize_email("  person@example.com  ") == "person@example.com"

    def test_combined(self):
        assert normalize_email("  Person@Example.COM ") == "person@example.com"

    def test_no_plus_address_rewriting(self):
        """STEP 14: only case/whitespace — never Gmail-style plus-address games."""
        assert normalize_email("Person+tag@Example.com") == "person+tag@example.com"

    def test_empty_and_none(self):
        assert normalize_email("") == ""
        assert normalize_email(None) == ""


class TestGlobalSuppressionModel:
    @pytest.mark.asyncio
    async def test_create_suppression_row(self, session):
        row = GlobalSuppression(email="a@example.com", reason="manual_block")
        session.add(row)
        await session.flush()
        assert row.id is not None
        assert row.created_at is not None

    @pytest.mark.asyncio
    async def test_duplicate_normalized_email_violates_unique_constraint(self, session):
        session.add(GlobalSuppression(email="dup@example.com", reason="manual_block"))
        await session.flush()
        session.add(GlobalSuppression(email="dup@example.com", reason="complaint"))
        with pytest.raises(IntegrityError):
            await session.flush()
        await session.rollback()

    @pytest.mark.asyncio
    async def test_reason_is_stored_as_provided(self, session):
        row = GlobalSuppression(email="b@example.com", reason="hard_bounce", source="send_failure")
        session.add(row)
        await session.flush()
        assert row.reason == "hard_bounce"
        assert row.source == "send_failure"


class TestAddSuppressionServiceLayer:
    @pytest.mark.asyncio
    async def test_add_suppression_creates_row(self, session):
        row, created = await add_suppression(session, "New@Example.com", reason="manual_block")
        assert created is True
        assert row.email == "new@example.com"  # normalized on write

    @pytest.mark.asyncio
    async def test_add_suppression_is_idempotent(self, session):
        row1, created1 = await add_suppression(session, "idem@example.com", reason="manual_block")
        row2, created2 = await add_suppression(session, "IDEM@Example.com", reason="complaint")
        assert created1 is True
        assert created2 is False
        assert row1.id == row2.id
        # First reason wins — a later automatic trigger must not rewrite history.
        assert row2.reason == "manual_block"

    @pytest.mark.asyncio
    async def test_add_suppression_rejects_empty_email(self, session):
        with pytest.raises(ValueError):
            await add_suppression(session, "   ", reason="manual_block")

    @pytest.mark.asyncio
    async def test_unknown_reason_falls_back_to_default(self, session):
        row, _ = await add_suppression(session, "weird@example.com", reason="not_a_real_reason")
        assert row.reason == "manual_block"

    @pytest.mark.asyncio
    async def test_is_globally_suppressed_true_and_false(self, session):
        await add_suppression(session, "yes@example.com")
        assert await is_globally_suppressed(session, "Yes@Example.com") is True
        assert await is_globally_suppressed(session, "no@example.com") is False

    @pytest.mark.asyncio
    async def test_get_all_suppressed_emails_returns_normalized_set(self, session):
        await add_suppression(session, "One@Example.com")
        await add_suppression(session, "two@example.com")
        emails = await get_all_suppressed_emails(session)
        assert emails == frozenset({"one@example.com", "two@example.com"})

    @pytest.mark.asyncio
    async def test_remove_suppression_deletes_row(self, session):
        await add_suppression(session, "remove-me@example.com")
        removed = await remove_suppression(session, "Remove-Me@Example.com")
        assert removed is True
        assert await is_globally_suppressed(session, "remove-me@example.com") is False

    @pytest.mark.asyncio
    async def test_remove_suppression_returns_false_when_absent(self, session):
        assert await remove_suppression(session, "never-added@example.com") is False

    @pytest.mark.asyncio
    async def test_list_suppressions_orders_newest_first(self, session):
        await add_suppression(session, "first@example.com")
        await add_suppression(session, "second@example.com")
        rows = await list_suppressions(session)
        emails_in_order = [r.email for r in rows]
        assert emails_in_order.index("second@example.com") < emails_in_order.index("first@example.com")

    @pytest.mark.asyncio
    async def test_get_suppression_returns_none_for_empty_email(self, session):
        assert await get_suppression(session, "") is None
