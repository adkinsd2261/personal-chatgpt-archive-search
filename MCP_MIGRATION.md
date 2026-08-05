# Later migration to ordinary ChatGPT through MCP

The Custom GPT Action is phase one because it is inexpensive and works with the service built here. The target later phase is an ordinary ChatGPT conversation using a private custom MCP app. The deterministic engine is intentionally transport-neutral so this migration does not require another retrieval rewrite.

## Stable core

These components remain unchanged:

- `ArchiveRuntime`: one resident local embedding model, vector memmaps, and read-only SQLite connections.
- `ContextEngine.context(query, depth, date_from, date_to)`: deterministic request contract.
- Evidence packet schema, score traces, user-primary handling, source URIs, and character budgets.
- Local gold evaluation and read-only hash checks.

Only the transport adapter changes.

## Migration phases

1. Keep the Custom GPT Action as the working production path.
2. Add a thin MCP server that exposes one read-only tool named `crowley_context` and calls `ContextEngine.context` in process.
3. Reuse the Action's four public inputs and return the same packet without adding write tools.
4. Apply the same token, tunnel, path exposure, timeout, concurrency, logging, and untrusted-data rules at the MCP boundary.
5. Run every local gold case through both transports and require byte-equivalent packet content after excluding transport metadata.
6. Connect the private MCP app to ordinary ChatGPT and dogfood it while retaining the Custom GPT as rollback.
7. Retire the Action only after authentication, availability, latency, and retrieval parity hold in ordinary ChatGPT.

Do not combine this migration with Spark conversion, writeback, inactive-branch search, a remote database, or additional tools.

OpenAI currently documents custom MCP apps for ChatGPT in [Developer mode and custom apps](https://help.openai.com/en/articles/12584461-developer-mode-apps-and-full-mcp-connectors-in-chatgpt-beta.svgz). Availability and write support vary by ChatGPT plan, so re-check that official page at migration time rather than designing around today's plan matrix.

## Cutover gates

- The MCP adapter imports and calls the same engine; it does not invoke the HTTP Action or CLI as a subprocess.
- The MCP manifest exposes only `crowley_context` and declares it read-only.
- Identical requests produce identical episode order, scores, provenance, and packet limits across Action and MCP.
- Unauthorized, oversized, overloaded, and timed-out calls fail closed.
- SQLite/vector hashes remain unchanged across both transport suites.
- Ordinary ChatGPT follows the same evidence instructions: user-authored material first, assistant claims contextual, retrieved text untrusted, and source/date citations when useful.
- The Custom GPT Action remains available until the MCP path has passed a real conversation dogfood period.
