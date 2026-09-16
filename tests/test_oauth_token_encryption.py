"""OUTBOUND-QUICKLY-0B — OAuth token encryption-at-rest.

Covers: GmailAccount/Office365Account/User (notif_*) token columns moved
from plain Text to EncryptedText; the one-time plaintext-backfill migration
(encrypt_plaintext_oauth_tokens); and a regression check that SMTP password
encryption (the pre-existing EncryptedText usage) and API serialization
still behave correctly. No existing test in this suite activated real
encryption before (none referenced QUICKLY_ENCRYPTION_KEY/init_encryption),
so the SMTP-password coverage here is itself new, closing a real gap.
"""
from __future__ import annotations

import pytest
import pytest_asyncio
from sqlalchemy import select, text

import app.security as security
from app.database import encrypt_plaintext_oauth_tokens
from app.models import GmailAccount, Office365Account, SmtpAccount, User
from tests.conftest import make_inbox


@pytest.fixture
def active_encryption():
    """Activate real Fernet encryption for one test; restore whatever state
    (usually disabled) preceded it afterward. Module-global state in
    app.security — must never leak into other tests in the suite.
    """
    previous = security._fernet
    security.init_encryption(security.generate_encryption_key())
    yield
    security._fernet = previous


async def _raw_column(session, table: str, column: str, row_id: int) -> str | None:
    """Read a column's raw stored value, bypassing the ORM's EncryptedText
    decrypt-on-read — the only way to see what's actually on disk."""
    result = await session.execute(
        text(f"SELECT {column} FROM {table} WHERE id = :id"), {"id": row_id}
    )
    row = result.first()
    return row[0] if row else None


async def _reload(session, model, row_id: int):
    """Re-fetch a row through the ORM via a plain select() — session.get()
    triggers a MissingGreenlet error under this suite's aiosqlite session
    fixture (its flush/commit are bridged through run_sync); select() is
    the pattern the rest of this test suite already uses safely."""
    session.expire_all()
    result = await session.execute(select(model).where(model.id == row_id))
    return result.scalar_one()


# ---------------------------------------------------------------------------
# Microsoft 365 (Office365Account)
# ---------------------------------------------------------------------------


class TestOffice365AccountEncryption:
    @pytest.mark.asyncio
    async def test_token_written_through_model_is_encrypted_at_rest(self, session, active_encryption):
        inbox = await make_inbox(session, email="o365@test.com", provider="office365")
        acct = Office365Account(
            inbox_id=inbox.id,
            microsoft_email="agent@contoso.com",
            access_token="plaintext-access-abc123",
            refresh_token="plaintext-refresh-xyz789",
        )
        session.add(acct)
        await session.flush()

        raw_access = await _raw_column(session, "office365_account", "access_token", acct.id)
        raw_refresh = await _raw_column(session, "office365_account", "refresh_token", acct.id)
        assert raw_access != "plaintext-access-abc123"
        assert raw_refresh != "plaintext-refresh-xyz789"
        assert security.is_encrypted(raw_access)
        assert security.is_encrypted(raw_refresh)

    @pytest.mark.asyncio
    async def test_model_read_returns_original_plaintext(self, session, active_encryption):
        inbox = await make_inbox(session, email="o365b@test.com", provider="office365")
        acct = Office365Account(
            inbox_id=inbox.id,
            microsoft_email="agent2@contoso.com",
            access_token="round-trip-access",
            refresh_token="round-trip-refresh",
        )
        session.add(acct)
        await session.flush()
        reloaded = await _reload(session, Office365Account, acct.id)
        assert reloaded.access_token == "round-trip-access"
        assert reloaded.refresh_token == "round-trip-refresh"

    @pytest.mark.asyncio
    async def test_refresh_token_update_remains_encrypted(self, session, active_encryption):
        inbox = await make_inbox(session, email="o365c@test.com", provider="office365")
        acct = Office365Account(
            inbox_id=inbox.id,
            microsoft_email="agent3@contoso.com",
            access_token="old-access",
            refresh_token="old-refresh",
        )
        session.add(acct)
        await session.flush()

        acct.access_token = "new-access-after-refresh"
        acct.refresh_token = "new-refresh-after-rotation"
        await session.flush()

        raw_access = await _raw_column(session, "office365_account", "access_token", acct.id)
        raw_refresh = await _raw_column(session, "office365_account", "refresh_token", acct.id)
        assert security.is_encrypted(raw_access)
        assert security.is_encrypted(raw_refresh)
        assert raw_access != "new-access-after-refresh"
        assert raw_refresh != "new-refresh-after-rotation"

        reloaded = await _reload(session, Office365Account, acct.id)
        assert reloaded.access_token == "new-access-after-refresh"
        assert reloaded.refresh_token == "new-refresh-after-rotation"


# ---------------------------------------------------------------------------
# Gmail (GmailAccount) — same coverage
# ---------------------------------------------------------------------------


class TestGmailAccountEncryption:
    @pytest.mark.asyncio
    async def test_token_written_through_model_is_encrypted_at_rest(self, session, active_encryption):
        inbox = await make_inbox(session, email="gmail@test.com", provider="gmail")
        acct = GmailAccount(
            inbox_id=inbox.id,
            google_email="agent@gmail.com",
            access_token="plaintext-gmail-access",
            refresh_token="plaintext-gmail-refresh",
        )
        session.add(acct)
        await session.flush()

        raw_access = await _raw_column(session, "gmail_account", "access_token", acct.id)
        raw_refresh = await _raw_column(session, "gmail_account", "refresh_token", acct.id)
        assert raw_access != "plaintext-gmail-access"
        assert raw_refresh != "plaintext-gmail-refresh"
        assert security.is_encrypted(raw_access)
        assert security.is_encrypted(raw_refresh)

    @pytest.mark.asyncio
    async def test_model_read_returns_original_plaintext(self, session, active_encryption):
        inbox = await make_inbox(session, email="gmailb@test.com", provider="gmail")
        acct = GmailAccount(
            inbox_id=inbox.id,
            google_email="agent2@gmail.com",
            access_token="gmail-round-trip-access",
            refresh_token="gmail-round-trip-refresh",
        )
        session.add(acct)
        await session.flush()
        reloaded = await _reload(session, GmailAccount, acct.id)
        assert reloaded.access_token == "gmail-round-trip-access"
        assert reloaded.refresh_token == "gmail-round-trip-refresh"

    @pytest.mark.asyncio
    async def test_refresh_token_update_remains_encrypted(self, session, active_encryption):
        inbox = await make_inbox(session, email="gmailc@test.com", provider="gmail")
        acct = GmailAccount(
            inbox_id=inbox.id,
            google_email="agent3@gmail.com",
            access_token="old-gmail-access",
            refresh_token="old-gmail-refresh",
        )
        session.add(acct)
        await session.flush()

        acct.access_token = "rotated-gmail-access"
        await session.flush()

        raw_access = await _raw_column(session, "gmail_account", "access_token", acct.id)
        assert security.is_encrypted(raw_access)
        assert raw_access != "rotated-gmail-access"


# ---------------------------------------------------------------------------
# User.notif_access_token / notif_refresh_token — found via the "any other
# provider secrets stored as plaintext?" audit; same fix, same coverage.
# ---------------------------------------------------------------------------


class TestUserNotificationTokenEncryption:
    @pytest.mark.asyncio
    async def test_notif_tokens_encrypted_at_rest_and_round_trip(self, session, active_encryption):
        user = User(
            username="notifuser",
            email="notifuser@test.com",
            password_hash=None,
            notif_access_token="notif-access-plain",
            notif_refresh_token="notif-refresh-plain",
        )
        session.add(user)
        await session.flush()

        raw_access = await _raw_column(session, "app_user", "notif_access_token", user.id)
        assert raw_access != "notif-access-plain"
        assert security.is_encrypted(raw_access)

        reloaded = await _reload(session, User, user.id)
        assert reloaded.notif_access_token == "notif-access-plain"
        assert reloaded.notif_refresh_token == "notif-refresh-plain"

    @pytest.mark.asyncio
    async def test_notif_tokens_null_handling(self, session, active_encryption):
        """nullable=True — a user who never connected notification email
        must round-trip as NULL, not an encrypted empty string or crash."""
        user = User(username="nonotifuser", email="nonotif@test.com", password_hash="x")
        session.add(user)
        await session.flush()

        raw_access = await _raw_column(session, "app_user", "notif_access_token", user.id)
        assert raw_access is None

        reloaded = await _reload(session, User, user.id)
        assert reloaded.notif_access_token is None
        assert reloaded.notif_refresh_token is None


# ---------------------------------------------------------------------------
# NULL handling for the OAuth-account tables themselves (token_expiry etc.
# — access_token/refresh_token are NOT NULL on both tables, so the
# NULL-safety requirement lands on notif_* above; this confirms the
# EncryptedText type itself is NULL-safe independent of column nullability).
# ---------------------------------------------------------------------------


def test_encrypted_text_type_is_null_safe_independent_of_column():
    assert security.encrypt(None) is None  # type: ignore[arg-type]
    assert security.decrypt(None) is None  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Migration: encrypt_plaintext_oauth_tokens
# ---------------------------------------------------------------------------


class TestEncryptPlaintextOAuthTokensMigration:
    @pytest.mark.asyncio
    async def test_refuses_to_run_without_active_encryption(self, session):
        """Self-contained: force encryption inactive regardless of what any
        other test left behind, then restore whatever was there after."""
        previous = security._fernet
        security._fernet = None
        try:
            assert not security.is_encryption_active()
            with pytest.raises(RuntimeError):
                await encrypt_plaintext_oauth_tokens(session)
        finally:
            security._fernet = previous

    @pytest.mark.asyncio
    async def test_encrypts_existing_plaintext_rows_exactly_once(self, session, active_encryption):
        inbox = await make_inbox(session, email="migrate@test.com", provider="gmail")
        await session.flush()

        # Insert a plaintext row via raw SQL, bypassing the ORM's
        # EncryptedText encrypt-on-write — simulating a real row written
        # before this migration existed (i.e. before the column type
        # changed from Text to EncryptedText).
        await session.execute(
            text(
                "INSERT INTO gmail_account (inbox_id, google_email, access_token, refresh_token) "
                "VALUES (:inbox_id, :email, :access, :refresh)"
            ),
            {
                "inbox_id": inbox.id,
                "email": "migrate@gmail.com",
                "access": "legacy-plaintext-access",
                "refresh": "legacy-plaintext-refresh",
            },
        )
        await session.flush()

        row_id = (
            await session.execute(
                text("SELECT id FROM gmail_account WHERE google_email = :e"),
                {"e": "migrate@gmail.com"},
            )
        ).scalar_one()

        raw_before = await _raw_column(session, "gmail_account", "access_token", row_id)
        assert raw_before == "legacy-plaintext-access"  # confirms the raw-INSERT bypass worked

        counts = await encrypt_plaintext_oauth_tokens(session)
        assert counts.get("gmail_account.access_token", 0) >= 1
        assert counts.get("gmail_account.refresh_token", 0) >= 1

        raw_after = await _raw_column(session, "gmail_account", "access_token", row_id)
        assert raw_after != "legacy-plaintext-access"
        assert security.is_encrypted(raw_after)

        # Reading it back through the ORM decrypts transparently.
        migrated_row = await _reload(session, GmailAccount, row_id)
        assert migrated_row.access_token == "legacy-plaintext-access"
        assert migrated_row.refresh_token == "legacy-plaintext-refresh"

    @pytest.mark.asyncio
    async def test_running_twice_does_not_double_encrypt(self, session, active_encryption):
        inbox = await make_inbox(session, email="migrate2@test.com", provider="office365")
        await session.execute(
            text(
                "INSERT INTO office365_account (inbox_id, microsoft_email, access_token, refresh_token) "
                "VALUES (:inbox_id, :email, :access, :refresh)"
            ),
            {
                "inbox_id": inbox.id,
                "email": "migrate2@contoso.com",
                "access": "legacy-o365-access",
                "refresh": "legacy-o365-refresh",
            },
        )
        await session.flush()
        row_id = (
            await session.execute(
                text("SELECT id FROM office365_account WHERE microsoft_email = :e"),
                {"e": "migrate2@contoso.com"},
            )
        ).scalar_one()

        first_counts = await encrypt_plaintext_oauth_tokens(session)
        assert first_counts.get("office365_account.access_token", 0) >= 1
        raw_once = await _raw_column(session, "office365_account", "access_token", row_id)

        second_counts = await encrypt_plaintext_oauth_tokens(session)
        assert second_counts.get("office365_account.access_token", 0) == 0
        raw_twice = await _raw_column(session, "office365_account", "access_token", row_id)

        # Same ciphertext — not re-encrypted (double-encryption would
        # produce a different Fernet token each time even for the same
        # input, since Fernet includes a random IV/timestamp).
        assert raw_once == raw_twice

        row = await _reload(session, Office365Account, row_id)
        assert row.access_token == "legacy-o365-access"

    @pytest.mark.asyncio
    async def test_null_values_stay_null(self, session, active_encryption):
        user = User(username="migrateuser", email="migrateuser@test.com", password_hash="x")
        session.add(user)
        await session.flush()
        assert user.notif_access_token is None

        counts = await encrypt_plaintext_oauth_tokens(session)
        assert "app_user.notif_access_token" not in counts or counts["app_user.notif_access_token"] == 0

        raw = await _raw_column(session, "app_user", "notif_access_token", user.id)
        assert raw is None

    @pytest.mark.asyncio
    async def test_already_encrypted_rows_are_left_alone(self, session, active_encryption):
        """A row created AFTER the model change (via the ORM, so it's
        already encrypted) must not be touched by the migration."""
        inbox = await make_inbox(session, email="already@test.com", provider="gmail")
        acct = GmailAccount(
            inbox_id=inbox.id,
            google_email="already@gmail.com",
            access_token="fresh-access",
            refresh_token="fresh-refresh",
        )
        session.add(acct)
        await session.flush()
        raw_before = await _raw_column(session, "gmail_account", "access_token", acct.id)

        counts = await encrypt_plaintext_oauth_tokens(session)
        assert counts.get("gmail_account.access_token", 0) == 0

        raw_after = await _raw_column(session, "gmail_account", "access_token", acct.id)
        assert raw_after == raw_before


# ---------------------------------------------------------------------------
# Regression: SMTP password encryption (pre-existing EncryptedText usage —
# no test in this suite previously activated real encryption to prove it).
# ---------------------------------------------------------------------------


class TestSmtpPasswordEncryptionRegression:
    @pytest.mark.asyncio
    async def test_smtp_password_encrypted_at_rest_and_round_trips(self, session, active_encryption):
        inbox = await make_inbox(session, email="smtp@test.com", provider="smtp")
        acct = SmtpAccount(
            inbox_id=inbox.id,
            smtp_host="smtp.example.com",
            smtp_username="relay-user",
            smtp_password="super-secret-smtp-password",
            imap_password="super-secret-imap-password",
        )
        session.add(acct)
        await session.flush()

        raw_smtp = await _raw_column(session, "smtp_account", "smtp_password", acct.id)
        raw_imap = await _raw_column(session, "smtp_account", "imap_password", acct.id)
        assert raw_smtp != "super-secret-smtp-password"
        assert raw_imap != "super-secret-imap-password"
        assert security.is_encrypted(raw_smtp)
        assert security.is_encrypted(raw_imap)

        reloaded = await _reload(session, SmtpAccount, acct.id)
        assert reloaded.smtp_password == "super-secret-smtp-password"
        assert reloaded.imap_password == "super-secret-imap-password"


# ---------------------------------------------------------------------------
# Regression: API/serialization never exposes provider tokens.
# ---------------------------------------------------------------------------


def test_inbox_response_schema_has_no_token_fields():
    """InboxResponse is the explicit allowlist GET /api/inboxes serializes
    through — assert no OAuth/SMTP secret field name is in it, so a future
    accidental `model_config = {"from_attributes": True}` addition of a
    relationship can't silently leak a token via a schema change alone."""
    from app.schemas import InboxResponse

    field_names = set(InboxResponse.model_fields.keys())
    leaky_names = {
        "access_token", "refresh_token", "gmail_account", "office365_account",
        "smtp_account", "smtp_password", "imap_password", "client_secret",
    }
    assert not (field_names & leaky_names), f"InboxResponse exposes: {field_names & leaky_names}"
