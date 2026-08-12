# Personal ChatGPT Archive Search

This folder is a local, evidence-backed interface to the full ChatGPT data export. The raw export is preserved unchanged. Derived databases and indexes can always be rebuilt.

The installed archive covers November 9, 2023 through July 29, 2026. See `PROJECT_STATUS.md` for verified counts and test status.

## Ask questions in Codex

Open this folder as the Codex workspace and ask a normal question about the archive. `AGENTS.md` instructs Codex to search the corpus and open the relevant original turns before answering.

No agent swarm is required. Codex uses deterministic local search tools and then answers in the current conversation.

## Use it from a Custom GPT

The `archive_context` package adds a resident, deterministic, read-only context service. A Custom GPT can call its single authenticated Action, `crowley_context`, through a narrowly scoped HTTPS tunnel. Every success or handled failure carries a fresh body-level receipt so actual Action use can be checked instead of inferred from model prose. Codex, Crowley, remote models, and search subprocesses are not in that request path.

See `CUSTOM_GPT_SETUP.md` for token creation, launch, tunnel, Action import, GPT instructions, health checks, and rollback. Run `CORRAL_TEST.md` before relying on archive-grounded answers. `MCP_MIGRATION.md` separates the later ordinary-ChatGPT MCP transport from a genuinely required-tool runtime using the Responses API or Agents SDK.

## Important locations

- `raw/chatgpt-export/`: copied source export; never edit these files.
- `index/archive.sqlite`: normalized conversations, messages, turns, and SQLite FTS5 index.
- `tools/search_archive.py`: primary search command.
- `tools/open_context.py`: opens complete neighboring turns from a result.
- `archive_context/`: deterministic retrieval engine and local HTTP service.
- `openapi-action.json`: minimal one-operation Custom GPT Action schema.
- `CORRAL_TEST.md`: 30-prompt live tool-use and fail-closed acceptance test.
- `tools/evaluate_context.py`: ignored local real-archive evaluation harness.
- `manifests/`: verification and index-build records.
- `canon/`: final songs, documents, or other accepted artifacts you choose to preserve.
- `reports/`: longitudinal analyses derived from the archive.

## Manual search

From PowerShell in this folder:

```powershell
.\.venv\Scripts\python.exe .\tools\search_archive.py --query "when did I start building Crowley" --limit 12 --context 1
```

Or use the short wrapper:

```powershell
& '.\Search Archive.cmd' "when did I start building Crowley"
```

For questions about change over time:

```powershell
.\.venv\Scripts\python.exe .\tools\search_archive.py --query "confidence style growth" --timeline --limit 16
```

Open the complete source window for a result:

```powershell
.\.venv\Scripts\python.exe .\tools\open_context.py --conversation-id CONVERSATION_ID --turn 12 --before 3 --after 3
```

## Rebuild the base index

```powershell
.\.venv\Scripts\python.exe .\tools\build_index.py
```

The base index uses only Python's standard library and SQLite FTS5. Semantic embeddings are optional; exact and weighted full-text retrieval works without cloud services.

## Semantic index

The optional semantic layer remains local. By default it embeds user-authored messages only; assistant text remains available through exact search and neighboring-turn expansion. This produces a cleaner model of the user and avoids spending compute on rejected assistant drafts. Install its dependencies, then build it:

```powershell
.\.venv\Scripts\python.exe -m pip install -r .\requirements-semantic.txt
.\.venv\Scripts\python.exe .\tools\build_semantic_index.py
```

The embedding model and vectors are derivative data. They can be deleted and rebuilt without affecting the archive.

## Test the complete project

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

Run the existing archive validation without replacing the checked local report:

```powershell
$validationOutput = Join-Path $env:TEMP 'archive-validation.json'
.\.venv\Scripts\python.exe .\tools\validate_archive.py --output $validationOutput
```

Real-archive context queries and source IDs stay in the ignored `evals/context_gold.local.json` file:

```powershell
.\.venv\Scripts\python.exe .\tools\evaluate_context.py --output .\evals\results\latest.local.json
```

## Updating with a future export

1. Preserve the existing raw export.
2. Copy the new export into a dated subfolder under `raw/`.
3. Point `build_index.py --raw` at the new folder.
4. Re-run the verification and retrieval evaluations.
