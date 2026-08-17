# Trust boundary — phase 2

Status: implemented on `feat/trust-boundary`.

> **What a scope means was settled on 2026-08-06, after this document was
> written.** A scope is blast-radius containment for a leaked key, not client
> isolation: *"keep the scoped keys, they're for limiting blast radius if one
> ever leaks, not client isolation, since we don't have clients."* The mechanism
> described below is correct and stays. The framing of rows 1 and 9 below, and of
> "`base_url` uniqueness" and "Frontend" further down, has been corrected in
> place. `app/services/authorization.py` holds the authoritative statement.

## Defaults for open questions

| # | Decision |
|---|----------|
| 1 | ~~One tenant = one client company.~~ There are no client companies. A scope bounds what one key can reach; one scope (`default`) is in use and every site belongs to it. |
| 2 | Site ownership is strictly singular (`sites.tenant_id` NOT NULL). |
| 3 | Only admin principals mint/rotate/revoke keys (`POST /admin/api-keys`). |
| 4 | Tenant keys are full read/write for that tenant; no finer scopes yet. |
| 5 | Admin keys list all sites by default. |
| 6 | Optional `expires_at` only; no forced rotation overlap in v1. |
| 7 | Cross-tenant access returns **403**. |
| 8 | Legacy `API_KEY` remains an explicit **admin** principal with a structured warning until a later release disables it. |
| 9 | Content-pool sources are **shared**: readable by every tenant, writable only by admins. |

## What shipped

- `tenants` and `api_keys` tables; every site has `tenant_id` (existing rows → `default`).
- HMAC-SHA256 key hashes with `API_KEY_PEPPER`.
- `Principal` + `require_site_access` / tenant filters on list and fleet routes.
- Admin routes: create tenant, delete empty tenant, mint key (plaintext once), list, revoke.
- Operator env keys and legacy `API_KEY` remain admin-scoped for the dashboard proxy.

## Read is wider than write

`require_site_access` (mutations, operational reads) is strict ownership.
`require_site_read` (a site's own description and articles) additionally admits
`platform = "pool"`.

This is not a softening of isolation — it resolves a contradiction. An approved
pool source is a link target in *every* tenant's queue, so scoping pool sites to
their owning tenant produced queues full of suggestions whose target site the
reviewer could not list, open, or verify before approving. Reading a shared
source is therefore allowed; creating one (`require_creatable_platform`) and
approving one (`require_operator_identity`) stay admin-only, because either act
is fleet-wide.

Strict `tenant_site_filter` still governs jobs, alerts, and publication, where
another tenant's activity must stay invisible.

## Key lifetime

`expires_at` is optional and enforced at authentication. The mint schema sets
`extra="forbid"`: an unrecognized lifetime field must fail loudly rather than
return 201 for a credential that is not actually bounded. Past timestamps are
rejected at 422.

`last_used_at` is refreshed at most once per `LAST_USED_REFRESH` (60s) and
committed in its own transaction. Committing matters because `get_db` never
does — a flush alone was rolled back on every read-only request, leaving the
column null for exactly the dormant keys an operator wants to find. Throttling
matters because the refresh is an `UPDATE` holding a row lock until commit, so
writing on every request would serialize all concurrent traffic sharing a key.

## `API_KEY_PEPPER`

Minting refuses with 503 outside development when the pepper is unset. The
fallback pepper is a source constant, so hashes made with it are verifiable
offline from a database leak alone — and minting under the fallback then setting
a real pepper invalidates every existing key at once, with a 401 and no
explanation. Failing at mint time puts that where an operator can act on it.

## `base_url` uniqueness

Unique per `(tenant_id, base_url)`, not globally (`d4f2a8c61b93`). The reason is
containment, not shared ownership: under a global constraint the 409 answers
"does this URL exist anywhere?" for a caller who may not read anywhere, which
turns site creation into an inventory oracle. With one scope in use the
constraint behaves exactly like a global one. The downgrade refuses while any URL
is held by more than one scope, since global uniqueness is no longer satisfiable
at that point.

## Compatibility

The production dashboard injects one shared admin service key downstream of a
verified session, and that is the intended end state rather than a stopgap:
operators are internal and see everything, so the dashboard has no reason to hold
a narrower credential. Scope enforcement therefore applies to keys issued via
`/admin/api-keys` — machine callers and integrations — not to dashboard traffic.

## Frontend

No SPA change is required, and none is planned. The earlier "phase 3: multi-tenant
operator UX and per-tenant proxy keys" is **dropped** — it was premised on
clients, and there are none.

The one scenario the SPA would have to handle, if a scoped key ever reached it,
is that a visible site is not always an actionable one: `GET /sites` returns
shared pool sources alongside owned sites, and mutating them 403s for a scoped
key. `SiteOut.tenant_id` is the field to branch on. Under the shared admin key
this cannot arise.
