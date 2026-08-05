# Local context evaluation

Real-archive evaluation cases belong in `evals/context_gold.local.json`. That file and generated results are ignored because queries and provenance identifiers may be private.

The local file has this shape:

```json
{
  "schema_version": 1,
  "cases": [
    {
      "name": "stable-case-name",
      "query": "private local query",
      "depth": "medium",
      "tags": ["exact"],
      "expected_sources": [
        {"conversation_id": "local-id", "turn_index": 0}
      ],
      "baseline_rank": 2
    }
  ]
}
```

Run `python tools/evaluate_context.py --output evals/results/latest.local.json`. The report contains query hashes rather than query text and verifies that the SQLite and semantic-index hashes remain unchanged.
