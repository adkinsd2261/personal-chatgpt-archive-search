# Project Status

Verified through the local Archive Context MVP on August 5, 2026.

## Corpus

- 34 copied export files, 323,366,614 bytes
- SHA-256 copy verification: all files match the source corpus
- 3,119 conversations
- 122,382 message nodes
- 54,864 user messages
- 67,518 assistant messages
- 119,338 messages on active conversation branches
- 53,553 user/assistant turns
- 141,482 searchable text chunks
- Archive date span: November 9, 2023 through July 29, 2026

## Retrieval

- SQLite FTS5 exact, phrase, prefix, and weighted lexical search
- 54,029 locally generated semantic vectors from user-authored chunks
- Assistant text retained in exact search and surrounding-turn context
- Hybrid rank fusion with extra authority for user-authored evidence
- Timeline diversification for longitudinal questions
- Earliest/latest intent handling
- Full source-window opening with stable `archive://` identifiers
- Heuristic accepted/rejected draft labels for creative-work navigation

## Archive Context MVP

- Transport-neutral deterministic engine in `archive_context/`
- One authenticated read-only HTTP operation: `POST /api/context`
- Static Custom GPT Action operation ID: `crowley_context`
- Resident embedding model and memory-mapped vectors; no per-request subprocess
- User-authored primary evidence with explicitly labeled assistant context
- Exact, earliest, latest, longitudinal, decision, and correction intent handling
- Bounded variants, score traces, seed expansion, duplicate suppression, conversation caps, and temporal relevance gates
- Light/medium/deep limits of 5/10/15 episodes and 9,000/18,000/30,000 serialized characters
- Standard-library service with constant-time bearer verification, strict request limits, two-worker default concurrency, queue timeout, and retrieval timeout
- Uniform Action envelopes with `success`, a fresh body-level receipt, evidence count, structured error codes, and retryability
- Complete HTTP responses remain inside the 9,000/18,000/30,000 character limits through a bounded transport reserve
- Default bind address `127.0.0.1`; no query or excerpt logging by default
- Custom GPT first; later ordinary-ChatGPT MCP transport and owned required-tool runtime documented as distinct phases

## Validation

- 31 offline unit, integration, and HTTP service tests passing, including all seven original tests
- Expected conversation and author-message counts match exactly
- Exact lyric, named-project, life-event, and conceptual retrieval smoke tests passing
- Local eight-case context evaluation: source Recall@5 and Recall@10 100%, user-primary precision 100%, zero rejected-assistant leakage, and deterministic repeated packets
- Known recent state correction improved from baseline rank 26 to rank 2
- Latest measured cold runtime plus first request: 4.93 seconds; warm p95: 1.34 seconds on this machine
- SQLite and semantic file hashes unchanged across the real-archive evaluation
- Thirty-prompt live Custom GPT corral test prepared; it must be completed in Preview because local tests cannot force the hosted GPT's tool-selection behavior
- Working launcher: `Search Archive.cmd`

See the ignored `evals/results/latest.local.json` for private machine-readable context metrics. Existing raw/index/validation manifests remain local and untracked.
