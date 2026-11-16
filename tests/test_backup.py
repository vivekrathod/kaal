import re
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import kaal.core as kaal

STAMP = re.compile(r"^\d{8}T\d{6}Z$")


class BackupTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.saved = kaal.VAULT
        kaal.configure_vault(self.dir / "vault")
        kaal.call_captured(kaal.init_vault, SimpleNamespace())
        self.note = kaal.call_captured(kaal.add_note, SimpleNamespace(title="Private", tags="", body="SSN: 078-05-1120", body_file=None, sensitivity="sensitive"))[0]["id"]
        src = self.dir / "a.txt"
        src.write_text("attachment text")
        kaal.call_captured(kaal.attach_file, SimpleNamespace(query=self.note, file=str(src), sensitivity="auto", extract=False, ocr=False))
        (kaal.VAULT / ".library-purge").mkdir(mode=0o700)
        self.dest = self.dir / "backups"

    def tearDown(self):
        kaal.configure_vault(self.saved)
        self.tmp.cleanup()

    def backup(self, keep=14):
        result, _ = kaal.call_captured(kaal.backup_vault, SimpleNamespace(dest=str(self.dest), keep=keep))
        return result

    def vault_files(self, root):
        return sorted(str(p.relative_to(root)) for p in root.rglob("*") if p.is_file())

    def test_first_backup_copies_the_vault_as_stored(self):
        result = self.backup()
        self.assertEqual(result["status"], "ok")
        backup = Path(result["backup"])
        self.assertTrue(STAMP.match(backup.name))
        expected = [f for f in self.vault_files(kaal.VAULT) if f != kaal.VAULT_LOCK_NAME]
        self.assertEqual(self.vault_files(backup), expected)
        for rel in expected:
            self.assertEqual((backup / rel).read_bytes(), (kaal.VAULT / rel).read_bytes())
            self.assertEqual((backup / rel).stat().st_mode & 0o777, 0o600)
        self.assertFalse((backup / ".library-purge").exists())
        self.assertTrue(all(d.stat().st_mode & 0o777 == 0o700 for d in [backup, *[p for p in backup.rglob("*") if p.is_dir()]]))
        self.assertEqual(self.dest.stat().st_mode & 0o777, 0o700)
        self.assertEqual((result["files"], result["copied"], result["linked"]), (len(expected), len(expected), 0))

    def test_second_backup_links_unchanged_files_and_copies_changed_ones(self):
        first = Path(self.backup()["backup"])
        time.sleep(1.1)
        kaal.call_captured(kaal.append_note, SimpleNamespace(query=self.note, body="more"))
        second_result = self.backup()
        second = Path(second_result["backup"])
        changed = {"index.json", f"notes/{self.note}.json"}
        for rel in self.vault_files(second):
            same_inode = (second / rel).stat().st_ino == (first / rel).stat().st_ino
            self.assertEqual(same_inode, rel not in changed, rel)
        self.assertEqual(second_result["copied"], 2)
        self.assertNotEqual((second / "index.json").read_bytes(), (first / "index.json").read_bytes())

    def test_keeps_only_the_newest_and_never_touches_other_folders(self):
        self.dest.mkdir(mode=0o700)
        for stamp in ("20260101T000000Z", "20260102T000000Z", "20260103T000000Z"):
            (self.dest / stamp).mkdir()
        (self.dest / "keep-me").mkdir()
        (self.dest / ".incomplete-20260104T000000Z").mkdir()
        result = self.backup(keep=2)
        names = sorted(p.name for p in self.dest.iterdir())
        self.assertEqual(names, sorted(["20260103T000000Z", Path(result["backup"]).name, "keep-me"]))
        self.assertEqual(result["pruned"], 2)
        self.assertEqual(result["kept"], 2)

    def test_copies_while_holding_the_vault_lock(self):
        held = []
        original = kaal.shutil.copy2

        def spy(*args, **kwargs):
            held.append(kaal.vault_lock_held())
            return original(*args, **kwargs)

        with patch.object(kaal.shutil, "copy2", spy):
            self.backup()
        self.assertTrue(held and all(held))

    def test_refuses_a_destination_inside_the_vault_or_a_bad_keep(self):
        with self.assertRaises(kaal.KaalError):
            kaal.call_captured(kaal.backup_vault, SimpleNamespace(dest=str(kaal.VAULT / "backups"), keep=14))
        with self.assertRaises(kaal.KaalError):
            self.backup(keep=0)

    def test_output_names_no_titles_or_files(self):
        result = self.backup()
        self.assertNotIn("Private", str(result))
        self.assertNotIn("a.txt", str(result))


if __name__ == "__main__":
    unittest.main()
