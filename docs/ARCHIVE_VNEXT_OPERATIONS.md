# Crowley Archive vNext and V2 primitive foundation

This is an additive shadow deployment. `crowley-archive-mcp` and the existing
`archive_private.connector_historical_reasoning_v3` remain the production entry
point. Do not merge a preview branch wholesale or replace those functions.

## What runs on Supabase

| Primitive | Implemented contract |
| --- | --- |
| Archive | Immutable role-labeled frames; lexical/context embeddings; inactive-branch labels; paginated evidence reopening; generation/cutoff reporting |
| State | Version-checked values, provenance, authority, expiry and append-only history |
| Preferences | Separate record kind; active preferences require explicit user authority |
| Intents | Draft/active/paused/terminal lifecycle; one-time and event triggers; idempotent condition-check events |
| Events | Immutable, payload-checked idempotency keys |
| Workers | Durable leases, bounded read capabilities, pre-dispatch budget reservation, retries, cancellation, deadlines, schema-validated results |
| Tools | Explicit authority, enabled flag, confirmation requirement, schemas and timeout |
| Runtime | Context manifest, event/run lineage, read-only tool selection and result events |

This foundation does not run an autonomous frontier model. A trusted model
adapter can consume `runtime_context`, claim a worker, reserve calls, dispatch only
registered tools, and submit schema-valid results. External action execution and
recursive delegation are deliberately disabled. State/preferences/intents are
not inferred and activated from old assistant messages.

## Retrieval interface

The `crowley-archive-vnext` Edge Function accepts POST JSON and stateless MCP
JSON-RPC. Its tools are `search_context`, `search_text`, `search_many`,
`open_context`, `browse_time`, and `archive_status`. It does not replace the
existing connector registration. Authenticate with a dedicated Bearer token;
query-string credentials are not accepted. Store only SHA-256 hashes in
`crowley_v2.api_tokens`; give readers `archive:read` and the indexer only
`embedding:write`. Tokens expire. Failed/expired/revoked auth returns no evidence.

The SQL schemas are private, RLS-enabled, and have no client-role grants. The
Edge Function uses its server-side database connection for fixed, parameterized
queries. Do not expose these schemas to the Data API, publish a database URL, or
give SQL credentials to the model. Custom authentication is why JWT verification
is disabled for this one function; every request still requires a scoped token.

`open_context` pages use Unicode code-point offsets, matching PostgreSQL rather
than JavaScript UTF-16 indexes. Read `next_offset` until null for a complete
frame. Message pointers distinguish the prior assistant turn, user anchor, and
paired assistant turn. Assistant prose and segment cards are discovery aids;
only source evidence and explicit adoption can support claims about the user.

Navigation episodes are deterministic eight-turn segments, clearly labeled as
such. They are not LLM-generated semantic summaries. The implementation makes no
claim to recover message metadata excluded from the existing cloud mirror.

## Embeddings and backfill

`Supabase/gte-small` produces normalized 384-dimensional vectors. The tokenizer
is pinned to model revision `93b36ff09519291b77d6000d2e86bd8565378086` and
`@huggingface/tokenizers@0.2.0`. Each input is checked with the actual tokenizer,
including special tokens and a contextual prefix, and must fit 512 tokens.
The model is English-oriented; exact text remains available for other languages.
Overlapping pieces cover every Unicode character. No truncation is accepted as
completion. Long frames checkpoint one piece per invocation after testing showed
that larger batches can exceed the Edge Function CPU limit.

Raw archive rows are never updated, deleted or given new triggers. Frame builds
read a fixed ready generation, preserve full text in the derived frame snapshot,
and verify expected active/inactive counts. Re-running a batch is idempotent.
Frame identity includes generation and source URI; content hashes bind embeddings
to the exact snapshot. Do not treat a turn index as a cross-generation message ID.

The dispatcher is installed disabled. Operator setup supplies a Vault secret,
dedicated expiring embedding token, approved endpoint, stop time and request
budget. The scheduled job calls only `archive_vnext.dispatch_backfill()`.
It allows at most two outstanding requests, stops after five failed responses,
and unschedules itself at the deadline, request budget or drained queue.
`pg_net` is a wake-up transport; logged embedding jobs and lease tokens provide
durability. A lost HTTP response cannot acknowledge another worker's lease.

Inspect progress without exposing credentials:

```sql
select archive_vnext.status();
select state, count(*) from archive_vnext.embedding_jobs group by state;
select enabled, stop_at, requests_dispatched, request_budget, stop_reason
from archive_vnext.backfill_control;
select status_code, count(*) from archive_vnext.backfill_dispatches group by 1;
```

Stop the backfill reversibly:

```sql
update archive_vnext.backfill_control set enabled=false, stop_reason='operator_pause';
select cron.unschedule('crowley-vnext-embedding-backfill');
```

Do not reset the source archive to roll back vNext. Disable its token/dispatcher
and keep using v3. The existing connector is already isolated from this index.

## Verification and benchmark

```bash
npm ci --ignore-scripts
npm test
npx --yes deno@2.9.6 check --no-config --node-modules-dir=manual supabase/functions/crowley-archive-vnext/index.ts
```

Run `tests/vnext/integration.sql` only against an isolated development database.
It creates synthetic archive fixtures in a transaction and rolls them back.
Live smoke fixtures, if retained on a preview branch for HTTP tests, must use a
different generation before re-running the transaction test.

`tools/vnext/control.mjs` uses an operator-provided `DATABASE_URL` environment
variable. Commands: `status`, `index`, `benchmark-lexical`, `tick`,
`worker-status`. It never prints archive text or credentials to stdout.
Benchmark case/results stay in private tables. Real fixtures must never enter
this public repository. The legacy 25-case suite is development-only.

Read [the frozen release contract](VNEXT_BENCHMARK_CONTRACT.md). Component scores,
synthetic tests and a partial vector index cannot satisfy it. Even an eligible
score only permits review; no code in this foundation switches the connector.

## Migration discipline

New files were created using the pinned Supabase CLI. Remote DDL was applied
through the Supabase migration tool after preview-branch verification. That tool
assigns remote timestamps, so reconcile by migration **name and content** before
using `db push`; do not replay local timestamps blindly against production.
When calling the migration tool, omit file-level BEGIN/COMMIT so schema changes
and its history entry remain in the tool's transaction. A same-second remote
timestamp collision was inspected and repaired by reapplying the idempotent
hardening migration; the pre-existing archive was not reset.

The public repository did not contain the existing v3 migration history. These
additions depend on that baseline and are not a standalone `supabase db reset`
replacement. The preview replay initially lacked `http`/`pg_trgm` extensions;
installing those on the preview and rebasing recovered it. Do not copy private
legacy migrations into a public repo without auditing them for data and secrets.

## Freshness

Index completion does not advance the archive's source cutoff. A fresh export
still needs the existing external ingestion pipeline. vNext reports the actual
generation cutoff, labels stale evidence and does not use old archive evidence
to assert current state. New generations require their own verified build.
