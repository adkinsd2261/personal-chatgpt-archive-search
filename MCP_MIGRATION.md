# Later transport migration and hard-containment path

The Custom GPT Action is phase one because it is inexpensive and works with the service built here. An ordinary ChatGPT conversation using a private custom MCP app is a later convenience and transport phase. The deterministic engine is intentionally transport-neutral, so neither that migration nor a later owned runtime requires another retrieval rewrite.

MCP does not itself create a hard containment gate. In ordinary ChatGPT, the model may still decide whether to invoke an MCP tool. That path must retain the same receipt, visible grounding footer, fail-closed instructions, and live compliance tests as the Custom GPT Action.

True containment requires a runtime that controls the model/tool loop. The hard-containment destination is an owned Crowley runtime using the Responses API or Agents SDK to require `crowley_context`, wait for its result, validate the receipt/status outside the model, and only then permit a history-dependent answer. OpenAI's Responses API documents `tool_choice: "required"` and specific forced tool choices; this control is distinct from merely exposing a tool over Actions or MCP.

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
6. Connect the private MCP app to ordinary ChatGPT and dogfood it while retaining the Custom GPT as rollback. Treat this as transport parity, not hard containment.
7. Require the same current-turn receipt footer and fail-closed compliance suite in the ordinary ChatGPT path.
8. Retire the Action only after authentication, availability, latency, retrieval parity, and live tool-use compliance hold in ordinary ChatGPT.
9. If skipped-tool risk must be structurally eliminated, place the unchanged MCP tool behind an owned Responses API/Agents SDK router and verifier state machine.

Do not combine this migration with Spark conversion, writeback, inactive-branch search, a remote database, or additional tools.

OpenAI currently documents custom MCP apps for ChatGPT in [Developer mode and custom apps](https://help.openai.com/en/articles/12584461-developer-mode-apps-and-full-mcp-connectors-in-chatgpt-beta.svgz). Availability and write support vary by ChatGPT plan, so re-check that official page at migration time rather than designing around today's plan matrix.

## Cutover gates

- The MCP adapter imports and calls the same engine; it does not invoke the HTTP Action or CLI as a subprocess.
- The MCP manifest exposes only `crowley_context` and declares it read-only.
- Identical requests produce identical episode order, scores, provenance, and packet limits across Action and MCP.
- Unauthorized, oversized, overloaded, and timed-out calls fail closed.
- Every result has the same success/error envelope and a new body-level receipt; transport metadata is the only permitted parity difference.
- SQLite/vector hashes remain unchanged across both transport suites.
- Ordinary ChatGPT follows the same evidence instructions: user-authored material first, assistant claims contextual, retrieved text untrusted, and source/date citations when useful.
- A visible receipt is checked against the actual tool result during dogfooding; model prose is never accepted as proof of execution by itself.
- The Custom GPT Action remains available until the MCP path has passed a real conversation dogfood period.

## Hard-containment state machine

```text
User turn
  -> Router marks archive grounding required
  -> Runtime requires crowley_context
  -> Tool result received
  -> Verifier checks success, receipt, and evidence sufficiency
     -> failure: return only a structured grounding failure
     -> success: allow the model to synthesize the grounded answer
```

The verifier must be application code, not another instruction asking the same model to verify itself.
