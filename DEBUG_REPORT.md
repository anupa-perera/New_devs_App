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

**Symptom (Client B):** "Sometimes when we refresh the page, we see revenue numbers that look like they
belong to another company."

**How I found it:** "Sometimes on refresh" implied shared *state*, not logic. `cache.py:13` had no tenant
in the key, and `seed.sql:8-9` shows `prop-001` under both tenants. I reproduced it directly while
verifying Bug 2.

**Root cause:** `cache.py:13` — `cache_key = f"revenue:{property_id}"`. Whichever tenant loads a shared
property first populates `revenue:prop-001`; for the next 300s (the TTL) the other tenant gets a cache
hit and is served **that tenant's** revenue — the intermittency tracks the 5-minute TTL. Secondary path:
`dashboard.py:14` collapsed any unresolved tenant into one shared `"default_tenant"` bucket.

**The fix:** key by tenant — `revenue:{tenant_id}:{property_id}`; and reject requests with no resolved
tenant (403) rather than bucketing them together.

**Verified by (reproduced, then fixed):**
- *Before:* flush Redis → Sunset `prop-001` = `2250.000`, then Ocean `prop-001` = **`2250.000`** (should
  be `0`). Reverse order leaked the other way (Sunset saw Ocean's `0.000`). One key: `revenue:prop-001`.
- *After:* both orderings correct — Sunset `2250.000 / 4`, Ocean `0.000 / 0` — and Redis holds **two**
  keys: `revenue:tenant-a:prop-001` and `revenue:tenant-b:prop-001`.

## Bug 4 — Money precision `[P1]`

**Symptom (Finance):** Some revenue totals were "slightly off by a few cents."

**How I found it:** `total_amount` is `NUMERIC(10,3)` — deliberately sub-cent (`schema.sql:28`). Followed
the value out of the DB: `float()` at `dashboard.py:18`, then `Math.round(x*100)/100` +
`maximumFractionDigits: 2` at `RevenueSummary.tsx:64,81`. The seed rows `333.333 + 333.333 + 333.334`
(`seed.sql:24-26`) are shaped to expose it, and the UI even shipped a **"Precision Mismatch Detected"**
banner (`RevenueSummary.tsx:107`) — a built-in detector for this exact bug.

**Root cause:** two independent precision losses — the API cast the exact `Decimal` to a binary `float`
(`dashboard.py`), and the UI re-rounded to 2 decimals, discarding the schema's 3rd decimal.

**The fix:** keep `Decimal` end-to-end and serialize money as a **string**
(`quantize(Decimal("0.001"), ROUND_HALF_UP)`); type `total_revenue` as `string` on the client, drop the
`Math.round` round-trip, and group the integer part for display without ever touching a float.

**Verified by:** Raw API now returns quoted strings matching `psql` **char-for-char** —
`prop-001 "2250.000"`, `prop-002 "4975.500"`, `prop-003 "6100.500"` (previously floats like `4975.5`,
silently dropping the 3rd decimal). Browser confirms the same, logged in for real as each client:
Sunset's dashboard shows **`USD 2,250.000`** for prop-001; Ocean's shows **`USD 0.000`** — full 3-decimal
precision on screen, "Precision Mismatch Detected" banner removed (the loss it detected no longer
exists). The Ocean screenshot is also live proof Bug 3 is fixed under a real logged-in session, not just
a forged token — the property *label* still reads "Beach House Alpha" for Ocean's prop-001, which is the
separate, not-yet-fixed Bug 6 (hardcoded property list/names).

## Bug 5 — Timezone month boundaries `[P1]`

**Symptom (Client A):** "The revenue numbers on your dashboard don't match our internal records... we're
showing different totals for March."

**How I found it:** `properties.timezone` exists (`schema.sql:16`) and is populated (`Europe/Paris`,
`America/New_York` — `seed.sql:8-13`), but grepping the codebase shows it is **never read anywhere**. A
populated column no code touches is a strong bug signal.

**Root cause:** `calculate_monthly_revenue` (`reservations.py`) built month boundaries as naive
`datetime(year, month, 1)` — implicitly UTC midnight — while `check_in_date` is a real UTC timestamp and
properties are not in UTC. Proof row `res-tz-1`: check-in `2024-02-29 23:30:00+00` = **2024-03-01 00:30
Paris local**. For a Paris property this is a March booking; naive-UTC boundary math filed it under
February, silently dropping €1250 from Client A's March total. (The function was also dead code —
returned a hardcoded `Decimal('0')`, never called by the live endpoint.)

**The fix:** repair the function to load the property's own `timezone`, build month boundaries with
`zoneinfo.ZoneInfo` in that timezone, convert to UTC only for the query, and run the real aggregate
against the database. Wire it into `/api/v1/dashboard/summary` behind **optional** `month`/`year` query
params — omitting them preserves the existing all-time behavior exactly, so nothing was rebuilt.

**Verified by** (`/api/v1/dashboard/summary?property_id=...&month=...&year=...`, tokens from the two
challenge logins):
- Paris, tenant-a, **March 2024** → `"2250.000" / 4` — includes `res-tz-1`'s €1250.
- Paris, tenant-a, **February 2024** → `"0.000" / 0` — `res-tz-1` moved out, not double-counted.
- **All-time** (no params) still → `"2250.000" / 4` — March + Feb reconstruct the all-time total exactly;
  the new params are additive, the old path is untouched.
- New York, tenant-b, `prop-004`, **March 2024** → `"1776.500" / 4`, identical to its all-time figure —
  confirms the boundary is driven by *that property's own* timezone, not a Paris hardcode (this property
  has no boundary-straddling reservation, so nothing should move, and nothing did).
- Cross-check: the naive-UTC ("buggy") March figure is mathematically `1000.000 / 3` (the three
  `333.33x` rows, excluding `res-tz-1`) — this is exactly the mock value captured in Bug 1's baseline,
  confirming the mock data was itself seeded from the buggy calculation.
- Demoed via `:8000/docs` (Swagger) / direct API calls — the dashboard UI has no month selector and none
  was added, per the "do not rebuild" constraint; only the endpoint needed the optional params.

## Bug 6 — Property selector not tenant-scoped `[P2]`

**Symptom:** Logged in as Ocean, the property selector listed Sunset's "City Apartment Downtown", and
`prop-001` was labeled "Beach House Alpha" (Sunset's property) instead of Ocean's actual "Mountain Lodge
Beta".

**How I found it:** `Dashboard.tsx:4-10` hardcoded a hardcoded 5-property array spanning **both**
tenants, rendered identically for every user, independent of who was logged in.

**Root cause:** the property list was a static constant, never fetched from any tenant-aware source.
Investigating further: no `/api/v1/properties` endpoint existed in this backend at all — the frontend's
`SecureAPI.getProperties()` pointed at a route that was never implemented in this challenge build. There
was also a separate, misleading vestige: `secureApi.ts`'s `getDashboardSummary` sent an
`X-Simulated-Tenant` header from `RevenueSummary`'s `debugTenant` prop — grepping the backend confirms it
is **never read anywhere**, so a client could set that header to any value with zero effect. Dead code
that implies a client can assert its own tenant is worth removing on its own merits.

**The fix:** add a minimal `GET /api/v1/properties` endpoint (`properties.py`) that filters by the
authenticated user's `tenant_id` — mirroring `dashboard.py`'s existing auth pattern, not a new
architecture. `Dashboard.tsx` now fetches this list on mount instead of using a hardcoded array. Removed
the dead `X-Simulated-Tenant` header and the `debugTenant`/`activeTenant` plumbing that sent it.

**Verified by** (API, then real logged-in browser sessions):
- `GET /api/v1/properties` as Sunset → `prop-001 "Beach House Alpha"`, `prop-002 "City Apartment
  Downtown"`, `prop-003 "Country Villa Estate"`.
- Same endpoint as Ocean → `prop-001 "Mountain Lodge Beta"`, `prop-004 "Lakeside Cottage"`, `prop-005
  "Urban Loft Modern"` — correct name for the shared ID.
- Cross-tenant probe: Ocean requesting `prop-002` (a tenant-a-only property) via the dashboard endpoint
  returns `"0.000" / 0`, never tenant-a's real data.
- Browser, logged in as each client: Sunset's selector lists only her 3 properties; Ocean's selector
  lists only his 3, with `prop-001` correctly reading **"Mountain Lodge Beta"**.
- The `X-Simulated-Tenant` header is now gone from the codebase entirely, and every check above still
  passes — confirming it never mattered.

## Out of scope (reported, not fixed)
- `schema.sql:35-36` enables RLS on `properties`/`reservations` but defines **zero policies**, and the
  app connects as superuser `postgres` → RLS is bypassed entirely. Policies are a design change.
- `print()` debug statements (`reservations.py`) should be `logger` calls.
- `tenant_resolver.py:92` defaults any unrecognized email to `tenant-a` rather than rejecting — a latent
  isolation smell outside the 6 fixes above (only the two provided challenge logins are exercised here).

## Video walkthrough — running order (severity order, 5–10 min cap)

| Order | Bug | Why this position | Budget |
|---|---|---|---|
| 1 | Bug 3 — cross-tenant cache leak | Highest severity: a real privacy breach (Client B's complaint) | ~2.5 min |
| 2 | Bug 5 — timezone month boundaries | Data accuracy: Client A's exact complaint, board-meeting stakes | ~2.5 min |
| 3 | Bug 4 — money precision | Lower stakes, still client-visible (finance's "few cents") | ~1.5 min |
| 4 | Bugs 1, 2, 6 (grouped) | Root-cause/infra enablers and minor isolation gap — mention briefly as what made 3/4/5 possible and demonstrable | ~2.5 min |

Total ≈ 9 min. Bugs 1 and 2 are the shared root cause behind everything (the DB was never read; the
session code was broken underneath it) — they're most efficiently narrated as *why* fixing 3/4/5 was
even possible, rather than as their own standalone segments.
