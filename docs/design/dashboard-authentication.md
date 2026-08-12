# Dashboard authentication design

Closes FE-SEC-01. Written 2026-08-06 against the team lead's decisions of the same day.

Scope is authentication and admission control for the dashboard. Per-site authorization
(`docs/design/per-site-authorization.md`) is already shipped and unchanged by this.

## Problem and threat model

`linking-engine-frontend/nginx.conf:62` attaches the shared backend key to every `/api/`
request unconditionally:

```nginx
proxy_set_header X-API-Key "${LINKMESH_API_KEY}";
```

That key resolves to `Principal(is_admin=True, source="legacy_env")`
(`app/services/authorization.py:126-131`). Three consequences:

1. **No authentication.** Anyone who can reach the dashboard holds full backend authority
   without presenting a credential. They can create and delete sites, enqueue expensive
   jobs, change review state, and publish links to live WordPress. `curl` against `/api/`
   is enough — the proxy supplies the key.
2. **No attribution.** Every action is logged as one key, so the audit trail cannot say
   who did what.
3. **A live failure.** Pool-source operator actions reject that key outright.
   `require_operator_identity` (`app/api/deps.py:69-98`) accepts operator-mapped env keys
   and database admin keys, but not `legacy_env`. Verified 2026-08-06 against the running
   API: `POST /api/v1/sites/7/pool-source/reactivate` returns **401** with the shared key
   and **409** (auth passed, wrong platform) with an operator key. This breaks approve,
   revoke and reactivate at `app/api/routes/sites.py:376,398,416`.

Setting `LINKMESH_API_KEY` to an operator key works around (3) but makes (2) worse, by
attributing every operator's actions to one name.

## Decisions already taken

From the team lead on 2026-08-06, in response to the 2026-08-05 report:

- **Telegram login** for the dashboard.
- **Pending request plus admin approval.** First login creates a request an admin must
  approve. Explicitly not an allowlist: "not automatic just from being on a list."
- **No per-person scoping.** "full access once approved... everyone's internal and sees
  everything." Authentication and admission only; no roles.

Amended on 2026-08-11, after the team lead read the live dashboard:

- **One privileged admin group.** Approve and revoke belong to admins alone — "we do have
  a bigger custom hierarchy system... but given how tight time is right now, keep it
  simple: limit approve/revoke power to one privileged admin group, nothing more
  elaborate for now." Everything else stays as above: an approved account still sees the
  whole dashboard.
- **Enforced in the API.** "just confirm that's actually enforced on the backend, not just
  hidden in the UI." `require_dashboard_admin` answers 403 for an approved non-admin; the
  hidden buttons are only the courtesy on top of it.
- **Server stays behind the IP-restricted firewall, not public.** "Firewall as the network
  layer, login plus admin approval as the second layer on top."
- **Scoped API keys stay**, reframed as blast-radius containment rather than client
  isolation: "we don't have clients."

### Consequence: fleet-wide reads are admin-only

Containment only means something if a scoped key cannot reach fleet-wide data. Routes whose
answer *is* an aggregate across every site therefore require an admin principal, because a
`site_id` query parameter is a filter and not a scope — omitting it reports on the whole
fleet. `/evaluation/*` (metrics, drill-down and CSV export) is admin-only for this reason,
enforced as a router dependency rather than per route so a new endpoint cannot be added
without it. Pipeline batches take the opposite shape: they name their sites, so they are
authorized site by site instead.

## Why not the Telegram Login Widget

The widget is the obvious reading of "Telegram login", but it does not fit this
deployment. Telegram requires the embedding domain to be registered with @BotFather and
to be **publicly routable**; localhost and bare private IPs are rejected, and it serves
only on ports 80/443. The documented workaround for unroutable environments is a public
tunnel, which is exactly what a firewalled deployment is meant to avoid.

There is a plausible path — register a real public domain whose DNS points at the private
address, and issue its certificate over a DNS-01 challenge. Telegram's servers never fetch
the dashboard, so public *resolvability* would be enough and public *reachability* would
not be needed. But that rests on an assumption we have not verified (that BotFather does
not probe the domain), and it adds a DNS and certificate dependency to a login path.

**Recommendation: use the bot deep-link flow instead.** It is still Telegram login and
still gives an authoritative Telegram user ID, but it has no domain, DNS or certificate
dependency and works from a bare IP. It needs only *outbound* access to `api.telegram.org`,
which an inbound IP restriction does not block. If a public hostname and certificate ever
exist, the widget becomes a drop-in UX improvement on the same data model.

## Implemented flow

1. The dashboard opens the static deep link `https://t.me/<bot>?start=login`. It contains
   no browser credential and is safe to forward.
2. Telegram delivers the operator's authenticated Telegram identity to the long-polling
   bot. No inbound webhook or public dashboard hostname is required.
3. An unknown Telegram ID is recorded as **pending** and receives no code. An existing
   approved operator must approve it; there is no automatic allowlist.
4. On a later `/start`, an approved operator receives a short-lived, single-use code in
   their private Telegram chat. Only an HMAC digest of that code is stored.
5. The operator carries the code back to the original browser. Redeeming it creates the
   `HttpOnly`, `SameSite=Lax` dashboard session cookie.

The browser does not create or poll a redeemable Telegram token. This prevents a relayed
browser-created bot link from signing the original browser in as the person who opened it.

## Single enforcement point

The gate belongs at the proxy, not in the SPA. A React route guard changes nothing,
because nginx would still attach the key for a direct `/api/` call.

nginx gains an `auth_request` against an internal verification endpoint. The shared key
injection stays exactly where it is, but only downstream of a verified session — it
becomes a proxy-to-backend credential rather than the caller's identity, which is what the
security review asked for. The existing `X-LinkMesh-Client` CSRF marker and Host allowlist
stay and keep doing their job; a session cookie makes the marker more load-bearing, not
less.

## Data model

- `dashboard_users` — telegram user ID, username, status (`pending`/`approved`/`revoked`),
  `is_admin`, who approved and when, last seen. `is_admin` is the whole hierarchy: one
  group, granted and removed by an admin, and never by the person holding it.
- `login_nonces` — legacy table/column names containing an HMAC digest of the one-time
  code, its expiry, the Telegram ID it belongs to, and consumed-at. Single use.
- Sessions — cookie carrying a signed, expiring token; `HttpOnly`, `SameSite=Lax`,
  `Secure` once TLS terminates in front.

## Attribution

An approved session supplies a real per-person operator identity. `require_operator_identity`
gains a session-derived branch ahead of its key-based ones, which fixes the 401 above and
lets pool audit events name a person instead of a key. This is the same identity the audit
trail at `PoolAuditModal.tsx:36` already displays.

## Security and test expectations

- Codes are single-use, short-TTL, stored only as keyed digests, and issued only through
  the operator's private Telegram conversation. Login start/completion is rate-limited at
  the proxy.
- The browser deep link is static and carries no credential.
- A pending or revoked user is refused at `auth_request`, not merely hidden in the UI.
- An approved non-admin may read the roster and gets 403 from approve, revoke, and both
  admin-group routes.
- Revocation takes effect on the next request, not the next login.
- Direct `/api/` access without a session returns 401 from the proxy, with no backend key
  attached. This is the regression test that pins FE-SEC-01 closed.
- Backend and frontend authentication regression suites must stay green.

## Explicitly out of scope

Per-person authorization, roles, and per-site scoping for dashboard users — ruled out by
the team lead. External users and public exposure. Replacing the scoped API keys, which
stay for blast-radius containment.

## Deployment choices

- `DASHBOARD_BOOTSTRAP_ADMIN_ID` may seed the first approved operator, and puts it in the
  admin group. It promotes a pending user only; it never silently restores a revoked
  account on restart. It *does* restore the admin flag on an approved account, which is
  the way back into a deployment whose last admin was demoted.
- Sessions default to 12 hours.
- The dashboard remains behind the IP-restricted firewall. TLS is still required anywhere
  WordPress application passwords would otherwise cross an untrusted network.
