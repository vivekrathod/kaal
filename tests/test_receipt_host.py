import importlib.util
import base64
import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("receipt_host", ROOT / "bin" / "kaal-receipt-host.py")
host = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(host)
INSTALL_SPEC = importlib.util.spec_from_file_location("receipt_host_installer", ROOT / "bin" / "install-chrome-receipt-capture.py")
installer = importlib.util.module_from_spec(INSTALL_SPEC)
assert INSTALL_SPEC.loader is not None
INSTALL_SPEC.loader.exec_module(installer)


class ReceiptHostTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.original_run = host.subprocess.run
        self.original_downloads_dir = host.DOWNLOADS_DIR
        self.original_staging_dir = host.STAGING_DIR

    def tearDown(self):
        host.subprocess.run = self.original_run
        host.DOWNLOADS_DIR = self.original_downloads_dir
        host.STAGING_DIR = self.original_staging_dir
        self.tmp.cleanup()

    def fake_success(self, *args, **kwargs):
        return SimpleNamespace(
            returncode=0,
            stdout=json.dumps({"status": "captured", "id": "receipt-id", "attachment_id": "attachment-id", "storage": "plaintext"}),
            stderr="",
        )

    def test_capture_rejects_a_file_outside_capture_staging_directory(self):
        receipt = self.root / "receipt.pdf"
        receipt.write_bytes(b"receipt")
        with self.assertRaisesRegex(ValueError, "staging"):
            host.capture({"action": "capture", "path": str(receipt)})

    def test_capture_rejects_a_symlink_even_if_its_path_looks_like_staging(self):
        source = self.root / "real-receipt.pdf"
        source.write_bytes(b"receipt")
        staged = self.root / "Kaal Capture"
        staged.mkdir()
        link = staged / "receipt-link.pdf"
        os.symlink(source, link)
        with self.assertRaisesRegex(ValueError, "symbolic"):
            host.capture({"action": "capture", "path": str(link)})

    def test_capture_accepts_chromes_configured_download_root_and_cleans_only_staging_file(self):
        staged = self.root / "custom-chrome-downloads" / "Kaal Capture"
        staged.mkdir(parents=True)
        host.STAGING_DIR = staged
        receipt = staged / "receipt-2026-07-30T11-40-19-123Z-demo.pdf"
        receipt.write_bytes(b"receipt")
        host.subprocess.run = self.fake_success

        result = host.capture({"action": "capture", "path": str(receipt), "cleanupStaging": True})

        self.assertTrue(result["ok"])
        self.assertTrue(result["stagingFileCleaned"])
        self.assertFalse(receipt.exists())

    def test_capture_rejects_matching_filename_outside_the_installed_staging_directory(self):
        host.STAGING_DIR = self.root / "trusted-downloads" / "Kaal Capture"
        untrusted = self.root / "other-downloads" / "Kaal Capture"
        untrusted.mkdir(parents=True)
        receipt = untrusted / "receipt-2026-07-30T11-40-19-123Z-demo.pdf"
        receipt.write_bytes(b"receipt")

        with self.assertRaisesRegex(ValueError, "staging"):
            host.capture({"action": "capture", "path": str(receipt), "cleanupStaging": True})
        self.assertTrue(receipt.exists())

    def test_capture_does_not_clean_staging_file_without_capture_success_contract(self):
        staged = self.root / "trusted-downloads" / "Kaal Capture"
        staged.mkdir(parents=True)
        host.STAGING_DIR = staged
        receipt = staged / "receipt-2026-07-30T11-40-19-123Z-demo.pdf"
        receipt.write_bytes(b"receipt")
        host.subprocess.run = lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout="{}", stderr="")

        result = host.capture({"action": "capture", "path": str(receipt), "cleanupStaging": True})

        self.assertFalse(result["ok"])
        self.assertTrue(receipt.exists())

    def test_capture_local_pdf_from_downloads_preserves_the_original_download(self):
        downloads = self.root / "Downloads"
        downloads.mkdir()
        host.DOWNLOADS_DIR = downloads
        receipt = downloads / "Payment receipt.pdf"
        receipt.write_bytes(b"%PDF receipt")
        host.subprocess.run = self.fake_success

        result = host.capture({"action": "capture-local-file", "path": str(receipt)})

        self.assertTrue(result["ok"])
        self.assertFalse(result["stagingFileCleaned"])
        self.assertTrue(receipt.exists())

    def test_capture_local_file_rejects_a_path_outside_downloads(self):
        host.DOWNLOADS_DIR = self.root / "Downloads"
        receipt = self.root / "Payment receipt.pdf"
        receipt.write_bytes(b"%PDF receipt")
        with self.assertRaisesRegex(ValueError, "Downloads"):
            host.capture({"action": "capture-local-file", "path": str(receipt)})

    def test_list_returns_bounded_receipt_metadata_from_kaal(self):
        calls = []
        def fake_list(command, **kwargs):
            calls.append(command)
            return SimpleNamespace(returncode=0, stdout=json.dumps([{"id": "r1", "title": "Receipt", "attachments": []}]), stderr="")
        host.subprocess.run = fake_list

        result = host.handle_request({"action": "list"})

        self.assertTrue(result["ok"])
        self.assertEqual(result["receipts"][0]["id"], "r1")
        self.assertEqual(calls[0][-4:], ["list", "--json", "--limit", str(host.RECEIPT_LIST_LIMIT)])

    def test_list_can_request_inbox_or_trashed_receipts_and_management_actions_require_an_id(self):
        calls = []
        def fake_command(command, **kwargs):
            calls.append(command)
            status = {"trash": "trashed", "review": "reviewed"}.get(command[-2], command[-2])
            payload = [] if "list" in command else {"status": status, "id": "r1"}
            return SimpleNamespace(returncode=0, stdout=json.dumps(payload), stderr="")
        host.subprocess.run = fake_command

        inbox = host.handle_request({"action": "list", "inbox": True})
        listed = host.handle_request({"action": "list", "trash": True})
        trashed = host.handle_request({"action": "trash", "id": "r1"})
        reviewed = host.handle_request({"action": "review", "id": "r1"})

        self.assertTrue(inbox["ok"])
        self.assertTrue(listed["ok"])
        self.assertEqual(trashed["result"]["status"], "trashed")
        self.assertEqual(reviewed["result"]["status"], "reviewed")
        self.assertIn("--inbox", calls[0])
        self.assertIn("--trash", calls[1])
        self.assertEqual(calls[2][-3:], ["medical", "trash", "r1"])
        self.assertEqual(calls[3][-3:], ["medical", "review", "r1"])
        with self.assertRaisesRegex(ValueError, "id is required"):
            host.handle_request({"action": "restore"})

    def test_bulk_purge_requires_typed_ids_and_forwards_one_confirmed_command(self):
        calls = []

        def fake_command(command, **kwargs):
            calls.append(command)
            return SimpleNamespace(returncode=0, stdout=json.dumps({"status": "purged", "count": 2, "ids": ["r1", "r2"]}), stderr="")

        host.subprocess.run = fake_command
        with self.assertRaisesRegex(ValueError, "confirmation"):
            host.handle_request({"action": "purge-bulk", "ids": ["r1", "r2"], "confirmPermanent": True, "confirmationText": "r1"})

        result = host.handle_request({
            "action": "purge-bulk",
            "ids": ["r1", "r2"],
            "confirmPermanent": True,
            "confirmationText": "r1,r2",
        })

        self.assertTrue(result["ok"])
        self.assertEqual(result["result"]["count"], 2)
        self.assertEqual(calls[0][-5:], ["medical", "purge-bulk", "r1", "r2", "--yes"])

    def test_capture_rejects_an_oversized_staging_file_before_subprocess(self):
        staged = self.root / "Kaal Capture"
        staged.mkdir()
        host.STAGING_DIR = staged
        receipt = staged / "receipt-2026-07-30T11-40-19-123Z-large.pdf"
        with receipt.open("wb") as out:
            out.truncate(host.MAX_CAPTURE_BYTES + 1)
        with self.assertRaisesRegex(ValueError, "exceeds"):
            host.capture({"action": "capture", "path": str(receipt)})

    def test_extension_manifest_and_installer_agree_on_the_native_host_origin(self):
        manifest = json.loads((ROOT / "chrome-extension" / "manifest.json").read_text())
        raw_key = base64.b64decode(manifest["key"])
        extension_id = "".join(
            chr(97 + (byte >> 4)) + chr(97 + (byte & 15))
            for byte in hashlib.sha256(raw_key).digest()[:16]
        )
        self.assertEqual(extension_id, installer.EXTENSION_ID)
        self.assertTrue({"downloads", "debugger", "nativeMessaging"}.issubset(set(manifest["permissions"])))
        self.assertEqual(installer.HOST_NAME, "com.kaal.receipt_capture")


if __name__ == "__main__":
    unittest.main()
