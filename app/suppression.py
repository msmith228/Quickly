"""Global suppression / do-not-contact service layer (OUTBOUND-SAFETY-0A).

Single source of truth for reading and writing ``GlobalSuppression`` rows.
REST (app/routers/suppressions.py) and MCP (app/mcp_leads.py) both call into
this module rather than touching the table directly, and so does every
internal safety check (send-fire-time in app/jobs.py via
app/campaign_lead_status.py, enrollment-time in app/routers/campaigns.py,
automatic suppression on unsubscribe/hard-bounce).

Authoritative rule: if an email is in this table, Quickly must not send to
it, under any circumstance — independent of any CampaignLead enrollment
state. See app/campaign_lead_status.py::campaign_lead_may_receive_sends for
the actual send-time gate.
"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import GlobalSuppression

# Suggested reasons (STEP 2). Not a DB-level enum — plain string, validated
# here, matching the project's existing convention for enrollment_status /
# interest_status (see app/campaign_lead_status.py).
SUPPRESSION_REASONS = frozenset(
    {"unsubscribed", "complaint", "manual_block", "customer", "do_not_contact", "hard_bounce"}
)
DEFAULT_REASON = "manual_block"


def normalize_email(email: str) -> str:
    """Canonical suppression key: strip whitespace, lowercase.

    Deliberately does NOT do Gmail-style plus-address or dot-insensitive
    rewriting (STEP 14) — those are provider-specific and risky to guess
    wrong; case + whitespace differences are the only universally safe
    normalization for arbitrary email addresses.
    """
    return (email or "").strip().lower()


def normalize_reason(reason: str | None) -> str:
    r = (reason or "").strip().lower()
    return r if r in SUPPRESSION_REASONS else DEFAULT_REASON


async def get_suppression(db: AsyncSession, email: str) -> GlobalSuppression | None:
    norm = normalize_email(email)
    if not norm:
        return None
    result = await db.execute(select(GlobalSuppression).where(GlobalSuppression.email == norm))
    return result.scalar_one_or_none()


async def is_globally_suppressed(db: AsyncSession, email: str) -> bool:
    return await get_suppression(db, email) is not None


async def get_all_suppressed_emails(db: AsyncSession) -> frozenset[str]:
    """Batch-load every suppressed email as a set, for hot-loop callers
    (the send job iterates many queue slots per tick) that would otherwise
    issue one query per row. Callers must re-fetch per job run/request —
    this is a point-in-time snapshot, not a cache."""
    result = await db.execute(select(GlobalSuppression.email))
    return frozenset(result.scalars().all())


async def add_suppression(
    db: AsyncSession,
    email: str,
    reason: str | None = None,
    note: str | None = None,
    source: str | None = None,
) -> tuple[GlobalSuppression, bool]:
    """Idempotent add. Returns (row, created). If already suppressed, the
    existing row is returned UNCHANGED (reason/note/source are not
    overwritten) — the first suppression reason wins; removal must be
    explicit (STEP 11), so a second automatic trigger (e.g. a bounce after
    an unsubscribe) must never silently rewrite history."""
    norm = normalize_email(email)
    if not norm:
        raise ValueError("email must not be empty")

    existing = await get_suppression(db, norm)
    if existing:
        return existing, False

    row = GlobalSuppression(
        email=norm,
        reason=normalize_reason(reason),
        note=note,
        source=source,
    )
    db.add(row)
    try:
        await db.flush()
    except IntegrityError:
        # Lost a race with a concurrent inserter for the same normalized
        # email — the unique constraint is the real guarantee; fall back to
        # returning the row that won.
        await db.rollback()
        existing = await get_suppression(db, norm)
        if existing:
            return existing, False
        raise
    return row, True


async def remove_suppression(db: AsyncSession, email: str) -> bool:
    """Explicit removal only (STEP 11) — nothing else in the codebase may
    delete a GlobalSuppression row. Returns True if a row was removed."""
    existing = await get_suppression(db, email)
    if not existing:
        return False
    await db.delete(existing)
    await db.flush()
    return True


async def list_suppressions(
    db: AsyncSession, limit: int = 100, offset: int = 0
) -> list[GlobalSuppression]:
    result = await db.execute(
        select(GlobalSuppression)
        .order_by(GlobalSuppression.created_at.desc(), GlobalSuppression.id.desc())
        .limit(limit)
        .offset(offset)
    )
    return list(result.scalars().all())
