"""OUTBOUND-QUICKLY-0B — pre-setup backup/restore hardening.

Root cause: validate_custom_format_dump_file (app/backup_pg.py) only
checks an uploaded file IS a well-formed pg_dump, never that it came from
THIS instance, and restore-setup/{metadata,preview,execute} are reachable
without authentication by design (there's no admin yet). On a publicly
reachable deployment, that's a genuine pre-auth takeover vector: anyone
who reaches the URL before the real operator's first login can craft
their own dump and restore it. QUICKLY_RESTORE_SETUP_TOKEN (opt-in,
app/routers/auth.py::_check_restore_setup_token) closes that gap.

_check_restore_setup_token is tested directly (plain function, a fake
Request, env var control) — the reliable way to test this suite's
security-check logic without fighting TestClient/engine fixture
interactions (see the note in test_mcp_tools.py for why). One router-level
test confirms the check is actually wired into an endpoint and runs
before the existing checks.
"""
from __future__ import annotations

import pytest
from starlette.requests import Request

from app.routers.auth import _check_restore_setup_token


def _fake_request(headers: dict[str, str]) -> Request:
    raw_headers = [(k.lower().encode(), v.encode()) for k, v in headers.items()]
    return Request(scope={"type": "http", "headers": raw_headers, "method": "POST", "path": "/api/auth/restore-setup/metadata"})


class TestCheckRestoreSetupToken:
    def test_unset_env_var_means_unchanged_behavior(self, monkeypatch):
        """Default (today's) behaviour: no token required at all."""
        monkeypatch.delenv("QUICKLY_RESTORE_SETUP_TOKEN", raising=False)
        _check_restore_setup_token(_fake_request({}))  # must not raise

    def test_set_env_var_rejects_missing_header(self, monkeypatch):
        monkeypatch.setenv("QUICKLY_RESTORE_SETUP_TOKEN", "correct-horse-battery-staple")
        with pytest.raises(Exception) as exc_info:
            _check_restore_setup_token(_fake_request({}))
        assert getattr(exc_info.value, "status_code", None) == 403

    def test_set_env_var_rejects_wrong_header(self, monkeypatch):
        monkeypatch.setenv("QUICKLY_RESTORE_SETUP_TOKEN", "correct-horse-battery-staple")
        with pytest.raises(Exception) as exc_info:
            _check_restore_setup_token(_fake_request({"X-Restore-Setup-Token": "wrong-guess"}))
        assert getattr(exc_info.value, "status_code", None) == 403

    def test_set_env_var_accepts_matching_header(self, monkeypatch):
        monkeypatch.setenv("QUICKLY_RESTORE_SETUP_TOKEN", "correct-horse-battery-staple")
        _check_restore_setup_token(
            _fake_request({"X-Restore-Setup-Token": "correct-horse-battery-staple"})
        )  # must not raise

    def test_set_env_var_rejects_empty_header(self, monkeypatch):
        """An explicitly empty header must not be treated as 'no token
        required' — only the unset env var does that."""
        monkeypatch.setenv("QUICKLY_RESTORE_SETUP_TOKEN", "correct-horse-battery-staple")
        with pytest.raises(Exception) as exc_info:
            _check_restore_setup_token(_fake_request({"X-Restore-Setup-Token": ""}))
        assert getattr(exc_info.value, "status_code", None) == 403


class TestRestoreSetupEndpointWiring:
    @pytest.mark.asyncio
    async def test_endpoint_enforces_token_before_any_other_check(self, monkeypatch):
        """Confirm the check actually runs inside the real endpoint, and
        runs FIRST — the 403 must be the token message, not the generic
        'setup already complete' one, regardless of DB state. Uses httpx
        directly against the ASGI app (no TestClient/engine fixture — see
        test_mcp_tools.py's note on why)."""
        import httpx

        from app.main import app as fastapi_app

        monkeypatch.setenv("QUICKLY_RESTORE_SETUP_TOKEN", "router-wiring-test-token")

        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=fastapi_app), base_url="http://testserver") as client:
            resp = await client.post(
                "/api/auth/restore-setup/metadata",
                files={"file": ("backup.qbk", b"not a real backup file", "application/octet-stream")},
            )
        assert resp.status_code == 403
        assert "X-Restore-Setup-Token" in resp.json()["detail"]
