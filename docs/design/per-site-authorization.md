# Per-site authorization design

Status: implemented on `feat/trust-boundary` (phase 2). Open questions below were
resolved with the defaults in `docs/design/trust-boundary-phase2.md`.

> **The threat model in this document is wrong, and it is kept only as a record
> of how the mechanism was designed.** It was written assuming client companies
> whose data must stay separate. There are none: every site belongs to the same
> team and every dashboard operator sees all of them. On 2026-08-06 the team lead
> settled it — *"keep the scoped keys, they're for limiting blast radius if one
> ever leaks, not client isolation, since we don't have clients."*
>
> Read every "client" below as "a key", and every "cross-tenant access" as "one
> leaked credential reaching further than it should". The shipped mechanism is
> unchanged and correct; only its purpose is restated.
> `app/services/authorization.py` is authoritative.

## Problem and threat model

Today, every protected API route uses one static `API_KEY` value from the environment and
the `X-API-Key` header. The key is global: anyone who has it can list, read, modify, analyze,
publish, or delete data for every site in the LinkMesh installation. Site IDs and related
resource IDs are not ownership boundaries.

The primary threat is one leaked or over-broad key reaching sites it was never issued for.
This includes direct site routes and indirect resources such as a suggestion ID or RQ job
ID. The target guarantee is:

> Credentials for site A cannot read or change site B or any resource owned by site B.

Admin operators still need an explicit cross-site credential for support and fleet-wide
operations. The health endpoint remains unauthenticated and must expose no tenant data.

## Proposed authorization model

### Tenants, sites, and keys

Introduce a first-class tenant (or owner) record. A tenant owns a set of sites, and each
normal API key belongs to exactly one tenant. A site belongs to exactly one tenant unless a
future requirement explicitly introduces shared ownership.

API keys become database records with at least:

- `id` and `tenant_id`;
- a unique, non-secret prefix used to find the candidate record;
- a hash of the secret, never the plaintext key;
- a display name and optional expiry;
- `created_at`, `last_used_at`, and `revoked_at` lifecycle fields;
- an explicit admin scope for keys allowed to operate across tenants.

A generated key should contain a recognizable environment marker, lookup prefix, and a
high-entropy secret. Only the prefix is stored or displayed after creation. The secret is
shown once, and requests verify its hash using constant-time comparison. A keyed hash with
an application-side pepper is preferred so a database-only leak is insufficient to test
candidate keys offline.

Normal keys derive their allowed site set from tenant ownership. Admin keys bypass the
tenant equality check, but the bypass must be explicit in the authenticated principal and
auditable; it must not be inferred from a missing tenant.

### Authentication result

Authentication should return a small principal object containing the key ID, tenant ID,
admin flag, and lifecycle state. Authentication rejects unknown, expired, or revoked keys
before route logic runs.

Authorization then evaluates the requested resource against that principal:

- normal key: requested site's `tenant_id` must equal the principal's `tenant_id`;
- admin key: access is allowed and recorded as an admin operation;
- mismatch: return `403 Forbidden` before reading or mutating site-owned data;
- missing resource: return `404 Not Found` after the caller has passed the applicable
  ownership lookup policy.

The team should decide whether returning `403` for an existing foreign site leaks too much
information; the requested behavior for this proposal is an explicit `403` on mismatch.

## Single enforcement point

The core policy should live in one FastAPI dependency, conceptually:

```python
require_site_access(site_id) -> AuthorizedSite
```

It authenticates the API key, loads the site, applies the admin-or-same-tenant policy, and
returns the authorized site. Route functions should depend on the returned site rather than
performing a second unscoped lookup. Tests should fail if a new site-bearing route omits the
dependency.

Routes identified only by a child resource need a thin resolver dependency that obtains the
resource's `site_id` and invokes the same central policy. These are not separate policy
implementations. For example, `require_suggestion_access(suggestion_id)` resolves the
suggestion and delegates to the site policy.

### Route inventory

| Area | Routes | Required enforcement |
| --- | --- | --- |
| Sites | `POST /sites` | Create under the caller's tenant; define separately how an admin chooses a tenant. |
| Sites | `GET /sites` | Filter the query to the caller's tenant; admins may list all or use an explicit tenant filter. |
| Sites | `GET /sites/{site_id}` | `require_site_access(site_id)`. |
| Sites | `DELETE /sites/{site_id}` | `require_site_access(site_id)` before deletion. |
| Sites | `GET /sites/{site_id}/articles` | `require_site_access(site_id)` before querying articles. |
| Ingestion | `POST /sites/{site_id}/ingest` | `require_site_access(site_id)` before enqueue. |
| Ingestion | `GET /sites/{site_id}/ingestion-runs/latest` | `require_site_access(site_id)` before reading runs. |
| Ingestion | `GET /sites/{site_id}/ingestion-runs` | `require_site_access(site_id)` before reading runs. |
| Suggestions | `POST /suggestions/{site_id}` | `require_site_access(site_id)` before analysis enqueue. |
| Suggestions | `GET /suggestions/{site_id}` | `require_site_access(site_id)` before listing suggestions. |
| Suggestions | `PUT /suggestions/{suggestion_id}` | Resolve `Suggestion.site_id`, then apply the central site policy before review. |
| Suggestions | `POST /suggestions/bulk-review` | Resolve every requested suggestion and require access to every site before changing any row; reject the whole request atomically on one mismatch. |
| Publish | `POST /publish/{site_id}` | `require_site_access(site_id)` before publication enqueue. |
| Publish | `GET /publish/{site_id}/status` | `require_site_access(site_id)` before returning counts. |
| Jobs | `GET /jobs/site/{site_id}` | `require_site_access(site_id)` before listing durable jobs. |
| Jobs | `GET /jobs/{job_id}` | Resolve the durable `JobRun` and its `site_id` before consulting or returning Redis status. Never expose a live RQ job before authorization. |

`GET /health` remains open. Any future fleet-wide route must require an admin principal
explicitly rather than relying on the normal site dependency.

## Key lifecycle

### Creation

Only an admin-controlled workflow may create keys initially, such as an internal CLI or a
separate admin-only endpoint. The workflow selects a tenant, generates a high-entropy key,
stores only its prefix and hash, and shows the plaintext exactly once. Keys must never be
written to application logs or returned by list endpoints.

### Rotation

Rotation creates a new key while the old key remains valid for a short, deliberate overlap.
The client switches credentials, operators verify use through `last_used_at` or audit data,
and then revoke the old key. Rotation must not mutate a stored hash in place because that
removes attribution and rollback options.

### Revocation and expiry

Revocation sets `revoked_at` and takes effect on the next request. Expired keys behave like
revoked keys. If authentication results are cached, the cache must have a short bounded TTL
and an explicit invalidation path so revocation is not delayed unexpectedly.

## Migration from the current `API_KEY`

Use a time-limited compatibility mode:

1. Add scope ownership and database-backed key authentication without changing existing
   callers.
2. When the legacy environment `API_KEY` matches, construct a compatibility principal with
   explicit all-sites admin scope.
3. Emit a deprecation metric or structured warning for every legacy-key request, without
   logging the key.
4. Issue scoped database keys to machine callers and integrations as they appear.
5. Set and communicate a removal date, then disable compatibility in non-development
   environments before deleting the legacy setting in a later release.

Compatibility must never silently map the legacy key to the first tenant or to an unscoped
principal. Its temporary all-sites power must be visible as an admin scope.

## Security and test expectations

- Keys are high entropy, hashed at rest, redacted from logs, and accepted only over HTTPS at
  the deployment boundary.
- Every site-scoped query includes the authorized site or tenant constraint; authorization
  must not rely only on filtering response data after an unscoped query.
- Bulk operations authorize all referenced rows before the first mutation.
- Admin bypasses and key lifecycle changes produce audit events.
- Integration tests create two tenants and prove that each route above allows same-tenant
  access, returns `403` for cross-tenant access, and does not partially mutate bulk requests.
- Tests cover revoked, expired, rotated, malformed, legacy compatibility, and admin keys.
- A route-inventory or OpenAPI-based test should flag newly added site-bearing routes that
  lack an authorization dependency.

## Explicitly out of scope

- End-user accounts, login screens, SSO, OAuth, or identity-provider integration;
- per-article, per-suggestion, or field-level permissions;
- rate limiting, quotas, and abuse throttling;
- changing RQ worker trust boundaries or encrypting site content at rest.

## Answers from the team lead

All eight are settled. Kept with their answers so nobody reopens them.

1. **Is one tenant exactly one client company?** Neither. There are no client
   companies — the question does not apply. A scope bounds one key's reach
   (2026-08-06).
2. **Can a site be shared by multiple tenants?** No, ownership is singular
   (`sites.tenant_id NOT NULL`). Content-pool sources are the one shared kind,
   readable by every scope and writable only by admins.
3. **Who may issue, rotate, and revoke keys?** Admin principals only, via
   `POST /admin/api-keys`. No self-service API is planned.
4. **Should keys support narrower scopes (read-only, analyze, publish)?** Not
   yet. Scope-wide read/write is sufficient, and finer grades only pay off
   against a threat model this product does not have.
5. **Should admin keys list all sites by default?** Yes.
6. **What expiration and rotation overlap?** Optional `expires_at` only, no
   forced rotation.
7. **`403` or `404` for a foreign-but-existing resource?** `403`. Enumeration
   hardening is not worth the debugging cost when every caller is internal.
8. **Deprecation date for the legacy `API_KEY`?** None set. It stays an explicit
   admin principal with a structured warning; the dashboard proxy depends on it,
   and no external caller has to be migrated off it.
