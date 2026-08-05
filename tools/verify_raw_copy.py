from __future__ import annotations

import argparse
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path


def digest(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as handle:
        while block := handle.read(1024 * 1024):
            hasher.update(block)
    return hasher.hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify the copied export against its source.")
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--copy", type=Path, default=Path("raw/chatgpt-export"))
    parser.add_argument("--output", type=Path, default=Path("manifests/raw_copy_manifest.json"))
    args = parser.parse_args()
    source_files = {path.name: path for path in args.source.glob("*.json")}
    copy_files = {path.name: path for path in args.copy.glob("*.json")}
    names = sorted(set(source_files) | set(copy_files))
    records = []
    ok = True
    for name in names:
        source = source_files.get(name)
        copied = copy_files.get(name)
        record = {
            "name": name,
            "source_exists": source is not None,
            "copy_exists": copied is not None,
        }
        if source and copied:
            record.update(
                {
                    "bytes": copied.stat().st_size,
                    "source_sha256": digest(source),
                    "copy_sha256": digest(copied),
                }
            )
            record["matches"] = (
                source.stat().st_size == copied.stat().st_size
                and record["source_sha256"] == record["copy_sha256"]
            )
        else:
            record["matches"] = False
        ok = ok and bool(record["matches"])
        records.append(record)
    manifest = {
        "verified_at": datetime.now(timezone.utc).isoformat(),
        "source": str(args.source.resolve()),
        "copy": str(args.copy.resolve()),
        "all_match": ok,
        "file_count": len(records),
        "total_bytes": sum(record.get("bytes", 0) for record in records),
        "files": records,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    print(json.dumps({key: manifest[key] for key in ("all_match", "file_count", "total_bytes")}, indent=2))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())

