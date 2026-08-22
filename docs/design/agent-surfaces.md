# Agent surfaces

LinkMesh exposes one read-only action set to two agent-facing surfaces: an
MCP server for external clients (Claude Code, Cursor, any MCP host) and a
chat assistant embedded in the dashboard. This note records what they may
touch, why the boundary sits where it sits, and how to operate them.

## The single registry

`app/agent_tools.py` is the whole tool surface — both surfaces execute its
handlers, and neither defines tools anywhere else. Most handlers call the
REST route functions directly (`count_suggestions`,
`list_suggestion_page`, `list_sites`, `get_evaluation_metrics`, …), so an
agent's answer is computed by exactly the code path the dashboard uses,
including tenant scoping and site authorization.

Every tool reads. Nothing in the registry approves, rejects, publishes,
crawls, or enqueues. That line is deliberate: suggestions carry untrusted
crawled text, and a manipulated model with write access would be an
injection path into customer sites. Review actions stay human-only; the
review workflow's success gates were designed for people, not prompts.

## MCP server (`/mcp`)

Streamable HTTP at `/mcp/`, stateless, JSON responses (no SSE). Two layers:

1. `ApiKeyGate` transport middleware rejects any request without a valid
   `X-API-Key` before JSON-RPC parsing, so unauthenticated callers cannot
   even open a protocol session.
2. Each tool execution runs the shared handler against a fresh session, using
   the `Principal` the gate resolved and left on the ASGI scope. Authentication
   happens once per request; authorization runs on every call, exactly as in
   REST. The tool keeps a header fallback for the case where this module is
   mounted without the gate — losing authentication is not an acceptable
   failure mode for a missing optimisation, and
   `test_a_call_authenticates_once_not_twice` pins the fast path.

Fleet-wide tools (`get_evaluation_metrics`) enforce the same admin-only
rule as their REST router.

Two things the protocol carries that prose cannot:

- **Annotations.** Every tool ships `readOnlyHint: true`,
  `destructiveHint: false`, `openWorldHint: false`. A host that can only read
  this note has to prompt for each call; one that reads the annotation can
  auto-approve a surface that provably changes nothing. The hints are set once
  in `mcp_server.READ_ONLY_ANNOTATIONS` because the read-only property belongs
  to the whole registry, not to individual tools —
  `test_registry_is_read_only_by_construction` is what keeps that true.
  `idempotentHint` is not declared: the specification gives it meaning only
  when `readOnlyHint` is false.
- **Result schemas.** Five tools publish an `outputSchema`: `search_queue`,
  `find_articles`, `get_site_jobs`, `get_suggestion_history`,
  `get_ingestion_diagnostics` — the ones that return a count beside a list,
  which is where one number was being read as another. `inputSchema` stops a
  model inventing an argument; `outputSchema` stops it misreading a field, and
  it does so *before* the call rather than after. The schema carries the
  distinction in prose a model reads: `match_count` says "the answer to how
  many", `page.returned` says "never the answer to how many".

  The rest of the registry declares none, deliberately. A payload of
  unambiguous scalars gains nothing from a contract, and an unused model is one
  more thing to keep true.

  fastmcp publishes the schema but does **not** validate results against it, so
  a declared shape is otherwise a promise nothing checks —
  `test_declared_output_schemas_describe_the_real_payload` calls every
  contracted tool against real fixtures, and `output_schema_violation` logs any
  drift a fixture never reaches. Drift is logged, not raised: a read-only
  status tool answering with an extra key is still a useful answer, and a
  schema slip should not become an outage.

  `structuredContent` is separate and was already there — fastmcp derives it
  from the handler's dict. The schema is the *contract*; `structuredContent` is
  the data.
- **Failures.** The registry answers failures as `{"error", "status"}` data,
  which the chat loop reads directly. Over MCP that shape is indistinguishable
  from a successful payload, so `_register` translates it through `error_of`
  into a `ToolError` — the client gets `isError` *and* the same quotable
  message in the content.

Connect a client with:

```json
{
  "mcpServers": {
    "linkmesh": {
      "url": "https://<engine-host>/mcp/",
      "headers": { "X-API-Key": "<key>" }
    }
  }
}
```

## Dashboard assistant (`POST /api/v1/agent/chat`)

A small tool-calling loop over OpenRouter (`app/services/agent_service.py`)
with the same registry as its toolset. Bounds: `AGENT_MAX_TOOL_ROUNDS`
model turns that may carry calls, a final no-tools turn if the cap hits,
and `AGENT_MAX_HISTORY_TURNS` on the client-supplied transcript. The panel
(`src/components/agent/AgentPanel.tsx`) renders complete turns plus a chip
per tool consulted; there is no streaming by design.

An empty key disables chat only (503 from `/agent/chat`, honest status from
`/agent/status`). MCP tools keep working without it — they answer from the
database, not from a model.

### Provider

The assistant reads `AGENT_BASE_URL` / `AGENT_API_KEY`, falling back to the
`OPENROUTER_*` pair when they are empty — which is what every deployment did
before they existed. Any OpenAI chat-completions endpoint works, because that
is all `chat_with_tools` speaks; `/agent/status` reports the host it resolves
to, since "which endpoint is actually being called" is the question an operator
debugging a dead panel is asking.

They are separate settings rather than a repointing of the shared pair because
those also drive **placement context**, which runs on every preview an editor
opens. Moving the assistant onto a development endpoint must not take a
production feature with it, and `test_placement_does_not_follow_it` is what
keeps that true.

`DEVELOPMENT_ONLY_HOSTS` names providers whose terms permit evaluation only —
NVIDIA NIM's API Trial terms allow internal testing and evaluation, and count
activity serving real end-users as production — and `log_provider_notice` warns
once at startup when one is configured. The point is not to prevent the choice;
it is that a development convenience should not become the production path
without anyone noticing.

### Choosing a model

`AGENT_MODEL` must support tool calling, because the registry is the whole
feature: a model that cannot call a tool answers every operational question
from nothing. Three properties beyond that decide whether one is usable, and
none of them is visible from a model's name, size, or catalogue description.

**Two tools in one turn.** Some models emit both calls in a single assistant
message and then reject that transcript on the following request.
`meta/llama-3.1-8b-instruct` on NVIDIA NIM answers a one-tool question and
fails a two-tool one with `500 ... This model only supports single tool-calls
at once!` — the failure is in the provider's prompt template, not in
`chat_with_tools`, so there is nothing to catch and retry. A model that calls
its tools sequentially across turns never meets it. Test with a question that
needs two tools; the one-tool case proves nothing.

**Latency tracks demand, not size.** On a shared free tier a newly published
model can spend over a minute queued before generating anything — a three-word
prompt returning two tokens in eighty-odd seconds is queue wait, not compute,
since two tokens cost milliseconds to produce. A comparable model on the same
key and endpoint answered in under a second. No prompt, budget, or timeout
change touches it; it simply exceeds `AGENT_TIMEOUT_SECONDS` partway through a
conversation, which the operator sees as the 503 from *Model failures* below.

**Reasoning models spend the output budget on thinking.** Where a model returns
`reasoning_content`, that text counts against `AGENT_MAX_OUTPUT_TOKENS` before
any answer is generated. A budget sized for the reply alone can be consumed
entirely by a model reasoning about a surprising tool result, and the turn
returns `finish_reason=length` with empty content — a blank panel rather than
an error, which is the harder version to diagnose.

### One answer path

Every question reaches the model. There is no shortcut for "easy" questions,
and adding one back is the thing to argue against.

An earlier version answered plain counts deterministically, which meant a
regex had to decide whether a question was one of the few the hand-written
replies could express. That decision cannot be made from words alone. It
matched on substrings, so "how many pending suggestions on **site** 1?" hit
the all-sites branch; the blocklist that was supposed to catch the misses
listed `above`, `orphan`, and `site <n>` but not `today`, `last week`, or
`no internal links`, and every phrasing outside the list was answered with a
whole-queue aggregate. The failure is silent by construction: the operator
gets a confident reply to a *different* question with no model in the loop.
That blocklist can never be finished, because it enumerates the language
people do not use.

The guarantee it was protecting now lives in the payload instead. The one
answer a model could plausibly get wrong was a count, because `list_sites`
published a capacity beside it — `suggestion_slots_available: 0` next to
`active_suggestion_count: 147`, two bare numbers at the same level, either a
reasonable answer to "how many suggestions do I have". `_compact_site` groups
them under separate nouns (`content`, `queue`, `suggestion_capacity`), which
is the shape `get_site_status` already used. Structure, not prose, is what
makes the question have one answer — the prompt rule that used to warn about
this is gone with it.

Keep new tool payloads that way. Where a number could be read as another
number, group them or rename them; do not add a warning to the system prompt
and do not add a code path that answers around the model.

### Model failures

`chat_with_tools` raises `OpenRouterError` for rate limits, timeouts, and
unusable bodies. The route answers 503 with a fixed message and logs the
provider's text, which can carry key and account detail. A spent free-tier
quota is a temporary outage, and it reads as one for every question rather
than working for a lucky few.

## Filter vocabularies

`status`, `method`, and job `kind` are `Literal`s in `agent_tools`, mirroring
`SuggestionStatus`, `SuggestionMethod`, and `job_service._QUEUES`. They are
written by hand because `Literal` members must be literal, and
`test_filter_vocabularies_match_the_database` is what keeps them equal to their
sources.

This is not validation hygiene. An unconstrained filter *accepts* an invented
value — `search_queue {"status": "nope"}` returned `{"returned": 0, "total": 0}`
with no error, which reads to a model, and then to an operator, as "there are
none of these". A closed filter fails loudly with the permitted values instead,
which the model can act on.

`event_type` is deliberately left open: `suggestion_events.event_type` is a
plain `String(30)` written from both the application and a database trigger, so
any list here would be a guess that silently blocks real events. Close a filter
only where a column definition already closes it.

## Payload order

Field order is load-bearing, because the transcript budget trims from the end.

Put the decisive scalars first and the rows last. `search_queue` published
`total` and `next_cursor` after its suggestion array; a 50-row page serializes
to 19,014 characters against a 12,000 budget, so both were cut, and the model
answered "how many match" with its own page size and called the list complete.

Group a page size away from a count for the same reason `list_sites` groups a
capacity away from one: `search_queue` and `get_suggestion_history` nest
`returned` under `page`, leaving `match_count` as the only top-level integer
that answers "how many". `match_count` is the name `preview_bulk_review`
already used.

`_bounded_json` enforces the budget by dropping whole rows from the longest
list and reporting `omitted_rows`, never by slicing the serialized string. A
slice cuts mid-token, so the model parses a broken object and cannot tell a
short answer from a complete one.

## Capped lists carry a count

Every tool that applies a `LIMIT` also returns `match_count` — the whole match,
under the same filters — and a `page` object saying what it actually returned.
`find_articles`, `get_site_jobs`, `get_ingestion_diagnostics` and `search_queue`
all use that shape; `get_ops_digest` returns a `counts` block for its four
sections. A bare row count after a `LIMIT` is indistinguishable from a complete
answer, and "how many articles are orphans" is exactly what these tools are
asked.

Filter in SQL, not after the fetch. Two tools used to read a page and narrow it
in Python: `get_publication_status` fetched 50 job runs of any kind and kept the
publication ones, which returned nothing at all on a site whose crawls are more
recent; `get_ingestion_diagnostics` applied `reason_code` to the route's first
200 rows, reporting no examples for a reason the run's own histogram counted in
the thousands. Both now filter and count in the query.

## Failures

Two channels, and they carry opposite amounts of detail.

Rejected **arguments** are described in full — field paths and pydantic's own
messages, which for a rejected `Literal` include the values it accepts. This is
the caller's own input, and a model given only a problem count reissued the
same call until the round cap ended the turn.

An **internal** failure returns a fixed sentence and logs the exception.
`str(exc)` on a SQLAlchemy error is the statement and its bound parameters, and
over `/mcp` that reaches any client holding a key.

## Dashboard links

Tools return ids. Inside the dashboard that is enough — the operator is
already there. Over MCP it is a dead end, since the point is that a person
reads the answer in their editor and then goes and reviews. So results carry
a link to the view they describe: `search_queue` the filters it just applied,
`preview_bulk_review` the rule staged for confirmation, `explain_suggestion`
the queue holding that row (and `/publish/{site}?suggestion={id}` once it is
past review), `get_site_status` its three pages.

This works because the frontend already keeps queue filters in the URL on
purpose (`useQueueFilters`), so the parameter names below are the frontend's,
not the registry's:

| Link | Query |
| --- | --- |
| `/queue` | `site`, `status`, `q`, `origin`, `unique`, `min`, `threshold` |
| `/publish/{site_id}` | `suggestion` |
| `/sites` | `q` |

`DASHBOARD_BASE_URL` is deployment configuration — the engine knows its own
database and nothing about where the operator's browser should go. Empty, or
anything that is not an absolute http(s) URL, omits the links entirely rather
than emitting a wrong one: an operator cannot tell a typo in deployment config
from a bug in the engine, and follows the link either way. The keys are
*absent* when unbuildable, never null, because a null link reads to a model as
"there is no link for this", which it then says out loud.

## Publication state

`get_publication_status` answers "what is blocking publication" by keeping
three counts apart, because each asks for different work:

| Count | Meaning | Next step |
| --- | --- | --- |
| `selected_suggestions` | approved, no plan yet | prepare and approve a plan |
| `prepared_plans` | plan built, unapproved | a human approves it |
| `approved_plans` | bound artifact | queue a publication job |

`next_action` ranks them: `blocked` (no credentials — preparation would fail
on every article) → `publish` → `approve_plan` → `prepare` →
`nothing_waiting`. The order is what has to happen first, so the answer is the
*next* step rather than a list of everything outstanding.

`prepared_plans` is the registry's own count. `publication_status` counts only
approved plans, so a site with a plan sitting prepared was told to "prepare"
work it had already prepared. The fleet view enriches the rows
`_pending_publication_query` returns but does not change which rows those are —
a site whose only work is a prepared plan is outside that route's definition of
pending and stays outside it here, so this view and the dashboard's inbox agree.

Unlike `pending_publication_site`, which 404s when a site has nothing waiting,
this tool answers `nothing_waiting`. That is an answer to the question, not a
failure of it.

## Settings

| Setting | Default | Purpose |
| --- | --- | --- |
| `AGENT_BASE_URL` | *(empty)* | Assistant's endpoint; empty reuses `OPENROUTER_BASE_URL` |
| `AGENT_API_KEY` | *(empty)* | Assistant's key; empty reuses `OPENROUTER_API_KEY` |
| `AGENT_MODEL` | `anthropic/claude-sonnet-4.5` | Must support tool calling; see *Choosing a model* |
| `AGENT_TIMEOUT_SECONDS` | `90` | Per model turn |
| `AGENT_MAX_TOOL_ROUNDS` | `4` | Tool-bearing turns per question |
| `AGENT_MAX_OUTPUT_TOKENS` | `1500` | Per turn |
| `AGENT_MAX_HISTORY_TURNS` | `20` | Transcript bound at the API edge |
| `DASHBOARD_BASE_URL` | *(empty)* | Dashboard origin for result links; empty omits them |

`TOOL_RESULT_BUDGET` (12,000 characters) is a module constant in
`agent_service`, not deployment configuration: it is a property of how much of
one tool result belongs in a four-round conversation.

Chat falls back to `OPENROUTER_API_KEY` / `OPENROUTER_BASE_URL` when the two `AGENT_*` settings above are empty.

## Adding a tool

Add it to the registry in `app/agent_tools.py`: an args pydantic model, a
handler returning JSON-safe dicts, a `title`, and a description written for a
model. Both surfaces pick it up automatically; add tests to
`tests/test_agent_tools.py`. Keep it read-only, or open a design discussion
first — the write boundary is the security property of this feature.

State every bound on the args model, even one the route already declares. A
handler calls its route *function*, so `Query(..., le=100)` never runs — the
pydantic model is the only validation an agent's arguments meet. `search_queue`
is the worked example: its percent band, term length, and page size are all
re-declared there.

Give a tool an `output_model` when its payload has a number that could be read
as a different number — in practice, a count beside a list. Handlers still
return plain dicts; the model describes that dict and is checked against it,
because the same dict is what the chat loop reads and what the tests assert on.
A tool answering with unambiguous scalars should not have one.

Cross-field rules belong on the model too, as a `model_validator`, not in the
handler. A rule in the handler is absent from the JSON Schema, so a model meets
it only by failing — and `preview_bulk_review` had drifted looser than the
endpoint it proposes to, accepting a `site_id` and `all_sites` combination
`BulkReviewFilter` rejects on submission. Only authorization stays in the
handler, because it needs a caller.

Paginate with a cursor, not an offset. `search_queue` takes the route's two
sort keys — valid only as a pair, which a model supplies correctly about half
the time — and hands back one opaque `next_cursor` string to copy verbatim.
Count the full match on the first page only; continuations ride the route's
look-ahead row.
