# Project Status

Verified complete on August 4, 2026.

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

## Validation

- Seven unit tests passing
- Expected conversation and author-message counts match exactly
- Exact lyric, named-project, life-event, and conceptual retrieval smoke tests passing
- Working launcher: `Search Archive.cmd`

See `manifests/raw_copy_manifest.json`, `manifests/index_manifest.json`, and `manifests/validation.json` for machine-readable evidence.

