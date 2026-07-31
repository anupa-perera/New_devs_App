# PropertyFlow — Debug Report

Investigation of three client/finance complaints on the Property Revenue Dashboard. Each section:
**Symptom → How I found it → Root cause (`file:line`) → The fix → Verified by** (actual observed values).

## Ground truth (from `database/seed.sql`, confirmed via `psql`)

```
 tenant_id | property_id | count |   sum
-----------+-------------+-------+----------
 tenant-a  | prop-001    |     4 | 2250.000
 tenant-a  | prop-002    |     4 | 4975.500
 tenant-a  | prop-003    |     2 | 6100.500
 tenant-b  | prop-004    |     4 | 1776.500
 tenant-b  | prop-005    |     3 | 3256.000
```
`prop-001` is the only property that exists under **both** tenants (tenant-a `2250.000/4`; tenant-b has
**no** reservations → `0.000/0`). It is the single discriminating property — every verification uses it.

**Baseline (before any fix):** `GET /api/v1/dashboard/summary?property_id=prop-001` returned
`{total_revenue: 1000.0, reservations_count: 3}` for **both** Sunset (tenant-a) and Ocean (tenant-b) —
identical, tenant-blind, and matching none of the real numbers. Proof the app never read the database.

---

## Bug 1 — The dashboard never read the database `[P0, master defect]`

**Symptom:** Every property showed the same figures for every client; `prop-001` read `1000.00 / 3` for
both Sunset and Ocean, matching neither tenant's real data.

**How I found it:** Traced the response back `dashboard.py → cache.py → reservations.py` and hit a
hardcoded dict (`reservations.py:93`). Grepped `supabase_db_user` — referenced in exactly one place,
defined in none.

**Root cause:** `database_pool.py:18` built its DSN from `settings.supabase_db_user/password/host/port/
name`, none of which exist on `Settings` (`config.py`). The `AttributeError` was swallowed by the bare
`except` at `:38` → `session_factory = None` → `reservations.py:46` falsy → `:86` raise → `:88` except →
**mock data returned** (`:93`). The correct DSN already existed: `settings.database_url`.

**The fix:** point `initialize()` at `settings.database_url` (rewriting the scheme to
`postgresql+asyncpg://`), drop the invalid `poolclass=QueuePool` (async engines pick
`AsyncAdaptedQueuePool`), let init failure **propagate** instead of nulling the factory, and **delete the
mock fallback** so the service returns real data or fails loudly.

**Verified by:** After the fix, `prop-001` no longer returns the mock `1000.00 / 3` for either tenant.
The endpoint now reaches the database and returns **HTTP 500 (fail-loud)** because of the latent async
bug fixed next (Bug 2). The traceback confirms execution flows through the real query path
(`dashboard.py:16 → cache.py:24 → reservations.py:47`), not the deleted mock. Real per-tenant figures
(`2250.000 / 4`, `0.000 / 0`) and the definitive "DB stopped → 500, never a number" check land with
Bug 2, once the pool's sessions work.

---

## Bug 2 — Async session handling `[P0, blocker]`

**Symptom:** With Bug 1 fixed, every dashboard request returned HTTP 500.

**How I found it:** The 500 traceback pointed straight at `reservations.py:47` —
`TypeError: 'coroutine' object does not support the asynchronous context manager protocol`.

**Root cause:** two issues in `calculate_total_revenue`:
- `db_pool.get_session()` is `async def` and *returns* a session, so `async with db_pool.get_session()`
  tried to enter a **coroutine** (no `__aenter__`) → `TypeError`.
- `db_pool = DatabasePool()` built a **new pool/engine per request**, ignoring the module-level singleton
  and leaking connections. The same coroutine bug also sat in the `get_db_session` dependency.

**The fix:** use `async with db_pool.session_factory() as session`; import the module-level `db_pool`
singleton; make `initialize()` idempotent and run it once from the `main.py` startup hook; fix
`get_db_session` the same way.

**Verified by:** `prop-001` → Sunset `2250.000 / 4`, Ocean (queried first, uncached) `0.000 / 0`;
prop-002 `4975.50 / 4`, prop-003 `6100.50 / 2` — all matching the `psql` ground truth. No coroutine error.
Postgres connections stayed flat at **2** across repeated requests (no per-request engine leak). Startup
log shows `✅ Application database pool initialized`. Stopping the DB and querying an uncached property
returned **HTTP 500**, never a fabricated number — this also confirms Bug 1's fail-loud gate.

> Observed en route: Sunset-first made Ocean see Sunset's `2250.000`, and Ocean-first made Sunset see
> `0.000`. That order-dependent cross-tenant bleed is **Bug 3**, reproduced and fixed next.

## Bug 3 — Cross-tenant cache leak `[P0, privacy]`
_(pending)_

## Bug 4 — Money precision `[P1]`
_(pending)_

## Bug 5 — Timezone month boundaries `[P1]`
_(pending)_

## Bug 6 — Property selector not tenant-scoped `[P2]`
_(pending)_

## Out of scope (reported, not fixed)
- `schema.sql:35-36` enables RLS on `properties`/`reservations` but defines **zero policies**, and the
  app connects as superuser `postgres` → RLS is bypassed entirely. Policies are a design change.
- `print()` debug statements (`reservations.py`) should be `logger` calls.
