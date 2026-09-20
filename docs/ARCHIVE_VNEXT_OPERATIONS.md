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

The messaging-lane QA pass adds strict date/scope validation, lossless timeline
cursors, explicit branch/freshness reporting and adjacent-turn navigation.
Context search uses lexical retrieval with `semantic_index_incomplete` until
all frames are embedded; partial vectors must not dominate ranking. See
[the lane QA contract](MESSAGING_LANE_QA.md) for coverage and remaining gaps.

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

Literal search uses a trigram index before inspecting full role text, with SQL
wildcards escaped. Database connections enforce a 12-second statement timeout
and a five-second lock timeout. A function-local timeout alone does not reliably
bound a statement already running when the function starts.

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
completion. The tokenizer assets are bundled with the function and checked by
SHA-256 tests. A conservative geometric shrink replaces repeated binary-search
tokenization. The worker checkpoints at most two pieces per invocation; larger
batches must be revalidated against the hosted CPU limit before increasing them.

Raw archive rows are never updated, deleted or given new triggers. Frame builds
read a fixed ready generation, preserve full text in the derived frame snapshot,
and verify expected active/inactive counts. Re-running a batch is idempotent.
Frame identity includes generation and source URI; content hashes bind embeddings
to the exact snapshot. Do not treat a turn index as a cross-generation message ID.

The dispatcher is installed disabled. Operator setup supplies a Vault secret,
dedicated expiring embedding token, approved endpoint, stop time and request
budget. The scheduled job calls only `archive_vnext.dispatch_backfill()`.
The current dispatcher runs one HTTP request at a time, stops after five failed
responses, and unschedules itself at the deadline, request budget or drained
queue. It uses private direct HTTP with a bounded fifteen-second HTTP timeout
and a thirty-second scheduled SQL deadline. Logged embedding jobs and lease tokens provide
durability. A lost HTTP response cannot acknowledge another worker's lease.

The original `pg_net` transport was replaced after testing showed that its
platform-owned queue grants could not be revoked by the project operator.
The active credential is never submitted to that queue; former queue credentials
were revoked. The historical grant migration is not a security boundary.

First-time operator setup, after `finish_build` succeeds, can run entirely
inside the database. Replace the endpoint placeholder with the target project.
This creates a seven-day, embedding-only credential without returning its value;
running it a second time fails instead of silently rotating an active worker.

```sql
begin;
select set_config('vnext.backfill_endpoint',
  'https://YOUR_PROJECT_REF.supabase.co/functions/v1/crowley-archive-vnext', true);
do $$
declare token text; g text;
begin
  select active_generation_id into g from archive_private.corpus_state;
  if not exists(select 1 from archive_vnext.builds
    where generation_id=g and status='indexed') then
    raise exception 'finish the frame build first';
  end if;
  token:=encode(extensions.gen_random_bytes(32),'hex');
  perform vault.create_secret(token,'archive_vnext_embedding_worker_v1',
    'Bounded vNext embedding backfill; seven-day expiry');
  insert into crowley_v2.api_tokens
    (token_sha256,label,scopes,expires_at,minute_limit)
    values(encode(extensions.digest(token,'sha256'),'hex'),
      'vnext-embedding-worker-v1',array['embedding:write'],
      now()+interval '7 days',60);
  insert into archive_vnext.backfill_control
    (endpoint,vault_secret_name,enabled,stop_at,request_budget)
    values(current_setting('vnext.backfill_endpoint'),
      'archive_vnext_embedding_worker_v1',true,
      now()+interval '7 days',200000);
end $$;
select cron.schedule('crowley-vnext-embedding-backfill','2 seconds',
  'set statement_timeout=''15s''; select archive_vnext.dispatch_backfill();');
commit;
```

The budget is a ceiling, not a prediction that every frame will finish within it.
Inspect remaining jobs before deliberately extending or restarting a stopped run.
Completion of this job never changes the production connector.

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
`tests/vnext/literal-search.sql` separately verifies literal percent, underscore,
backslash and authorship filtering against the indexed path.

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

A full-corpus trigram build can exceed the management API timeout. The optional
`vnext_literal_index_builder` migration installs a database-side helper. Schedule
its documented one-time job with a ten-minute statement limit; it unschedules
on success or after two failed attempts. Confirm the index is valid, then apply
`vnext_literal_search_index`, whose `IF NOT EXISTS` avoids rebuilding it. This
prebuild may happen before that migration on an existing large database. Record
and verify both migration names; never treat a timed-out response as success.

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

## Operating controls

`crowley_v2.operating_policy` defaults to maintenance disabled. Enable it only
with an explicit protected-until date and an embedding credential valid beyond
that date. `crowley_v2.maintenance_tick()` is suitable for a five-minute schedule.
It expires deadlines and approvals, requeues abandoned worker leases within
existing attempt budgets, and records private health snapshots and alerts.
Intent dispatch is independently disabled until its host delivery path is ready.

Transient embedding circuit breaks can recover after a cooldown, within a daily
recovery cap. A credential failure, deliberate pause, spent request budget,
expired deadline, or terminal job does not automatically restart. Successful HTTP
responses and actual embedding progress are tracked separately. A response that
only reports an idle worker does not count as progress.

`archive_vnext.dispatch_literal_build()` builds bounded, overlapping literal
passages in resumable batches and unschedules on completion. Exact retrieval
uses the passage index only after every frame in the generation is covered.
Passages retain role separation and literal wildcard handling. Quotes crossing
passage or user/assistant boundaries remain searchable. Original source frames
remain immutable. Literal results use a documented newest-first order.

Operational retention only removes disposable usage, dispatch and health logs.
It never removes source data, events, record versions or evaluation evidence.
The existing recovery adapter remains authoritative: a checkpoint is not proof
of a successful restore. An unchanged restore blocker must not be reported as
fixed merely because operating checks succeed.

Read private health with `select crowley_v2.operating_status();`. Alert only on
unrecovered actionable faults. Known stale exports, advancing shadow indexes
and an unrun release benchmark are launch limitations, not recurring incidents.
New archive exports still require the existing ingestion path. A completed
index never authorizes a retrieval cutover.

Run `tests/vnext/operations.sql` and `tests/vnext/literal.sql` only on development;
their synthetic writes roll back. `tests/vnext/tokenizer.test.mjs` validates the
real bundled tokenizer, including long multilingual inputs. Keep real queries,
credentials, health snapshots and operational deployment details private.
