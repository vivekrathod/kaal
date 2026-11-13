import io
import json
import tarfile
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from fastapi.testclient import TestClient

import kaal.core as kaal
from kaal import server, tokens


class ExportImportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.saved = kaal.VAULT
        kaal.configure_vault(self.dir / "vault")
        kaal.call_captured(kaal.init_vault, SimpleNamespace())
        self.private = kaal.call_captured(kaal.add_note, SimpleNamespace(title="Private", tags="", body="SSN: 078-05-1120", body_file=None, sensitivity="sensitive"))[0]["id"]
        self.public = kaal.call_captured(kaal.add_note, SimpleNamespace(title="Public", tags="", body="hello", body_file=None, sensitivity="public"))[0]["id"]
        src = self.dir / "a.txt"
        src.write_text("attachment text")
        kaal.call_captured(kaal.attach_file, SimpleNamespace(query=self.public, file=str(src), sensitivity="public", extract=False, ocr=False))

    def tearDown(self):
        kaal.configure_vault(self.saved)
        self.tmp.cleanup()

    def export(self, out):
        return kaal.call_captured(kaal.export_vault, SimpleNamespace(output=str(out)))[0]

    def test_round_trip_into_an_empty_vault(self):
        archive = self.dir / "kaal.tar"
        exported = self.export(archive)
        self.assertEqual(exported["status"], "exported")
        self.assertEqual(exported["notes"], 2)
        self.assertEqual(archive.stat().st_mode & 0o777, 0o600)
        with tarfile.open(archive) as tar:
            names = tar.getnames()
        self.assertIn("index.json", names)
        self.assertFalse(any(name.endswith(".lock") for name in names))
        kaal.configure_vault(self.dir / "restored")
        imported = kaal.call_captured(kaal.import_vault, SimpleNamespace(archive=str(archive)))[0]
        self.assertEqual(imported["status"], "imported")
        self.assertEqual(imported["verify"]["status"], "ok")
        self.assertEqual(imported["verify"]["warnings"]["loose_permissions"], {"dirs": 0, "files": 0})
        self.assertEqual(kaal.load_note(kaal.note_by_id(self.private))["body"], "SSN: 078-05-1120")

    def test_export_never_overwrites(self):
        archive = self.dir / "kaal.tar"
        archive.write_text("existing")
        with self.assertRaises(kaal.KaalError):
            self.export(archive)
        self.assertEqual(archive.read_text(), "existing")

    def test_import_refuses_a_vault_that_has_data(self):
        archive = self.dir / "kaal.tar"
        self.export(archive)
        with self.assertRaises(kaal.KaalError) as cm:
            kaal.call_captured(kaal.import_vault, SimpleNamespace(archive=str(archive)))
        self.assertIn("not empty", cm.exception.message)

    def make_archive(self, members):
        archive = self.dir / "bad.tar"
        with tarfile.open(archive, "w") as tar:
            index = json.dumps({"version": 1, "notes": []}).encode()
            info = tarfile.TarInfo("index.json")
            info.size = len(index)
            tar.addfile(info, io.BytesIO(index))
            for info in members:
                tar.addfile(info, io.BytesIO(b"x" * info.size) if info.isfile() else None)
        return archive

    def test_import_rejects_unsafe_members(self):
        kaal.configure_vault(self.dir / "target")
        cases = []
        evil = tarfile.TarInfo("../escape.txt")
        evil.size = 1
        cases.append(evil)
        absolute = tarfile.TarInfo("/tmp/kaal-absolute.txt")
        absolute.size = 1
        cases.append(absolute)
        link = tarfile.TarInfo("notes/link.md")
        link.type = tarfile.SYMTYPE
        link.linkname = "/etc/passwd"
        cases.append(link)
        for member in cases:
            archive = self.make_archive([member])
            with self.assertRaises(kaal.KaalError, msg=member.name) as cm:
                kaal.call_captured(kaal.import_vault, SimpleNamespace(archive=str(archive)))
            self.assertIn("unsafe", cm.exception.message)
            self.assertFalse((self.dir / "escape.txt").exists())
            archive.unlink()

    def test_server_export_needs_admin(self):
        settings = server.Settings(home=self.dir, vault=kaal.VAULT, tokens_file=self.dir / "tokens.json", tmp_dir=self.dir / "tmp")
        store = tokens.TokenStore(settings.tokens_file)
        _r, admin = store.create("admin", ["read", "reveal", "admin"])
        _r, reader = store.create("reader", ["read", "reveal", "write", "delete"])
        client = TestClient(server.create_app(settings))
        self.assertEqual(client.get("/v1/export", headers={"Authorization": f"Bearer {reader}"}).status_code, 403)
        response = client.get("/v1/export", headers={"Authorization": f"Bearer {admin}"})
        self.assertEqual(response.status_code, 200)
        with tarfile.open(fileobj=io.BytesIO(response.content)) as tar:
            self.assertIn("index.json", tar.getnames())
        self.assertEqual(list((self.dir / "tmp").iterdir()), [])


if __name__ == "__main__":
    unittest.main()
