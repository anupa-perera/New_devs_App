from datetime import datetime, timezone as dt_timezone
from decimal import Decimal
from typing import Dict, Any
from zoneinfo import ZoneInfo


async def calculate_monthly_revenue(
    property_id: str, tenant_id: str, month: int, year: int
) -> Dict[str, Any]:
    """
    Calculates revenue for a specific calendar month, in the property's own timezone.

    Properties span multiple timezones (Paris, New York, ...). A reservation whose
    check-in is, say, 2024-02-29 23:30 UTC is already 2024-03-01 00:30 local time in a
    UTC+1 property — it belongs to March locally even though it's still February in UTC.
    Bucketing by naive UTC month boundaries silently moves such reservations into the
    wrong month, which is exactly the kind of discrepancy clients notice in their totals.
    """
    from app.core.database_pool import db_pool
    from sqlalchemy import text

    await db_pool.initialize()

    async with db_pool.session_factory() as session:
        tz_result = await session.execute(
            text("SELECT timezone FROM properties WHERE id = :property_id AND tenant_id = :tenant_id"),
            {"property_id": property_id, "tenant_id": tenant_id},
        )
        tz_row = tz_result.fetchone()
        if not tz_row:
            raise ValueError(f"Property {property_id} not found for tenant {tenant_id}")

        property_tz = ZoneInfo(tz_row.timezone)

        start_local = datetime(year, month, 1, tzinfo=property_tz)
        if month < 12:
            end_local = datetime(year, month + 1, 1, tzinfo=property_tz)
        else:
            end_local = datetime(year + 1, 1, 1, tzinfo=property_tz)

        # Convert the local-time month boundaries to UTC to compare against
        # check_in_date, which Postgres stores as TIMESTAMP WITH TIME ZONE (UTC).
        start_utc = start_local.astimezone(dt_timezone.utc)
        end_utc = end_local.astimezone(dt_timezone.utc)

        result = await session.execute(
            text("""
                SELECT SUM(total_amount) as total_revenue, COUNT(*) as reservation_count
                FROM reservations
                WHERE property_id = :property_id AND tenant_id = :tenant_id
                AND check_in_date >= :start_utc AND check_in_date < :end_utc
            """),
            {
                "property_id": property_id,
                "tenant_id": tenant_id,
                "start_utc": start_utc,
                "end_utc": end_utc,
            },
        )
        row = result.fetchone()

        if row and row.total_revenue is not None:
            total_revenue = Decimal(str(row.total_revenue))
            count = row.reservation_count
        else:
            total_revenue = Decimal("0")
            count = 0

        return {
            "property_id": property_id,
            "tenant_id": tenant_id,
            "total": str(total_revenue),
            "currency": "USD",
            "count": count,
        }

async def calculate_total_revenue(property_id: str, tenant_id: str) -> Dict[str, Any]:
    """
    Aggregates revenue from database.
    """
    try:
        # Use the shared, app-lifetime connection pool (initialized once at startup).
        from app.core.database_pool import db_pool

        # Idempotent: no-op if the startup hook already initialized the pool.
        await db_pool.initialize()

        async with db_pool.session_factory() as session:
            # Use SQLAlchemy text for raw SQL
            from sqlalchemy import text

            query = text("""
                SELECT
                    property_id,
                    SUM(total_amount) as total_revenue,
                    COUNT(*) as reservation_count
                FROM reservations
                WHERE property_id = :property_id AND tenant_id = :tenant_id
                GROUP BY property_id
            """)

            result = await session.execute(query, {
                "property_id": property_id,
                "tenant_id": tenant_id
            })
            row = result.fetchone()

            if row:
                total_revenue = Decimal(str(row.total_revenue))
                return {
                    "property_id": property_id,
                    "tenant_id": tenant_id,
                    "total": str(total_revenue),
                    "currency": "USD",
                    "count": row.reservation_count
                }
            else:
                # No reservations found for this property
                return {
                    "property_id": property_id,
                    "tenant_id": tenant_id,
                    "total": "0.00",
                    "currency": "USD",
                    "count": 0
                }

    except Exception as e:
        # No fabricated fallback. Presenting invented financial figures as authoritative
        # is worse than an error, so surface the failure instead of masking it.
        print(f"Database error for {property_id} (tenant: {tenant_id}): {e}")
        raise
