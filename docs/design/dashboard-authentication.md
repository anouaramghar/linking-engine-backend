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
- **Server stays behind the IP-restricted firewall, not public.** "Firewall as the network
  layer, login plus admin approval as the second layer on top."
- **Scoped API keys stay**, reframed as blast-radius containment rather than client
  isolation: "we don't have clients."

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

## Proposed flow

1. The dashboard is unauthenticated. It shows a login screen with a deep link and QR for
   `https://t.me/<bot>?start=<nonce>`, where the nonce is single-use and short-lived.
2. The operator opens it and presses Start. Telegram delivers `/start <nonce>` to the bot
   together with the sender's Telegram user ID, which Telegram has already authenticated.
3. The bot worker (long-polling `getUpdates`, so no inbound webhook) resolves the nonce and
   binds it to that Telegram user ID.
4. The dashboard polls the nonce endpoint. On success it receives a session cookie.
5. Unknown Telegram IDs are recorded as **pending** and the login stops there with an
   explanatory screen. An approved admin approves them; the next login succeeds.

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
  who approved and when, last seen.
- `login_nonces` — nonce, expiry, bound Telegram ID, consumed-at. Single use.
- Sessions — cookie carrying a signed, expiring token; `HttpOnly`, `SameSite=Lax`,
  `Secure` once TLS terminates in front.

## Attribution

An approved session supplies a real per-person operator identity. `require_operator_identity`
gains a session-derived branch ahead of its key-based ones, which fixes the 401 above and
lets pool audit events name a person instead of a key. This is the same identity the audit
trail at `PoolAuditModal.tsx:36` already displays.

## Security and test expectations

- Nonces are single-use, short-TTL, and rate-limited per IP; an unconsumed nonce leaks
  nothing.
- A pending or revoked user is refused at `auth_request`, not merely hidden in the UI.
- Revocation takes effect on the next request, not the next login.
- Direct `/api/` access without a session returns 401 from the proxy, with no backend key
  attached. This is the regression test that pins FE-SEC-01 closed.
- Existing suites stay green: 567 backend, 310 frontend.

## Explicitly out of scope

Per-person authorization, roles, and per-site scoping for dashboard users — ruled out by
the team lead. External users and public exposure. Replacing the scoped API keys, which
stay for blast-radius containment.

## Open questions for the team lead

1. **Bootstrap.** Who approves the first admin? Proposed default: seed one Telegram user ID
   from the environment as pre-approved, and require it to approve everyone else.
2. **Session lifetime.** Proposed default: 12 hours, sliding.
3. **Widget later?** If a public hostname and certificate become available, is the one-click
   widget worth adopting over the deep link?
4. **Residual from the security review, unanswered.** WordPress application passwords cross
   the network in cleartext unless TLS terminates in front of nginx. The firewall reduces
   this but does not remove it. Worth fixing while auth is being touched.
