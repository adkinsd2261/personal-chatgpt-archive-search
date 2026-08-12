#!/usr/bin/env python3
"""Encrypted disaster recovery for the personal ChatGPT archive.

The live SQLite database is never copied directly. A consistent online SQLite
backup is staged alongside the immutable raw export, semantic vectors, and
manifests. Restic then encrypts and uploads that verified recovery bundle.
"""

from __future__ import annotations

import argparse
import base64
import ctypes
import hashlib
import json
import os
import platform
import secrets
import shutil
import socket
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


ROOT = Path(__file__).resolve().parent.parent
RUNTIME_DIR = ROOT / ".archive-backup"
CONFIG_PATH = RUNTIME_DIR / "config.json"
ACCESS_KEY_PATH = RUNTIME_DIR / "access-key.dpapi"
SECRET_KEY_PATH = RUNTIME_DIR / "secret-key.dpapi"
PASSWORD_PATH = RUNTIME_DIR / "repository-password.dpapi"
STAGING_ROOT = RUNTIME_DIR / "staging"
STAGING_DIR = STAGING_ROOT / "current"
DRILLS_DIR = RUNTIME_DIR / "drills"
LOG_PATH = RUNTIME_DIR / "backup.log"
DATABASE_PATH = ROOT / "index" / "archive.sqlite"
DEFAULT_RESTIC_WINDOWS = Path(os.environ.get("LOCALAPPDATA", "")) / "Restic" / "restic.exe"
SCHEMA_VERSION = 1


class BackupError(RuntimeError):
    pass


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


class _DataBlob(ctypes.Structure):
    _fields_ = [
        ("cbData", ctypes.c_ulong),
        ("pbData", ctypes.POINTER(ctypes.c_ubyte)),
    ]


def _dpapi(data: bytes, *, decrypt: bool) -> bytes:
    if os.name != "nt":
        raise BackupError("Windows DPAPI is required for local credential sealing")
    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32
    input_buffer = ctypes.create_string_buffer(data)
    input_blob = _DataBlob(
        len(data), ctypes.cast(input_buffer, ctypes.POINTER(ctypes.c_ubyte))
    )
    output_blob = _DataBlob()
    flags = 0x01  # CRYPTPROTECT_UI_FORBIDDEN
    if decrypt:
        ok = crypt32.CryptUnprotectData(
            ctypes.byref(input_blob), None, None, None, None, flags, ctypes.byref(output_blob)
        )
    else:
        description = ctypes.c_wchar_p("Archive encrypted backup credential")
        ok = crypt32.CryptProtectData(
            ctypes.byref(input_blob), description, None, None, None, flags,
            ctypes.byref(output_blob),
        )
    if not ok:
        raise ctypes.WinError()
    try:
        return ctypes.string_at(output_blob.pbData, output_blob.cbData)
    finally:
        kernel32.LocalFree(output_blob.pbData)


def save_secret(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    protected = _dpapi(value.encode("utf-8"), decrypt=False)
    path.write_bytes(base64.b64encode(protected))


def load_secret(path: Path) -> str:
    if not path.is_file():
        raise BackupError(f"Missing sealed credential: {path}")
    protected = base64.b64decode(path.read_bytes(), validate=True)
    return _dpapi(protected, decrypt=True).decode("utf-8")


def copy_to_windows_clipboard(value: str) -> None:
    result = subprocess.run(
        ["powershell.exe", "-NoProfile", "-Command", "Set-Clipboard -Value ([Console]::In.ReadToEnd())"],
        input=value,
        text=True,
        capture_output=True,
        check=False,
        timeout=10,
    )
    if result.returncode != 0:
        raise BackupError("Could not place recovery password on the Windows clipboard")


def configure(repository: str) -> int:
    repository = repository.strip()
    if not repository.startswith("s3:https://") or not repository.endswith("/archive-recovery"):
        raise BackupError("Repository must be the HTTPS S3 URL for archive-recovery")
    access_key = load_secret(ACCESS_KEY_PATH)
    secret_key = load_secret(SECRET_KEY_PATH)
    if len(access_key) != 32 or len(secret_key) != 64:
        raise BackupError("The sealed R2 credentials have an unexpected shape")

    if PASSWORD_PATH.is_file():
        repository_password = load_secret(PASSWORD_PATH)
    else:
        repository_password = secrets.token_urlsafe(36)
        save_secret(PASSWORD_PATH, repository_password)

    config = {
        "schema_version": SCHEMA_VERSION,
        "repository": repository,
        "bucket": "archive-recovery",
        "account_id": "14406b179061f37844162743c9474361",
        "restic_path": str(DEFAULT_RESTIC_WINDOWS),
        "host": socket.gethostname(),
        "configured_at": utc_now(),
        "schedule": {"backup": "weekly Sunday 03:30", "check": "monthly day 1 04:30"},
    }
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(json.dumps(config, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    copy_to_windows_clipboard(repository_password)
    print(f"Configured encrypted repository: {repository}")
    print("The restic recovery password is now on the Windows clipboard.")
    print("Save it in a password manager; DPAPI credentials do not migrate to another PC.")
    return 0


def load_config() -> dict[str, Any]:
    if not CONFIG_PATH.is_file():
        raise BackupError("Backup is not configured")
    value = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    if not isinstance(value, dict) or not value.get("repository"):
        raise BackupError("Invalid backup configuration")
    return value


def restic_path(config: dict[str, Any]) -> Path:
    candidate = Path(str(config.get("restic_path", "")))
    if not candidate.is_file():
        discovered = shutil.which("restic")
        candidate = Path(discovered) if discovered else candidate
    if not candidate.is_file():
        raise BackupError(f"restic executable not found: {candidate}")
    return candidate


def restic_environment(config: dict[str, Any]) -> dict[str, str]:
    env = os.environ.copy()
    env.update(
        {
            "RESTIC_REPOSITORY": str(config["repository"]),
            "RESTIC_PASSWORD": load_secret(PASSWORD_PATH),
            "AWS_ACCESS_KEY_ID": load_secret(ACCESS_KEY_PATH),
            "AWS_SECRET_ACCESS_KEY": load_secret(SECRET_KEY_PATH),
            "AWS_DEFAULT_REGION": "auto",
        }
    )
    return env


def run_restic(
    args: list[str], *, capture: bool = False, check: bool = True, cwd: Path = ROOT
) -> subprocess.CompletedProcess[str]:
    config = load_config()
    result = subprocess.run(
        [str(restic_path(config)), *args],
        cwd=cwd,
        env=restic_environment(config),
        text=True,
        capture_output=capture,
        check=False,
    )
    if check and result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        raise BackupError(f"restic failed ({result.returncode}): {detail}")
    return result


def _relative_files(root: Path) -> Iterable[Path]:
    if not root.is_dir():
        return []
    return (path.relative_to(ROOT) for path in root.rglob("*") if path.is_file())


def should_include(relative: Path) -> bool:
    parts = tuple(part.lower() for part in relative.parts)
    if relative == Path("index/archive.sqlite"):
        return False
    if relative.name.lower() in {"archive.sqlite-wal", "archive.sqlite-shm"}:
        return False
    if parts[:3] == ("index", "semantic", "model-cache"):
        return False
    return True


def source_files() -> list[Path]:
    candidates: list[Path] = []
    for root in (ROOT / "raw", ROOT / "index", ROOT / "manifests"):
        candidates.extend(_relative_files(root))
    for relative in (Path("config.json"), Path("PROJECT_STATUS.md")):
        if (ROOT / relative).is_file():
            candidates.append(relative)
    return sorted({path for path in candidates if should_include(path)})


def _link_or_copy(source: Path, target: Path) -> str:
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.link(source, target)
        return "hardlink"
    except OSError:
        shutil.copy2(source, target)
        return "copy"


def _sqlite_snapshot(source_db: Path, target_db: Path) -> dict[str, Any]:
    target_db.parent.mkdir(parents=True, exist_ok=True)
    source = sqlite3.connect(f"file:{source_db}?mode=ro", uri=True, timeout=60)
    destination = sqlite3.connect(target_db)
    try:
        source.backup(destination, pages=4096)
        destination.commit()
        quick = str(destination.execute("PRAGMA quick_check").fetchone()[0])
        integrity = str(destination.execute("PRAGMA integrity_check").fetchone()[0])
    finally:
        destination.close()
        source.close()
    if quick.lower() != "ok" or integrity.lower() != "ok":
        raise BackupError(f"SQLite snapshot validation failed: quick={quick}, integrity={integrity}")
    return {
        "relative_path": "index/archive.sqlite",
        "size_bytes": target_db.stat().st_size,
        "sha256": sha256_file(target_db),
        "quick_check": quick,
        "integrity_check": integrity,
    }


def _safe_remove_stage(path: Path) -> None:
    resolved = path.resolve()
    staging = STAGING_ROOT.resolve()
    if resolved.parent != staging or resolved.name not in {"current"} and not resolved.name.startswith(".partial-"):
        raise BackupError(f"Refusing to remove unmanaged path: {resolved}")
    if resolved.exists():
        shutil.rmtree(resolved)


def create_bundle() -> dict[str, Any]:
    if not DATABASE_PATH.is_file():
        raise BackupError(f"Archive database not found: {DATABASE_PATH}")
    STAGING_ROOT.mkdir(parents=True, exist_ok=True)
    partial = STAGING_ROOT / f".partial-{secrets.token_hex(12)}"
    partial.mkdir(parents=True, exist_ok=False)
    try:
        transfer_modes = {"hardlink": 0, "copy": 0}
        for relative in source_files():
            mode = _link_or_copy(ROOT / relative, partial / relative)
            transfer_modes[mode] += 1
        database = _sqlite_snapshot(DATABASE_PATH, partial / "index" / "archive.sqlite")
        files: list[dict[str, Any]] = []
        for path in sorted(item for item in partial.rglob("*") if item.is_file()):
            relative = path.relative_to(partial).as_posix()
            files.append(
                {"relative_path": relative, "size_bytes": path.stat().st_size, "sha256": sha256_file(path)}
            )
        manifest = {
            "schema_version": SCHEMA_VERSION,
            "created_at": utc_now(),
            "database": database,
            "files": files,
            "file_count": len(files),
            "total_bytes": sum(item["size_bytes"] for item in files),
            "transfer_modes": transfer_modes,
            "machine": {"hostname": socket.gethostname(), "platform": platform.platform()},
            "excluded": [".venv", ".cache", "index/semantic/model-cache", "SQLite WAL/SHM"],
        }
        payload = (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode("utf-8")
        (partial / "manifest.json").write_bytes(payload)
        (partial / "manifest.sha256").write_text(hashlib.sha256(payload).hexdigest() + "\n", encoding="utf-8")
        if STAGING_DIR.exists():
            _safe_remove_stage(STAGING_DIR)
        partial.rename(STAGING_DIR)
        return manifest
    except Exception:
        if partial.exists():
            _safe_remove_stage(partial)
        raise


def init_repository() -> int:
    result = run_restic(["snapshots", "--json"], capture=True, check=False)
    if result.returncode != 0:
        combined = f"{result.stdout}\n{result.stderr}".lower()
        if "repository does not exist" in combined or "unable to open config file" in combined:
            run_restic(["init"])
        else:
            raise BackupError(combined.strip())
    print("Archive restic repository is initialized.")
    return 0


def backup(tag: str) -> int:
    manifest = create_bundle()
    config = load_config()
    try:
        result = run_restic(
            [
                "backup", "current", "--host", str(config.get("host") or socket.gethostname()),
                "--tag", "archive", "--tag", tag, "--json",
            ],
            capture=True,
            cwd=STAGING_ROOT,
        )
        summary: dict[str, Any] = {}
        for line in result.stdout.splitlines():
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            if item.get("message_type") == "summary":
                summary = item
        print(json.dumps({
            "status": "ok",
            "snapshot_id": summary.get("snapshot_id"),
            "created_at": manifest["created_at"],
            "file_count": manifest["file_count"],
            "total_bytes": manifest["total_bytes"],
            "database_sha256": manifest["database"]["sha256"],
            "data_added": summary.get("data_added"),
        }, indent=2, sort_keys=True))
    finally:
        if STAGING_DIR.exists():
            _safe_remove_stage(STAGING_DIR)
    return 0


def check_repository(read_data: bool) -> int:
    args = ["check"]
    if read_data:
        args.append("--read-data")
    run_restic(args)
    print("Archive backup repository check passed.")
    return 0


def _find_manifest(target: Path) -> Path:
    manifests = sorted(
        path
        for path in target.rglob("manifest.json")
        if path.with_name("manifest.sha256").is_file()
    )
    if len(manifests) != 1:
        raise BackupError(
            f"Expected one signed restored recovery manifest; found {len(manifests)}"
        )
    return manifests[0]


def verify_restored_bundle(target: Path) -> dict[str, Any]:
    manifest_path = _find_manifest(target)
    sidecar = manifest_path.with_name("manifest.sha256")
    if not sidecar.is_file() or sidecar.read_text(encoding="utf-8").strip() != sha256_file(manifest_path):
        raise BackupError("Restored manifest checksum mismatch")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    bundle = manifest_path.parent
    for item in manifest["files"]:
        path = bundle / item["relative_path"]
        if not path.is_file() or path.stat().st_size != item["size_bytes"] or sha256_file(path) != item["sha256"]:
            raise BackupError(f"Restored file verification failed: {item['relative_path']}")
    db_path = bundle / manifest["database"]["relative_path"]
    connection = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        integrity = str(connection.execute("PRAGMA integrity_check").fetchone()[0])
    finally:
        connection.close()
    if integrity.lower() != "ok":
        raise BackupError(f"Restored database integrity failed: {integrity}")
    return {
        "status": "ok", "manifest": str(manifest_path), "file_count": manifest["file_count"],
        "total_bytes": manifest["total_bytes"], "database_integrity": integrity,
    }


def restore_snapshot(snapshot: str, target: Path) -> None:
    result = run_restic(
        ["restore", snapshot, "--target", str(target), "--tag", "archive"],
        capture=True,
        check=False,
    )
    if result.returncode == 0:
        return
    combined = f"{result.stdout}\n{result.stderr}"
    if os.name == "nt" and "failed to restore timestamp" in combined and "Summary: Restored" in combined:
        print("WARNING: Windows refused a directory timestamp; file verification continues.", file=sys.stderr)
        return
    raise BackupError(f"restic restore failed ({result.returncode}): {combined.strip()}")


def drill() -> int:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    target = DRILLS_DIR / stamp
    target.mkdir(parents=True, exist_ok=False)
    try:
        restore_snapshot("latest", target)
        result = verify_restored_bundle(target)
        result["drilled_at"] = utc_now()
        report = DRILLS_DIR / f"{stamp}.json"
        report.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        print(json.dumps(result, indent=2, sort_keys=True))
    finally:
        shutil.rmtree(target, ignore_errors=True)
    return 0


def status() -> int:
    snapshots = json.loads(run_restic(["snapshots", "--json", "--tag", "archive"], capture=True).stdout or "[]")
    print(json.dumps({
        "configured": True,
        "repository": load_config()["repository"],
        "snapshot_count": len(snapshots),
        "latest": snapshots[-1] if snapshots else None,
        "scheduled_tasks": windows_schedule_status(),
    }, indent=2, sort_keys=True))
    return 0


def _write_windows_wrappers() -> tuple[Path, Path]:
    python = Path(sys.executable).resolve()
    script = Path(__file__).resolve()
    backup_cmd = RUNTIME_DIR / "run-backup.cmd"
    check_cmd = RUNTIME_DIR / "run-check.cmd"
    backup_cmd.write_text(
        f'@echo off\r\n"{python}" "{script}" backup --tag scheduled >> "{LOG_PATH}" 2>&1\r\n', encoding="utf-8"
    )
    check_cmd.write_text(
        f'@echo off\r\n"{python}" "{script}" check --read-data >> "{LOG_PATH}" 2>&1\r\n', encoding="utf-8"
    )
    return backup_cmd, check_cmd


def install_windows_schedule() -> int:
    if os.name != "nt":
        raise BackupError("Windows scheduling is required")
    load_config()
    load_secret(PASSWORD_PATH)
    backup_cmd, check_cmd = _write_windows_wrappers()
    commands = [
        ["schtasks", "/Create", "/TN", "Archive Encrypted Backup", "/TR", str(backup_cmd), "/SC", "WEEKLY", "/D", "SUN", "/ST", "03:30", "/F"],
        ["schtasks", "/Create", "/TN", "Archive Backup Integrity Check", "/TR", str(check_cmd), "/SC", "MONTHLY", "/D", "1", "/ST", "04:30", "/F"],
    ]
    for command in commands:
        result = subprocess.run(command, capture_output=True, text=True, check=False)
        if result.returncode != 0:
            raise BackupError((result.stderr or result.stdout).strip())
    print("Installed weekly Archive backup and monthly full-data integrity check.")
    return 0


def windows_schedule_status() -> dict[str, bool] | None:
    if os.name != "nt":
        return None
    result: dict[str, bool] = {}
    for name in ("Archive Encrypted Backup", "Archive Backup Integrity Check"):
        query = subprocess.run(["schtasks", "/Query", "/TN", name], capture_output=True, text=True, check=False)
        result[name] = query.returncode == 0
    return result


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    configure_parser = sub.add_parser("configure")
    configure_parser.add_argument("--repository", required=True)
    sub.add_parser("init")
    sub.add_parser("snapshot")
    backup_parser = sub.add_parser("backup")
    backup_parser.add_argument("--tag", default="manual")
    check_parser = sub.add_parser("check")
    check_parser.add_argument("--read-data", action="store_true")
    sub.add_parser("drill")
    sub.add_parser("status")
    sub.add_parser("install-schedule")
    sub.add_parser("copy-recovery-password")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        if args.command == "configure":
            return configure(args.repository)
        if args.command == "init":
            return init_repository()
        if args.command == "snapshot":
            print(json.dumps(create_bundle(), indent=2, sort_keys=True))
            return 0
        if args.command == "backup":
            return backup(args.tag)
        if args.command == "check":
            return check_repository(args.read_data)
        if args.command == "drill":
            return drill()
        if args.command == "status":
            return status()
        if args.command == "install-schedule":
            return install_windows_schedule()
        if args.command == "copy-recovery-password":
            copy_to_windows_clipboard(load_secret(PASSWORD_PATH))
            print("Recovery password copied to the Windows clipboard.")
            return 0
    except (BackupError, OSError, sqlite3.Error, json.JSONDecodeError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
