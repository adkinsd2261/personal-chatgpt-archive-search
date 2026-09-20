# Deployment verification — 2026-09-20

Status: **shadow deployment verified and running; promotion blocked**.

The subsequent [messaging-lane QA pass](MESSAGING_LANE_QA.md) repaired workflow,
authorization-binding, timeline and retrieval-degradation defects. The updated
checks passed 16 Node tests, Deno type checking, 28 workflow SQL assertions,
the original 44 integration assertions and 14 development HTTP checks.
It also found unresolved search timeouts and missing runtime/delivery paths.
Successful foundation tests do not mean every messaging lane is operational.

Archive vNext and the V2 primitive foundation are deployed alongside v3.
The development branch is healthy. Production continues to use the original
connector and retrieval function.

## Deployed and verified

- All **60,980 frames** are indexed: 57,731 active turns and 3,249 inactive messages.
  Role labels, original message pointers, source hashes, pagination, dates and
  inactive-branch labels are preserved. The source cutoff remains August 24.
- Full-text search, indexed literal search, contextual vector storage, evidence
  reopening, time browsing and the authenticated shadow MCP endpoint are deployed.
- V2 provides versioned state/preferences, immutable events, intents, bounded
  workers, a tool registry, approval records and runtime context/run lineage.
  A frontier-model adapter is still required to run an autonomous assistant.
- Production turn/history counts and content fingerprints, active generation,
  and the original v3 entry-function definition matched the recorded baseline
  after the full frame build. No source archive data was replaced.
- All 12 Node tests and Deno type checking passed. Development SQL integration
  passed for retrieval, roles, freshness, immutability, Unicode pagination,
  state/version conflicts, events, intent lifecycle, worker limits/cancellation,
  output schemas, approvals, authentication, disclosure limits and private grants.
- Additional literal-search tests passed for percent, underscore, backslash and
  authorship filtering. The full-corpus query plan uses the trigram index.
- Live HTTP checks passed for scoped reads, MCP discovery, hybrid discovery,
  long-message embedding checkpoints, unauthenticated denial and separation of
  read credentials from embedding credentials.
- All **67 returned sources** in the final component run reopened successfully.
  All 24 gold sources also passed full-pagination SHA-256 round-trip checks.

The transient database read-only condition cleared before indexing resumed.
The final preservation check ran with normal write access. No read-only guard
was overridden.

## Limited component regression

The final run alternated arms per case after index construction completed.
Both arms used the same frozen generation, 25 legacy development cases, one
query, eight results and a 12-second statement limit. Background embedding
work continued for both arms.

| Measurement | Legacy lexical component | vNext lexical component |
| --- | ---: | ---: |
| Mean gold-source recall@8 | 20% | 44% |
| Timed-out cases | 4 / 25 | 1 / 25 |
| Observed SQL p95 | 12.164 s | 2.478 s |

The recall difference was +24 percentage points; the paired bootstrap interval
was +8 to +40 points. These are development diagnostics. The baseline here is
`connector_search_v1`, not the complete production reasoning workflow.
This is **not a held-out or frontier-model end-to-end win**, and the observed SQL
latency is not an API latency claim. The earlier run that overlapped index
construction was retained privately and superseded by this interleaved run.

Queries, expected sources and full results remain in private database tables.
Only aggregate measurements are published.

## Background work and remaining gate

The embedding worker is running in Supabase. It checkpoints one tokenizer-bounded
piece per invocation, uses durable leases, and has verified successful live
requests. Semantic coverage is still incomplete.

The current run is bounded to **200,000 requests** and stops by
**September 27, 2026 at 10:24 UTC**, at queue completion, or after repeated failures.
These limits are ceilings, not a completion forecast. Inspect remaining/dead jobs
before deliberately extending a stopped run.

Promotion remains blocked until semantic coverage finishes and the frozen
release contract is satisfied: at least 50 real questions including 20 held-out
cases, independent gold checks, end-to-end safety/category grading, fair latency
and cost measurements, and another preservation check. The remaining exact
search timeout is recorded as an error, not hidden.

The foundation does not switch the production route, including after a favorable
component score.

## Transport verification and platform advisory

The active worker uses private direct HTTP. Its current credential is held in
Vault and is never put in the platform HTTP queue. Prior queue credentials were
revoked. The current dispatcher rejects client roles; anonymous and authenticated
roles cannot read decrypted Vault secrets.

Supabase reports the managed `pg_net` extension's public namespace metadata as an
[extension advisory](https://supabase.com/docs/guides/database/database-linter?lint=0014_extension_in_public).
Its objects belong to the platform owner, and the attempted grant revocations
did not establish a private queue. The active worker was changed to avoid that
transport. No ownership controls were bypassed. RLS-without-policy notices for
the private schemas are intentional deny-all defaults.

See [operations and resume commands](ARCHIVE_VNEXT_OPERATIONS.md) and the
[frozen release contract](VNEXT_BENCHMARK_CONTRACT.md).
