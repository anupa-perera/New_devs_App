from decimal import Decimal, ROUND_HALF_UP
from typing import Dict, Any, Optional
from fastapi import APIRouter, Depends, HTTPException
from app.services.cache import get_revenue_summary
from app.services.reservations import calculate_monthly_revenue
from app.core.auth import authenticate_request as get_current_user

router = APIRouter()

@router.get("/dashboard/summary")
async def get_dashboard_summary(
    property_id: str,
    month: Optional[int] = None,
    year: Optional[int] = None,
    current_user: dict = Depends(get_current_user)
) -> Dict[str, Any]:

    # Never collapse an unresolved tenant into a shared "default_tenant" bucket — that is
    # itself a cross-tenant leak path (every tenant-less request would share one cache
    # entry and one dataset). No tenant resolved => no data.
    tenant_id = getattr(current_user, "tenant_id", None)
    if not tenant_id:
        raise HTTPException(status_code=403, detail="No tenant associated with this account")

    # month/year are optional: omitting them preserves the existing all-time, cached
    # behavior. Supplying both switches to a timezone-aware monthly breakdown, computed
    # fresh each time (not cached — the leak fixed above was specific to the all-time key).
    if month is not None and year is not None:
        revenue_data = await calculate_monthly_revenue(property_id, tenant_id, month, year)
    else:
        revenue_data = await get_revenue_summary(property_id, tenant_id)

    # Money stays a Decimal serialized as a string — never a binary float — so the
    # NUMERIC(10,3) sub-cent precision survives to the client intact.
    total_revenue = str(
        Decimal(revenue_data['total']).quantize(Decimal("0.001"), rounding=ROUND_HALF_UP)
    )

    return {
        "property_id": revenue_data['property_id'],
        "total_revenue": total_revenue,
        "currency": revenue_data['currency'],
        "reservations_count": revenue_data['count']
    }
