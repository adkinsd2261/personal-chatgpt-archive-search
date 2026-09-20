# Messaging lane QA — shadow only

The private archive supplied these patterns before testing. Conversation modes
and infrastructure flows are separate coverage dimensions, not a classifier or
permanent memory ontology.

| Lane | Acceptance |
| --- | --- |
| Casual | Concrete callbacks, natural brevity, no invented familiarity |
| Emotional | Distinguish context, venting, analysis and requests for action |
| Analytical | Separate evidence, inference and speculation |
| Learning | Resolve numbers and follow-ups in the active thread; honor pivots |
| Creative | Preserve editing constraints and proposed/rejected/adopted versions |
| Decisions and planning | Preserve constraints and phase; drafting is not sending |
| Execution and troubleshooting | Produce the artifact and verify the real result |
| Historical recall | Open originals; retain dates, roles and branches; avoid false completeness |
| Live state and board | Read current verified records and active obligations |
| Preferences | Scope explicit preferences, retain versions, reject stale writes |
| Future intentions | Persist, check conditions, deduplicate, cancel and verify delivery |
| Attachments | Inspect actual bytes and modality, not just a transcript |
| Workers and notifications | Bound capabilities, validate results, wake runtime, verify delivery |

All lanes cross corrections, topic changes, provenance, freshness, timeouts,
authentication and disclosure limits.

## Scope of evidence

The frozen private suite has 32 source-guided retrieval/replay fixtures across 22
conversations: 20 development and 12 withheld until the final run. Formulations
include words from known sources. These test availability and API mechanics,
**not unaided query planning or an independent held-out release benchmark**.

The runner initializes a real MCP client, discovers tools, pins the generation,
queries, opens evidence and hashes complete pages. It never uses gold IDs when
choosing what to open. Answer quality requires separate review. Six questions
also went through v3; its different internal budgets prohibit a fair A/B quality
or latency claim from these traces.

SQL tests cover state/preferences, conflicts, due events, fanout, cancellation,
workers, approvals and access boundaries. Synthetic data rolls back. HTTP tests
cover real auth, scope separation, schemas, dates, branches and degradation.

Manual dogfooding uses the current host assistant. The foundation has no configured
autonomous model adapter, message gateway, attachment processor or notification
transport. Passing SQL transitions does not establish those user flows end to
end. Production V2 records, intents, workers and runtime runs remained empty.

## Repairs

| Reproduced failure | Repair |
| --- | --- |
| Sparse vectors promoted unrelated early conversations | Use lexical coverage until embeddings finish; report degradation |
| Null lease stranded work | Validate non-null bounded durations |
| Fanout repeated its first page | Exclude emitted intent/event/version keys; 21 intents drain as 20, 1, 0 |
| Approved payload or expiry could change | Immutable identity and payload hash constraint |
| Inert/impossible triggers | Validate event kind, due time and expiry |
| Unsupported scope silently disappeared | Reject unsupported arguments and nested scope overrides |
| Dates shifted and cursors lost precision | Validate calendar dates; preserve strings through validation and explicit SQL text casts |
| Schemas omitted supported filters | Expose literal and multi-query date filters |
| Timeline mixed branches and omitted freshness | Inactive branches opt in; generation, status and cursor explicit |

Opening a source now supplies bounded adjacent-turn navigation and declares the
absence of attachment bytes. Navigation snippets must be reopened for evidence.

## Reproduce

```bash
npm test
npx --yes deno@2.9.6 check --no-config --node-modules-dir=manual supabase/functions/crowley-archive-vnext/index.ts
```

Run `tests/vnext/integration.sql`, `workflows.sql` and `literal-search.sql` only on
isolated development. If retained HTTP fixtures use the integration generation,
choose distinct generation/conversation identifiers for the rollback-only test.

For HTTP tests provide `ARCHIVE_QA_ENDPOINT`, `ARCHIVE_QA_TOKEN_FILE` and
`ARCHIVE_QA_GENERATION`; set `ARCHIVE_QA_SYNTHETIC=1` only for the documented
development fixture. Seed `tests/vnext/cursor-fixture.sql` in that isolated synthetic
development corpus before running `node tests/vnext/live.mjs` with an expiring reader.

For replays provide the endpoint and token-file environment variables:

```bash
node tools/vnext/dogfood.mjs private/suite.json private/run-new.json --split=development
```

The runner checks the frozen suite hash, refuses to replace the suite or existing
results, and prints only IDs and aggregates. Sensitive fixtures/results stay in
ignored `private/` and private evaluation storage.

The first full run hit the hourly disclosure quota. Keep those denials and finish
blocked cases after the normal window resets. Do not rotate tokens or reset
counters to manufacture a successful run. Budget metadata and excerpts as well
as opened text.

## Live QA findings

The initial 32-case replay found expected evidence in the fused top eight for
25 cases and completely opened the expected original for 23. Ten cases had
errors: four retrieval failures and six affected by the disclosure quota.
Those initial errors remain in private results.

After the normal quota window reset, all six affected cases completed their
searches without a transport or retrieval error. One still exhausted its 40,000
character opening budget before completing the expected original. Using those
follow-ups gives expected evidence in 29/32 top-eight results and complete
expected originals for 27/32 cases. These are source-guided availability figures,
not answer accuracy or a release result. The follow-up used server-side HTTP
because the local execution environment disconnected; latency is not comparable.

Manual review followed corrections and adjacent turns, distinguished an adopted
creative title from an assistant suggestion, respected a later lesson pivot,
and refused current facts, exhaustive counts, scientific validation, or document
rendering claims unsupported by the available evidence. This is host-assisted
dogfooding, not an autonomous model adapter.

The final real HTTP cursor replay caught a second precision loss: the PostgreSQL
driver serialized inferred timestamp parameters through JavaScript Date even
after validation preserved the original string. Explicit text parameters followed
by PostgreSQL timestamp casts fix all five search/timeline date parameters.
Two synthetic messages one microsecond apart now page distinctly, followed by
an empty terminal page; both search modes retain the exact date boundary.
The live regression is in `tests/vnext/live.mjs`; its fixture is isolated synthetic
data. A SQL-only cursor test had not caught this API/driver boundary.

## Still blocked

Embeddings remain incomplete. Broad searches can hit the statement deadline with
cold data. The exact-phrase plan showed thousands of trigram false positives
requiring heap rechecks; a faster warm repeat does not fix that failure.

The source cutoff has not advanced. Current facts need current sources. The
model/runtime and delivery adapters, independent answer grading, 50 real questions
with 20 held-out cases, fair latency/cost comparison and remaining
[release requirements](VNEXT_BENCHMARK_CONTRACT.md) remain gates. v3 stays live.
