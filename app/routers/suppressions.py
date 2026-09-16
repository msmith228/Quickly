"""Global suppression / do-not-contact REST API (OUTBOUND-SAFETY-0A).

Thin HTTP layer over app/suppression.py — no business logic lives here, and
nothing here touches app.models.GlobalSuppression directly. Auth is applied
at router-registration time in app/main.py (dependencies=_auth_deps), same
as every other authenticated router — there is no unauthenticated mutation
path.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession
from fastapi import Depends

from app.database import get_db
from app.schemas import SuppressionCheckResponse, SuppressionCreate, SuppressionResponse
from app.suppression import (
    add_suppression,
    get_suppression,
    list_suppressions,
    remove_suppression,
)

log = logging.getLogger("quickly.suppression")

router = APIRouter(prefix="/api/suppressions", tags=["suppressions"])


@router.get("", response_model=list[SuppressionResponse])
async def list_global_suppressions(
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    db: AsyncSession = Depends(get_db),
):
    return await list_suppressions(db, limit=limit, offset=offset)


@router.get("/check", response_model=SuppressionCheckResponse)
async def check_global_suppression(
    email: str = Query(..., description="Email address to check"),
    db: AsyncSession = Depends(get_db),
):
    row = await get_suppression(db, email)
    return SuppressionCheckResponse(email=email, suppressed=row is not None, suppression=row)


@router.post("", response_model=SuppressionResponse, status_code=201)
async def add_global_suppression(payload: SuppressionCreate, db: AsyncSession = Depends(get_db)):
    try:
        row, created = await add_suppression(
            db, str(payload.email), reason=payload.reason, note=payload.note, source=payload.source or "api"
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    if created:
        log.info("Global suppression added: reason=%s source=%s", row.reason, row.source)
    return row


@router.delete("/{email}")
async def remove_global_suppression(email: str, db: AsyncSession = Depends(get_db)):
    removed = await remove_suppression(db, email)
    if not removed:
        raise HTTPException(404, "No suppression found for that email")
    log.info("Global suppression removed (explicit request)")
    return {"ok": True, "email": email.strip().lower()}
