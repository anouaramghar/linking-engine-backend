# Trust boundary — phase 1

Status: implemented on `feat/trust-boundary`. Full multi-tenant keys remain
proposal-only in [per-site-authorization.md](per-site-authorization.md).

## What shipped

1. **Site delete confirmation** — `DELETE /sites/{id}` requires
   `confirm_name` exactly equal to the site's current name. Missing or wrong
   values leave the row untouched (`422` / `409`). Stops one-click and bare
   CSRF deletes when a key is already present.
2. **Dashboard proxy boundary (frontend repo)** — loopback bind, Host
   allowlist, custom `X-LinkMesh-Client: dashboard` on unsafe `/api` methods,
   browser security headers, TLS terminator required before non-loopback
   exposure. Documented in the frontend README.

## What this does not do

- End-user login, SSO, or session cookies.
- Per-tenant database API keys (see the proposal).
- Stopping anyone who already reaches a mis-bound dashboard proxy: they still
  inherit the shared service key. Network placement remains the primary
  control until phase 2.

## Next (phase 2)

Implement the proposal: tenant ownership, hashed DB keys, `require_site_access`,
legacy `API_KEY` as explicit admin compatibility, integration tests with two
tenants. Open questions in the proposal must be answered before coding that
layer.
