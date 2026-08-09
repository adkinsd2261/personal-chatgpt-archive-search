from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from tools import archive_backup


class ArchiveBackupTests(unittest.TestCase):
    def test_index_filter_excludes_live_database_and_model_cache(self):
        self.assertFalse(archive_backup.should_include(Path("index/archive.sqlite")))
        self.assertFalse(archive_backup.should_include(Path("index/archive.sqlite-wal")))
        self.assertFalse(
            archive_backup.should_include(Path("index/semantic/model-cache/model.bin"))
        )
        self.assertTrue(archive_backup.should_include(Path("index/semantic/vectors.npy")))
        self.assertTrue(archive_backup.should_include(Path("index/semantic/chunk_ids.npy")))

    def test_stage_removal_rejects_unmanaged_paths(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            staging = root / "staging"
            staging.mkdir()
            outside = root / "raw"
            outside.mkdir()
            with patch.object(archive_backup, "STAGING_ROOT", staging):
                with self.assertRaises(archive_backup.BackupError):
                    archive_backup._safe_remove_stage(outside)
            self.assertTrue(outside.exists())

    def test_source_files_excludes_model_cache(self):
        files = archive_backup.source_files()
        self.assertNotIn(Path("index/archive.sqlite"), files)
        self.assertIn(Path("index/semantic/vectors.npy"), files)
        self.assertTrue(all("model-cache" not in path.parts for path in files))

    def test_find_manifest_requires_signed_recovery_manifest(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            unsigned = root / "manifests" / "manifest.json"
            unsigned.parent.mkdir(parents=True)
            unsigned.write_text("{}", encoding="utf-8")
            signed = root / "current" / "manifest.json"
            signed.parent.mkdir(parents=True)
            signed.write_text("{}", encoding="utf-8")
            signed.with_name("manifest.sha256").write_text("hash", encoding="utf-8")
            self.assertEqual(archive_backup._find_manifest(root), signed)


if __name__ == "__main__":
    unittest.main()
