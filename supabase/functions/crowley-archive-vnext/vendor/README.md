# Pinned tokenizer assets

Source: [Supabase/gte-small](https://huggingface.co/Supabase/gte-small/tree/93b36ff09519291b77d6000d2e86bd8565378086), revision
`93b36ff09519291b77d6000d2e86bd8565378086`.

These public tokenizer assets are bundled with the Edge Function to remove a
cold-start network dependency. They contain no archive data or credentials.

SHA-256:

- `tokenizer.json`: `da0e79933b9ed51798a3ae27893d3c5fa4a201126cef75586296df9b4d2c62a0`
- `tokenizer_config.json`: `73687f47b47aedc8bfa8712f7e6616450058f1f1bb3d5e5861f8a92964d6467a`

Do not replace these independently of the model/tokenizer revision and regression
checks. Embeddings retain their model, tokenizer revision and original character
coverage metadata.

The pinned upstream model card declares the model and tokenizer MIT-licensed.
Source model card: https://huggingface.co/Supabase/gte-small/blob/93b36ff09519291b77d6000d2e86bd8565378086/README.md
