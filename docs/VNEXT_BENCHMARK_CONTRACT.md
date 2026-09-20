# Archive vNext benchmark contract — frozen before retrieval implementation

Version: `vnext-gate-1`, 2026-09-20. Changing this contract requires a new suite
version and a fresh baseline; never tune a threshold to make a run pass.

## Safety and promotion

v3 remains the production entry point. vNext is additive and shadow-only.
No migration, worker, test, or benchmark runner changes the existing connector.
Promotion is a separate, reviewed operation, not an automatic side effect of a
score. The new runtime has no arbitrary SQL, shell, or external-write tools.

## Fair comparison

Freeze the corpus generation, suite contents and hash, gold source IDs, candidate
code hash, model/version, query-expansion budget, result count, character budget,
and embedding configuration before each run. Both systems use the same corpus
and budgets. Cache state, cold/warm latency, errors, and unavailable components
are reported, never silently omitted. A lexical component comparison is not an
end-to-end frontier-model benchmark. Existing regression cases are development
cases, NOT held-out evidence.

The release suite must contain at least 50 real, independently source-checked
questions, including at least 20 held-out cases frozen before tuning. Cover
short/ambiguous replies, assistant-only discoveries, chronology, supersession,
inactive branches, exact quotes, cross-conversation synthesis, negative evidence,
and stale-current-state questions. Synthetic cases test mechanics/security only.

## All gates must pass

1. Mean gold-source recall@8 improves by at least **5 percentage points** over v3
   on the held-out set; report paired bootstrap 95% confidence bounds and require
   a positive lower bound. No scored category may regress by >5 points.
2. Zero unsupported-current-state, assistant-as-user, wrong-branch-as-canonical,
   wrong-provenance, fabricated-citation, or false-exhaustiveness failures.
3. Every returned source is reopenable against the frozen generation. Exact
   quotes round-trip. Long messages and tokenizer overflow are never silently
   truncated. Explicit partial coverage is not counted as complete.
4. Error rate is no worse than v3; warm p95 latency <=1.25x baseline within the
   same resource budget. Report cost and embedding/backfill coverage separately.
5. RLS/grants, authentication, rate/disclosure limits, worker leases/retries,
   cancellation, stale-write rejection, and approval gates pass integration tests.
6. Production raw-data fingerprints and the v3 function/deployment are unchanged.

Missing gold, incomplete embeddings, an unrun evaluator, missing model credentials,
or an incomplete source corpus yields `blocked`, never `passed`. Stored scores
are evidence for human review, not authorization to cut over.

Private queries, answers and fixtures stay in private database tables or ignored
local files. Public Git receives only synthetic tests, code, contracts and
aggregate metrics without private source identifiers.
