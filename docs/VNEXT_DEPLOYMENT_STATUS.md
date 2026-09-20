# Deployment verification — 2026-09-20

Status: **shadow implementation deployed; corpus build blocked by infrastructure;
no benchmark win and no cutover**.

- The isolated Supabase development branch is healthy. Its original migration
  replay was recovered by adding missing extension prerequisites and rebasing.
- The Archive/V2 schemas, hardening migration and authenticated vNext endpoint
  are deployed alongside v3 on the main project.
- All 12 Node tests passed. Deno type checking passed. Transactional SQL
  integration passed for retrieval, pagination, roles, inactive branches,
  immutability, freshness, state/history, events, intent lifecycle, worker budgets,
  cancellation, output schemas, approvals, authentication and disclosure limits.
- Live development HTTP tests verified unauthenticated denial, scoped reads,
  MCP tool discovery, hybrid search and checkpointed long-message embeddings.
- The development dispatcher sent three successful bounded requests and stopped
  at its configured request budget. The dispatcher migration is verified on
  development; production scheduling has not been enabled.
- Main-project backfill reached **32,000 of 57,731 active turns** before the
  database entered read-only mode. The 3,249 inactive messages are not indexed
  there yet. The generation remains the original August 24 corpus cutoff.
- Raw turn/history counts and content fingerprints, active generation and the
  v3 entry-function definition matched their pre-deployment baseline after the
  backfill stopped. No existing source table or connector was replaced.
- Supabase still reports Pro, but PostgreSQL reports
  `default_transaction_read_only=on` from its configuration. Observed database
  size was approximately 1.1 GB and WAL approximately 672 MB. This alone does
  not establish the provisioned disk size; inspect Infrastructure before changing
  capacity or attempting more writes. Do not disable the guard to keep indexing.
- A private snapshot of the legacy 25-case development suite is stored in the
  benchmark tables. A fair production comparison is blocked by incomplete
  coverage; no partial-corpus score is presented as a win. The separate held-out
  end-to-end release suite remains required by the frozen gate.

## Resume

1. Resolve the main project's disk allocation/read-only condition through the
   Supabase Infrastructure controls. Verify normal write access and v3 health.
2. Resume the idempotent corpus build (`control.mjs index` safely replays completed
   batches, or continue the turn cursor after raw ID 32000).
3. Verify active and inactive frame counts, then finish the build.
4. Apply the tested dispatcher migration; provision a dedicated expiring
   `embedding:write` token in Vault and start a bounded schedule.
5. Complete embedding coverage, run the frozen component and end-to-end suites,
   verify source fingerprints again, and review the promotion gate.

This file is an operator checkpoint, not a claim that the background job is
running or that retrieval is ready to replace v3. The detailed operational
contracts and commands are in [ARCHIVE_VNEXT_OPERATIONS.md](ARCHIVE_VNEXT_OPERATIONS.md).
