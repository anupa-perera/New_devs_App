from fastapi import APIRouter, Depends, HTTPException
from typing import Dict, Any, List
from sqlalchemy import text
from app.core.auth import authenticate_request as get_current_user
from app.core.database_pool import db_pool

router = APIRouter()


@router.get("/properties")
async def list_properties(current_user: dict = Depends(get_current_user)) -> Dict[str, Any]:
    """List properties belonging to the authenticated user's tenant only.

    Property IDs are shared across tenants (e.g. prop-001 is a different property
    for each tenant), so this must always filter by tenant_id — never return the
    global property list.
    """
    tenant_id = getattr(current_user, "tenant_id", None)
    if not tenant_id:
        raise HTTPException(status_code=403, detail="No tenant associated with this account")

    await db_pool.initialize()

    async with db_pool.session_factory() as session:
        result = await session.execute(
            text("SELECT id, name FROM properties WHERE tenant_id = :tenant_id ORDER BY id"),
            {"tenant_id": tenant_id},
        )
        rows = result.fetchall()

    properties: List[Dict[str, str]] = [{"id": row.id, "name": row.name} for row in rows]
    return {"items": properties}
