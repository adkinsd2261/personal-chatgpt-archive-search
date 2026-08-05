# Archive-backed conversation rules

This workspace is the user's personal ChatGPT archive. For questions about their history, relationships, creative work, projects, tastes, behavior, development, or prior conversations, search the archive before answering.

## Required retrieval workflow

1. Run one broad local search using the user's actual question or its most meaningful terms:

   ```powershell
   .\.venv\Scripts\python.exe .\tools\search_archive.py --query "<question>" --limit 16
   ```

2. For longitudinal questions, include `--timeline` so one intense period does not dominate the answer.
3. Open the strongest sources with `tools/open_context.py`; do not rely on isolated snippets.
4. Refine the local search once if the evidence is weak or dominated by an irrelevant meaning of a term.
5. Answer in the current Codex thread. Do not spawn subagents for archive retrieval.

## Evidence rules

- User-authored messages are primary evidence about the user.
- Assistant-authored claims are not facts about the user unless the user confirmed, adopted, or repeatedly built upon them.
- Distinguish user-written artifacts, assistant proposals, rejected drafts, revisions, and accepted/canonical work.
- Summaries and reports are navigation aids, not substitutes for original turns.
- For important conclusions, name the conversation title and date. Include the `archive://` source identifier when useful.
- Separate direct evidence, repeated patterns, inference, and uncertainty.
- For questions about change over time, deliberately compare early, middle, and recent evidence.
- Never claim exhaustive coverage unless the retrieval performed actually supports it.

## Search guidance

- Exact quotes, lyrics, names, and project identifiers: search their distinctive words.
- Broad themes: search two or three related formulations and compare the original turns.
- Creative work: prefer user-authored lines and explicitly accepted versions over rejected assistant drafts.
- Relationship and mental-health questions: preserve dates and context; do not convert temporary distress into a fixed identity or diagnosis.

The raw files under `raw/chatgpt-export/` are immutable source data. Do not modify or delete them.

