# Archive disaster recovery

The personal archive is backed up to the private Cloudflare R2 bucket
`archive-recovery` with restic client-side encryption.

The recovery bundle contains:

- immutable files under `raw/`;
- an online, transactionally consistent snapshot of `index/archive.sqlite`;
- semantic vectors, chunk IDs, and index manifests;
- project configuration and status metadata.

It deliberately excludes `.venv`, caches, `index/semantic/model-cache`, and
SQLite WAL/SHM files. Those are replaceable or unsafe to copy directly.

## Commands

```powershell
.\.venv\Scripts\python.exe .\tools\archive_backup.py status
.\.venv\Scripts\python.exe .\tools\archive_backup.py backup --tag manual
.\.venv\Scripts\python.exe .\tools\archive_backup.py check --read-data
.\.venv\Scripts\python.exe .\tools\archive_backup.py drill
```

`drill` restores into an isolated temporary directory, verifies every SHA-256
listed in the manifest, runs SQLite `PRAGMA integrity_check`, and deletes the
temporary restored data. It never replaces the live archive.

## Required recovery information

The following must be retained outside the old Windows installation:

1. Repository URL:
   `s3:https://14406b179061f37844162743c9474361.r2.cloudflarestorage.com/archive-recovery`
2. The restic repository password.
3. Cloudflare account access. A new bucket-scoped R2 token can be generated if
   the original token is unavailable.

The local `.archive-backup/*.dpapi` files are sealed to the current Windows
user and are intentionally not portable to the new computer.

## Cloud hardening and schedule

- `archive_recovery` is an account token restricted to Object Read & Write on
  `archive-recovery`; it cannot access `crowley-recovery`.
- The R2 bucket is private and uses Standard storage.
- The `data/` pack prefix has a 30-day bucket lock. Recent encrypted pack data
  cannot be overwritten or deleted, while older packs remain prunable later.
- Windows runs an encrypted backup every Sunday at 03:30 and a full remote-data
  integrity check on the first day of each month at 04:30.
