"""The core's result/error layer that both the CLI and kaal-server use."""
import io
import json
import tempfile
import threading
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import kaal.core as kaal


class CoreServiceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.vault = Path(self.tmp.name) / "vault"
        self.saved = (kaal.VAULT, kaal.NOTES_DIR, kaal.ATTACH_DIR, kaal.INDEX_PATH)
        kaal.VAULT = self.vault
        kaal.NOTES_DIR = self.vault / "notes"
        kaal.ATTACH_DIR = self.vault / "attachments"
        kaal.INDEX_PATH = self.vault / "index.json"
        for path in (kaal.VAULT, kaal.NOTES_DIR, kaal.ATTACH_DIR):
            path.mkdir(mode=0o700, parents=True, exist_ok=True)
        kaal.save_index({"version": 1, "created": kaal.now_iso(), "notes": []})

    def tearDown(self):
        kaal.VAULT, kaal.NOTES_DIR, kaal.ATTACH_DIR, kaal.INDEX_PATH = self.saved
        self.tmp.cleanup()

    def add(self, title="Note", body="# Note\n", tags=""):
        result, _indent = kaal.call_captured(kaal.add_note, SimpleNamespace(title=title, tags=tags, body=body, body_file=None, sensitivity="auto"))
        return result

    def test_call_captured_returns_the_result_without_printing(self):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            result, options = kaal.call_captured(kaal.add_note, SimpleNamespace(title="Groceries", tags="home", body="- milk", body_file=None, sensitivity="auto"))
        self.assertEqual(result["status"], "created")
        self.assertEqual(result["title"], "Groceries")
        self.assertEqual(options, {"indent": 2, "ensure_ascii": False})
        self.assertEqual(out.getvalue(), "")
        self.assertEqual(err.getvalue(), "")

    def test_emit_prints_exactly_like_json_dumps_on_the_cli(self):
        payload = {"title": "Café", "n": 1}
        for kwargs in ({}, {"indent": 2}, {"ensure_ascii": False}, {"indent": 2, "ensure_ascii": False}):
            out = io.StringIO()
            with redirect_stdout(out):
                kaal.emit(payload, **kwargs)
            self.assertEqual(out.getvalue(), json.dumps(payload, **kwargs) + "\n")

    def test_errors_in_capture_mode_carry_a_kind_and_print_nothing(self):
        err = io.StringIO()
        with redirect_stderr(err), self.assertRaises(kaal.KaalError) as cm:
            kaal.call_captured(kaal.show_note, SimpleNamespace(query="missing", full=False, json=True))
        self.assertEqual(cm.exception.kind, "not_found")
        self.assertIn("No note found", cm.exception.message)
        self.assertEqual(err.getvalue(), "")

    def test_cli_errors_still_print_and_exit_1(self):
        err = io.StringIO()
        with redirect_stderr(err), self.assertRaises(SystemExit) as cm:
            kaal.show_note(SimpleNamespace(query="missing", full=False, json=True))
        self.assertEqual(cm.exception.code, 1)
        self.assertIn("ERROR: No note found matching: missing", err.getvalue())

    def test_ambiguous_query_reports_matches_as_a_conflict(self):
        self.add(title="Tax 2024")
        self.add(title="Tax 2025")
        with self.assertRaises(kaal.KaalError) as cm:
            kaal.call_captured(kaal.show_note, SimpleNamespace(query="Tax", full=False, json=True))
        self.assertEqual(cm.exception.kind, "conflict")
        self.assertEqual(sorted(m["title"] for m in cm.exception.details["matches"]), ["Tax 2024", "Tax 2025"])

    def test_exact_id_wins_over_a_title_that_contains_it(self):
        first = self.add(title="First")
        self.add(title=f"Mentions {first['id']}")
        self.assertEqual([n["id"] for n in kaal.find_notes(first["id"])], [first["id"]])

    def test_capture_mode_never_reads_stdin_for_a_missing_body(self):
        with patch.object(kaal.sys, "stdin") as stdin, self.assertRaises(kaal.KaalError) as cm:
            kaal.call_captured(kaal.add_note, SimpleNamespace(title="No body", tags="", body=None, body_file=None, sensitivity="auto"))
        stdin.read.assert_not_called()
        self.assertEqual(cm.exception.kind, "invalid")

    def test_capture_text_accepts_text_without_stdin(self):
        result, _ = kaal.call_captured(kaal.browser_capture_text, SimpleNamespace(text="Selected words", title="", source_url="https://example.com/a?b=c", source_title="Page", captured_at=""))
        self.assertEqual(result["status"], "captured")
        note = kaal.load_note(kaal.find_notes(result["id"])[0])
        self.assertIn("Selected words", note["body"])
        self.assertNotIn("b=c", note["body"])

    def test_note_field_value_and_copy_output(self):
        meta = self.add(title="IDs", body="Passport: X1234567\n")
        self.assertEqual(kaal.note_field_value(meta["id"], "passport"), (meta["id"], "X1234567"))
        with patch.object(kaal.shutil, "which", return_value="/usr/bin/pbcopy"), patch.object(kaal.subprocess, "run") as run:
            result, _ = kaal.call_captured(kaal.copy_field, SimpleNamespace(query=meta["id"], field="passport"))
        self.assertEqual(result, {"status": "copied", "note": meta["id"], "field": "passport", "characters": 8})
        self.assertEqual(run.call_args.kwargs["input"], "X1234567")

    def test_match_attachment_by_id_or_unique_name(self):
        meta = {"id": "n1", "attachments": [{"id": "a1", "name": "Return 2024.pdf"}, {"id": "a2", "name": "Return 2025.pdf"}]}
        self.assertEqual(kaal.match_attachment(meta, "a2")["id"], "a2")
        self.assertEqual(kaal.match_attachment(meta, "2024")["id"], "a1")
        with self.assertRaises(kaal.KaalError) as cm:
            kaal.call_captured(lambda _args: kaal.match_attachment(meta, "Return"), SimpleNamespace())
        self.assertEqual(cm.exception.kind, "conflict")
        with self.assertRaises(kaal.KaalError) as cm:
            kaal.call_captured(lambda _args: kaal.match_attachment(meta, "nope"), SimpleNamespace())
        self.assertEqual(cm.exception.kind, "not_found")

    def test_library_extract_runs(self):
        created, _ = kaal.call_captured(kaal.library_create, SimpleNamespace(title="Doc", tags="", body="Body", source_url="", source_title=""))
        note_id = created["note"]["id"]
        src = Path(self.tmp.name) / "page.txt"
        src.write_text("hello world", encoding="utf-8")
        kaal.call_captured(kaal.library_attach, SimpleNamespace(note_id=note_id, file=str(src)))
        result, _ = kaal.call_captured(kaal.library_extract, SimpleNamespace(note_id=note_id))
        self.assertEqual(result["status"], "updated")
        self.assertEqual(result["scanned_attachments"], 1)

    def test_vault_lock_is_reentrant(self):
        done = []
        with kaal.vault_capture_lock():
            with kaal.vault_capture_lock():
                done.append(True)
        self.assertEqual(done, [True])

    def test_vault_lock_serialises_threads(self):
        order = []
        entered = threading.Event()

        def other():
            entered.set()
            with kaal.vault_capture_lock():
                order.append("other")

        with kaal.vault_capture_lock():
            thread = threading.Thread(target=other)
            thread.start()
            entered.wait(1)
            thread.join(0.2)
            order.append("main")
        thread.join(2)
        self.assertEqual(order, ["main", "other"])

    def test_writes_that_used_to_skip_the_lock_now_hold_it(self):
        held = []
        original = kaal.create_note

        def spy(*args, **kwargs):
            held.append(kaal.vault_lock_held())
            return original(*args, **kwargs)

        with patch.object(kaal, "create_note", spy):
            self.add(title="Locked")
        self.assertEqual(held, [True])
        meta = kaal.find_notes("Locked")[0]
        saves = []
        original_save = kaal.save_index

        def save_spy(index):
            saves.append(kaal.vault_lock_held())
            return original_save(index)

        src = Path(self.tmp.name) / "a.txt"
        src.write_text("attachment", encoding="utf-8")
        with patch.object(kaal, "save_index", save_spy):
            kaal.call_captured(kaal.append_note, SimpleNamespace(query=meta["id"], body="more"))
            kaal.call_captured(kaal.attach_file, SimpleNamespace(query=meta["id"], file=str(src), sensitivity="auto", extract=False, ocr=False))
            kaal.call_captured(kaal.delete_note, SimpleNamespace(query=meta["id"], yes=True))
        self.assertEqual(len(saves), 3)
        self.assertTrue(all(saves))


if __name__ == "__main__":
    unittest.main()


class VaultLocationTests(unittest.TestCase):
    def run_status(self, **env_vars):
        import os
        import subprocess
        import sys
        env = {k: v for k, v in os.environ.items() if k not in {"KAAL_VAULT", "PYTHONPATH"}}
        env.update(env_vars, KAAL_LOCAL="1")
        launcher = Path(__file__).resolve().parents[1] / "bin" / "kaal.py"
        proc = subprocess.run([sys.executable, str(launcher), "status"], env=env, capture_output=True, text=True, check=True)
        return json.loads(proc.stdout)["vault"]

    def test_kaal_vault_names_the_vault_and_the_old_name_is_ignored(self):
        with tempfile.TemporaryDirectory() as td:
            self.assertEqual(self.run_status(KAAL_VAULT=f"{td}/a"), f"{td}/a")
            self.assertEqual(self.run_status(KAAL_VAULT=f"{td}/a", SECURE_NOTES_VAULT=f"{td}/b"), f"{td}/a")
            self.assertEqual(self.run_status(SECURE_NOTES_VAULT=f"{td}/b", HOME=td), f"{td}/.kaal")


class BrokenPipeTests(unittest.TestCase):
    def test_closing_the_pipe_early_exits_quietly(self):
        import os
        import subprocess
        import sys
        with tempfile.TemporaryDirectory() as td:
            vault = Path(td) / "vault"
            (vault / "notes").mkdir(parents=True)
            (vault / "attachments").mkdir()
            notes = [{"id": f"{i:012x}", "title": f"Note {i}", "tags": [], "updated": "", "attachments": []} for i in range(20000)]
            (vault / "index.json").write_text(json.dumps({"version": 1, "notes": notes}))
            env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
            env.update(KAAL_LOCAL="1", KAAL_VAULT=str(vault))
            launcher = Path(__file__).resolve().parents[1] / "bin" / "kaal.py"
            proc = subprocess.Popen([sys.executable, str(launcher), "list"], env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            proc.stdout.readline()
            proc.stdout.close()
            stderr = proc.stderr.read().decode()
            proc.wait(timeout=60)
            proc.stderr.close()
        self.assertNotIn("Traceback", stderr)
        self.assertNotIn("BrokenPipeError", stderr)
        self.assertEqual(proc.returncode, 141)
