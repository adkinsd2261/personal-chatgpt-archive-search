# Personal ChatGPT Archive Search

A local retrieval system for searchable access to multi-year ChatGPT conversation archives, emphasizing privacy, deterministic results, and hybrid retrieval with evaluation.

## What It Is

This project provides a read-only interface to a private ChatGPT data export. The raw export is preserved unchanged. Derived databases and semantic indexes are derivative; they can always be rebuilt.

Raw data flows through three layers:

```
Private ChatGPT export
    ↓
SQLite database + FTS5 index (lexical search)
    ↓
Local semantic embeddings (optional, in-memory)
    ↓
Intent-aware ranking + filtering
    ↓
Deterministic evidence packet
```

The system serves three interfaces:

- **CLI** (`search_archive.py`): Local search with context window expansion
- **HTTP Service** (`archive_context/service.py`): Deterministic read-only API with bearer token auth
- **Custom GPT Integration** (`CUSTOM_GPT_SETUP.md`): Resident engine as a Custom GPT Action

## Why It Exists

Keyword search is insufficient for large conversational archives containing:

- Long-term history with changing positions over time
- Conversational branches and rejected drafts
- Corrections and final positions vs. earlier drafts
- Duplicate or overlapping material
- Nuanced decisions that require context

The system addresses **retrieval quality as a testing problem**: expected sources are defined upfront, retrieval is evaluated against those sources, and the ranking logic is tuned to reach them consistently.

## What It Solves

### Hybrid Retrieval

- **Lexical search (SQLite FTS5)**: Exact, prefix, and weighted term matching with BM25 scoring
- **Semantic search**: Local in-memory embeddings (sentence-transformers) for intent-based retrieval
- **Rank fusion**: Reciprocal Rank Fusion with role-based weighting (user-authored evidence preferred)

### Intent Handling

Queries are classified into retrieval modes:

- **Recall**: General search for relevant evidence
- **Exact**: Phrase or quote matching
- **Earliest**: Temporal ordering by creation time
- **Latest/Current**: Most recent position
- **Longitudinal**: Timeline diversification (earliest → latest)
- **Correction**: Prioritize recent corrections over earlier drafts
- **Decision**: Planning and directional evidence

### Evidence Ranking

Scoring combines eight dimensions:

- Lexical match strength (BM25-weighted)
- Semantic similarity
- Exact phrase/identifier matches
- Conversation title overlap
- **User-authored preference** (1.0 vs. 0.0 for assistant text)
- Correction signals (user rejection, refinement language)
- Corroboration (multi-turn evidence in same conversation)
- Temporal relevance (distance from oldest/newest)

Assistant-authored rejected drafts are penalized; accepted drafts receive a small boost.

### Result Shaping

- **Duplicate suppression**: Identical evidence removed
- **Conversation caps**: Per-conversation limit to prevent clustering
- **Output budgets**: Light (5 episodes, 9KB), Medium (10 episodes, 18KB), Deep (15 episodes, 30KB)
- **Deterministic packing**: JSON-serialized results respect character limits without slicing mid-field
- **Temporal bucketing** (longitudinal mode): Quarterly representatives + backfill
- **Rejected-context labeling**: Assistant context from rejected turns is marked for transparency

### API / HTTP Service

- Single operation: `POST /api/context` with bearer token auth
- Constant-time token verification (timing-safe digest comparison)
- Request validation: depth, date ranges, query bounds
- Timeout handling: per-request timeout + queue timeout for capacity management
- No query/excerpt logging by default
- Local-only binding (127.0.0.1)
- Binds to configured port via environment variables

## Ownership & Implementation

This project was developed with AI-assisted implementation.

**My responsibility includes:**
- Defining system requirements and expected behaviors
- Architecture decisions (hybrid retrieval, intent handling, evidence ranking)
- Privacy constraints and data-integrity rules
- Evaluation criteria (expected-source matching, Recall@K metrics)
- Test strategy and test expectations
- Iterative troubleshooting and tuning based on evaluation results
- Validation and verification of system behavior

**AI assistance included:**
- Code implementation and refactoring
- Boilerplate generation
- Documentation drafts

## Testing & Evaluation

### Test Coverage

**28 unit/integration/HTTP tests** covering:

- **Synthetic fixtures** (no archive required):
  - Deterministic output (identical queries → identical results)
  - Input validation (depth, dates, bounds)
  - Budget enforcement (character limits respected)
  - Control character and injection sanitization
  - Duplicate suppression and conversation caps
  - Temporal ordering and relevance floors
  - Authentication and authorization
  - Request/response shape validation
  - HTTP status codes and error handling

- **Real-engine tests** (with synthetic database):
  - End-to-end ranking with correction signals
  - User-authored evidence prioritization
  - Rejected-context labeling

### Real-Archive Evaluation

An **8-case evaluation set** with explicitly defined expected sources (not committed, privacy-protected):

- **100% Recall@5** on all cases (expected source in top 5 results)
- **100% Recall@10** on all cases (expected source in top 10 results)
- **100% user-primary precision** (all top results are user-authored, not assistant)
- **Zero rejected-assistant leakage** (no assistant-rejected drafts in results)
- **Deterministic output** (repeated identical queries return identical result packets)

**Notable case**: A known current-state retrieval placed the expected source at baseline rank 26. After tuning intent-aware ranking and current-state handling, the same case returned rank 2, demonstrating the effectiveness of correction-signal detection and longitudinal ordering.

### Verification

- SQLite and semantic-index file hashes remain stable across evaluation runs
- Cold startup + first request: <3 seconds on this machine
- Warm p95 latency: <2 seconds
- Archive integrity: SHA-256 verification of raw export files

## Important Locations

| Path | Purpose |
|------|---------|
| `raw/chatgpt-export/` | Raw export (copied, never edited; not committed) |
| `index/archive.sqlite` | Normalized database with FTS5 index (not committed) |
| `index/semantic/` | Local embeddings and chunk IDs (not committed) |
| `archive_context/` | Deterministic retrieval engine and HTTP service |
| `tools/` | CLI tools: search, build index, validate, evaluate |
| `tests/` | Unit/integration/HTTP tests (synthetic fixtures) |
| `evals/context_gold.local.json` | Private evaluation queries (not committed) |
| `evals/results/` | Evaluation metrics (not committed) |
| `canon/` | Final accepted artifacts (e.g., polished songs, documents) |
| `reports/` | Longitudinal analyses derived from archive |
| `CUSTOM_GPT_SETUP.md` | Setup and operation for Custom GPT integration |
| `MCP_MIGRATION.md` | Alternative MCP adapter documentation |

## Usage

### Manual Search (CLI)

From PowerShell in this folder:

```powershell
.\.venv\Scripts\python.exe .\tools\search_archive.py --query "when did I start building Crowley" --limit 12 --context 1
```

Or use the convenience wrapper:

```powershell
& '.\Search Archive.cmd' "when did I start building Crowley"
```

For longitudinal questions:

```powershell
.\.venv\Scripts\python.exe .\tools\search_archive.py --query "confidence style growth" --timeline --limit 16
```

Open the complete source window for a result:

```powershell
.\.venv\Scripts\python.exe .\tools\open_context.py --conversation-id CONVERSATION_ID --turn 12 --before 3 --after 3
```

### Build the Base Index

```powershell
.\.venv\Scripts\python.exe .\tools\build_index.py
```

The base index uses only Python's standard library and SQLite FTS5. Semantic embeddings are optional and do not require cloud services.

### Add Semantic Search (Optional)

```powershell
.\.venv\Scripts\python.exe -m pip install -r .\requirements-semantic.txt
.\.venv\Scripts\python.exe .\tools\build_semantic_index.py
```

The embedding model and vectors are derivative. Delete and rebuild without affecting the archive.

### HTTP Service & Custom GPT

See `CUSTOM_GPT_SETUP.md` for launching the deterministic API service and integrating with a Custom GPT Action.

### Run Tests

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

### Validate Archive Integrity

```powershell
$validationOutput = Join-Path $env:TEMP 'archive-validation.json'
.\.venv\Scripts\python.exe .\tools\validate_archive.py --output $validationOutput
```

### Evaluate Against Known Expected Sources

```powershell
.\.venv\Scripts\python.exe .\tools\evaluate_context.py --output .\evals\results\latest.local.json
```

(Requires private `evals/context_gold.local.json` file with expected sources.)

## Updating with a Future Export

1. Preserve the existing raw export.
2. Copy the new export into a dated subfolder under `raw/` (e.g., `raw/chatgpt-export-2027-01/`).
3. Point `build_index.py --raw` at the new folder.
4. Re-run validation and evaluation.

## Privacy Model

The system is designed for **local-first operation** with **explicit privacy boundaries**:

- **Raw export**: Preserved unchanged as ground truth
- **Derived indexes**: Rebuilt from scratch; never persisted as authoritative
- **Semantic processing**: Local; no embeddings sent to external services
- **User-authored evidence preference**: Treats user text as stronger primary evidence than assistant-generated suggestions
- **Rejected-content handling**: Assistant-rejected drafts are labeled and penalized
- **Read-only API**: No mutation, no logging of queries by default
- **Bearer token auth**: Simple, constant-time verification; no session state
- **Intentionally private**: Evaluation queries and real archive data remain local/gitignored

See `docs/PRIVACY_MODEL.md` for more details.

## Project Status

As of August 5, 2026:

- **Corpus**: 3,119 conversations, 122,382 message nodes, 54,864 user messages, 67,518 assistant messages over ~2.75 years
- **Searchable chunks**: 141,482 text segments with FTS5 index
- **Semantic vectors**: 54,029 locally generated embeddings (user-authored messages only)
- **Test status**: 28/28 passing
- **Evaluation**: 8-case real-archive set with 100% Recall@5/10, 100% user-primary precision, zero rejected-assistant leakage
- **Performance**: Cold <3s, warm p95 <2s
- **API**: Deterministic read-only HTTP service with bearer auth

See `PROJECT_STATUS.md` for detailed metrics.

## License

No license specified. This is a private project.
