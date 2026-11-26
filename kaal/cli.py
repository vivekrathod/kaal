"""Command-line entry point for Kaal.

With no server configured, commands work directly on the local vault. With a
server URL configured (see kaal.client), the same commands go to kaal-server.
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from kaal import client, core


def build_parser() -> argparse.ArgumentParser:
    prog = os.environ.get("KAAL_CLI_NAME", Path(sys.argv[0]).name)
    description = "Kaal: tiny local encrypted notes vault for sensitive data" if prog == "kaal" else "Tiny local encrypted notes vault for sensitive data"
    p = argparse.ArgumentParser(prog=prog, description=description)
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("init", help="Create the vault and its key file")
    sp.set_defaults(func=core.init_vault)

    sp = sub.add_parser("status", help="Show vault status")
    sp.set_defaults(func=core.status)

    sp = sub.add_parser("add", help="Add a Markdown note")
    sp.add_argument("--title", required=True)
    sp.add_argument("--tags", default="", help="Comma-separated tags")
    sp.add_argument("--body", help="Note body; omit to read stdin")
    sp.add_argument("--body-file", help="Markdown/text file to use as the note body")
    sp.add_argument("--sensitivity", default="auto", choices=["auto", "sensitive", "public", "encrypted", "plaintext", "encrypt", "plain"], help="A label only: everything is stored encrypted. Default: sensitive")
    sp.set_defaults(func=core.add_note)

    sp = sub.add_parser("list", help="List note metadata only")
    sp.add_argument("--tag", default="")
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(func=core.list_notes)

    sp = sub.add_parser("search", help="Search titles/tags only")
    sp.add_argument("query")
    sp.set_defaults(func=core.search_notes)

    sp = sub.add_parser("show", help="Show note; redacted by default")
    sp.add_argument("query", help="Note id or title substring")
    sp.add_argument("--full", action="store_true", help="Print full plaintext to terminal/chat")
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(func=core.show_note)

    sp = sub.add_parser("append", help="Append encrypted note content")
    sp.add_argument("query")
    sp.add_argument("--body", help="Text to append; omit to read stdin")
    sp.set_defaults(func=core.append_note)

    sp = sub.add_parser("copy", help="Copy a field value from a note to clipboard without printing it")
    sp.add_argument("query")
    sp.add_argument("--field", required=True, help="Field name, e.g. ssn, passport, dob")
    sp.set_defaults(func=core.copy_field)

    sp = sub.add_parser("attach", help="Attach a local file; preserves original and can extract/OCR Markdown")
    sp.add_argument("query")
    sp.add_argument("file")
    sp.add_argument("--sensitivity", default="auto", choices=["auto", "sensitive", "public", "encrypted", "plaintext", "encrypt", "plain"], help="A label only: everything is stored encrypted. Default: sensitive")
    sp.add_argument("--extract", action="store_true", help="Keep the file's text beside it: a text file as it is, a PDF's own text")
    sp.add_argument("--ocr", action="store_true", help="OCR image attachments with Tesseract when available")
    sp.set_defaults(func=core.attach_file)

    sp = sub.add_parser("export-attachment", help="Decrypt attachment to an output path")
    sp.add_argument("query")
    sp.add_argument("attachment")
    sp.add_argument("--output", default="")
    sp.add_argument("--force", action="store_true")
    sp.set_defaults(func=core.export_attachment)

    sp = sub.add_parser("ocr-attachments", help="Extract/OCR text from existing attachments into separate sidecars")
    sp.add_argument("--query", default="", help="Optional note id or title substring to limit processing")
    sp.add_argument("--dry-run", action="store_true", help="Report eligible attachments without writing OCR sidecars")
    sp.add_argument("--force", action="store_true", help="Reprocess attachments that already have extracted Markdown")
    sp.add_argument("--limit", type=int, default=0, help="Maximum eligible attachments to process")
    sp.add_argument("--no-extract", action="store_true", help="Do not extract text/PDF attachments")
    sp.add_argument("--no-ocr", action="store_true", help="Do not OCR image attachments")
    sp.set_defaults(func=core.ocr_attachments)

    sp = sub.add_parser("import-joplin-raw", help="Import notes and referenced resources from a Joplin export: the .jex file, or a RAW export directory")
    sp.add_argument("path", help="Joplin .jex file or RAW export directory")
    sp.add_argument("--dry-run", action="store_true", help="Scan and report counts without writing to the Kaal vault")
    sp.add_argument("--incremental", "--repair", action="store_true", help="Repair/import only missing Joplin notes and attachments; skip existing imported notes")
    sp.add_argument("--tags", default="", help="Comma-separated extra tags for imported notes")
    sp.add_argument("--sensitivity", default="auto", choices=["auto", "sensitive", "public", "encrypted", "plaintext", "encrypt", "plain"], help="A label for the imported notes and files; everything is stored encrypted")
    sp.add_argument("--no-extract", action="store_true", help="Do not extract text/Markdown from text/PDF resources; extraction is on by default")
    sp.add_argument("--no-ocr", action="store_true", help="Do not OCR image resources; OCR is on by default")
    sp.set_defaults(func=core.import_joplin_raw)

    sp = sub.add_parser("import-keep", help="Import notes, checklists and pictures from a Google Keep export (the Google Takeout .zip, or its unpacked folder)")
    sp.add_argument("path", help="The Takeout .zip, or the folder it unpacks to")
    sp.add_argument("--dry-run", action="store_true", help="Count what would be imported without writing anything")
    sp.add_argument("--tags", default="", help="Comma-separated extra tags for the imported notes")
    sp.set_defaults(func=core.import_keep)

    sp = sub.add_parser("capture", help="Capture a browser artifact as a normal Kaal note and attachment")
    sp.add_argument("file", help="Downloaded browser file or generated page PDF")
    sp.add_argument("--title", default="", help="Capture title; defaults to browser title or filename")
    sp.add_argument("--source-url", default="", help="Original browser URL; query strings and fragments are discarded")
    sp.add_argument("--source-title", default="", help="Original browser tab title")
    sp.add_argument("--captured-at", default="", help="ISO-8601 capture time; defaults to now")
    sp.set_defaults(func=core.browser_capture)

    sp = sub.add_parser("capture-text", help="Capture selected browser text from stdin as a normal Kaal note")
    sp.add_argument("--title", default="", help="Capture title; defaults to browser title")
    sp.add_argument("--source-url", default="", help="Original browser URL; query strings and fragments are discarded")
    sp.add_argument("--source-title", default="", help="Original browser tab title")
    sp.add_argument("--captured-at", default="", help="ISO-8601 capture time; defaults to now")
    sp.set_defaults(func=core.browser_capture_text)

    sp = sub.add_parser("library", help="Manage non-medical Kaal notes for the local dashboard")
    library_sub = sp.add_subparsers(dest="library_cmd", required=True)
    library_list_parser = library_sub.add_parser("list", help="List bounded non-medical note metadata")
    library_list_parser.add_argument("--trash", action="store_true")
    library_list_parser.add_argument("--limit", type=int, default=100)
    library_list_parser.set_defaults(func=core.library_list)
    library_search_parser = library_sub.add_parser("search", help="Find notes by title, tags, text, or the text read from their files")
    library_search_parser.add_argument("query")
    library_search_parser.add_argument("--trash", action="store_true")
    library_search_parser.add_argument("--limit", type=int, default=50)
    library_search_parser.add_argument("--offset", type=int, default=0)
    library_search_parser.set_defaults(func=core.library_search)
    library_show_parser = library_sub.add_parser("show", help="Reveal one non-medical note")
    library_show_parser.add_argument("note_id")
    library_show_parser.set_defaults(func=core.library_show)
    library_create_parser = library_sub.add_parser("create", help="Create a non-medical note from stdin")
    library_create_parser.add_argument("--title", required=True)
    library_create_parser.add_argument("--tags", default="")
    library_create_parser.add_argument("--source-url", default="")
    library_create_parser.add_argument("--source-title", default="")
    library_create_parser.set_defaults(func=core.library_create)
    library_update_parser = library_sub.add_parser("update", help="Replace a non-medical note body from stdin")
    library_update_parser.add_argument("note_id")
    library_update_parser.add_argument("--title", required=True)
    library_update_parser.add_argument("--tags", default="")
    library_update_parser.set_defaults(func=core.library_update)
    for action, handler, help_text in (("trash", core.library_trash, "Move a non-medical note to Trash"), ("restore", core.library_restore, "Restore a non-medical note")):
        action_parser = library_sub.add_parser(action, help=help_text)
        action_parser.add_argument("note_id")
        action_parser.set_defaults(func=handler)
    library_purge_parser = library_sub.add_parser("purge", help="Permanently delete a trashed non-medical note")
    library_purge_parser.add_argument("note_id")
    library_purge_parser.add_argument("--yes", action="store_true")
    library_purge_parser.set_defaults(func=core.library_purge)
    library_attach_parser = library_sub.add_parser("attach", help="Attach a file to an active non-medical note")
    library_attach_parser.add_argument("note_id")
    library_attach_parser.add_argument("file")
    library_attach_parser.set_defaults(func=core.library_attach)
    library_extract_parser = library_sub.add_parser("extract", help="Run generic extraction/OCR for one Library note")
    library_extract_parser.add_argument("note_id")
    library_extract_parser.set_defaults(func=core.library_extract)
    library_text_parser = library_sub.add_parser("extracted-text", help="Reveal one Library attachment's extracted text")
    library_text_parser.add_argument("note_id")
    library_text_parser.add_argument("--attachment-id", required=True)
    library_text_parser.set_defaults(func=core.library_extracted_text)

    sp = sub.add_parser("medical", help="Capture and manage medical expense receipts")
    medical_sub = sp.add_subparsers(dest="medical_cmd", required=True)
    capture = medical_sub.add_parser("capture", help="Capture a browser-downloaded receipt into the medical inbox")
    capture.add_argument("file", help="Downloaded PDF, image, or receipt file")
    capture.add_argument("--title", default="", help="Receipt title; defaults to browser title or filename")
    capture.add_argument("--source-url", default="", help="Original browser page or PDF URL")
    capture.add_argument("--source-title", default="", help="Original browser tab title")
    capture.add_argument("--captured-at", default="", help="ISO-8601 capture time; defaults to now")
    capture.set_defaults(func=core.medical_capture)
    medical_list_parser = medical_sub.add_parser("list", help="List medical receipt records")
    medical_list_filter = medical_list_parser.add_mutually_exclusive_group()
    medical_list_filter.add_argument("--inbox", action="store_true", help="Show only receipts awaiting review")
    medical_list_filter.add_argument("--trash", action="store_true", help="Show only receipts in the recoverable trash")
    medical_list_parser.add_argument("--json", action="store_true")
    medical_list_parser.add_argument("--limit", type=int, default=0, help="Maximum newest records to return; default all")
    medical_list_parser.set_defaults(func=core.medical_list)
    medical_show_parser = medical_sub.add_parser("show", help="Show one receipt with the line of text each value came from")
    medical_show_parser.add_argument("note_id")
    medical_show_parser.set_defaults(func=core.medical_show)
    medical_review_parser = medical_sub.add_parser("review", help="Mark a receipt as reviewed")
    medical_review_parser.add_argument("note_id")
    medical_review_parser.set_defaults(func=core.medical_review)
    medical_reopen_parser = medical_sub.add_parser("reopen", help="Return a reviewed receipt to the inbox")
    medical_reopen_parser.add_argument("note_id")
    medical_reopen_parser.set_defaults(func=core.medical_reopen)
    medical_text_parser = medical_sub.add_parser("extracted-text", help="Print extracted text for one receipt attachment")
    medical_text_parser.add_argument("note_id")
    medical_text_parser.add_argument("--attachment-id", default="")
    medical_text_parser.set_defaults(func=core.medical_extracted_text)
    medical_export_parser = medical_sub.add_parser("export-original", help="Export one exact managed receipt attachment")
    medical_export_parser.add_argument("note_id")
    medical_export_parser.add_argument("--attachment-id", required=True)
    medical_export_parser.add_argument("--output", required=True)
    medical_export_parser.set_defaults(func=core.medical_export_original)
    medical_update_parser = medical_sub.add_parser("update", help="Manually correct extracted receipt fields")
    medical_update_parser.add_argument("note_id")
    medical_update_parser.add_argument("--fields-json", required=True)
    medical_update_parser.set_defaults(func=core.medical_update)
    medical_report_parser = medical_sub.add_parser("report", help="Summarize active receipts by field filters")
    medical_report_parser.add_argument("--year", default="")
    medical_report_parser.add_argument("--patient-name", default="")
    medical_report_parser.add_argument("--from-date", default="")
    medical_report_parser.add_argument("--to-date", default="")
    medical_report_parser.add_argument("--reimbursed", default="all", choices=["all", "yes", "no"], help="Only receipts that are (yes) or are not (no) reimbursed; default all")
    medical_report_parser.set_defaults(func=core.medical_report)
    medical_reimburse_parser = medical_sub.add_parser("reimburse", help="Record that reviewed receipts were reimbursed, or undo that")
    medical_reimburse_parser.add_argument("note_ids", nargs="+")
    medical_reimburse_parser.add_argument("--date", default="", help="Reimbursement date, YYYY-MM-DD; defaults to today")
    medical_reimburse_parser.add_argument("--note", default="", help="Optional reference, for example a withdrawal id")
    medical_reimburse_parser.add_argument("--undo", action="store_true", help="Mark the receipts as not reimbursed")
    medical_reimburse_parser.set_defaults(func=core.medical_reimburse)
    medical_reextract_parser = medical_sub.add_parser("reextract", help="Read stored receipts again; values you typed or confirmed are kept")
    medical_reextract_parser.add_argument("note_ids", nargs="*")
    medical_reextract_parser.add_argument("--unreviewed", action="store_true", help="Every receipt still awaiting review")
    medical_reextract_parser.set_defaults(func=core.medical_reextract)
    medical_providers_parser = medical_sub.add_parser("providers", help="List the provider names Kaal shows your way, or forget some")
    medical_providers_parser.add_argument("--forget", action="append", default=[], metavar="PRINTED_NAME", help="Stop replacing this printed name; repeat for more")
    medical_providers_parser.set_defaults(func=core.medical_providers)
    medical_evaluate_parser = medical_sub.add_parser("evaluate", help="Measure extraction accuracy against confirmed receipt values; prints ids and outcomes only")
    medical_evaluate_parser.add_argument("--id", dest="ids", action="append", default=[], metavar="NOTE_ID", help="Evaluate only this receipt; repeat for more")
    medical_evaluate_parser.add_argument("--limit", type=int, default=0, help="Stop after this many receipts; default all")
    medical_evaluate_parser.set_defaults(func=core.medical_evaluate)
    medical_people_parser = medical_sub.add_parser("people", help="List, add or remove the family members receipts belong to")
    medical_people_parser.add_argument("--add", action="append", default=[], metavar="NAME", help="Add a family member; repeat for more")
    medical_people_parser.add_argument("--remove", action="append", default=[], metavar="NAME", help="Remove a family member; repeat for more")
    medical_people_parser.set_defaults(func=core.medical_people)
    medical_sub.add_parser("sources", help="List the sites receipts were saved from (host names only)").set_defaults(func=core.medical_sources)
    medical_trash_parser = medical_sub.add_parser("trash", help="Move a receipt to recoverable trash")
    medical_trash_parser.add_argument("note_id")
    medical_trash_parser.set_defaults(func=core.medical_trash)
    medical_restore_parser = medical_sub.add_parser("restore", help="Restore a receipt from recoverable trash")
    medical_restore_parser.add_argument("note_id")
    medical_restore_parser.set_defaults(func=core.medical_restore)
    medical_purge_parser = medical_sub.add_parser("purge", help="Permanently delete a receipt already in trash")
    medical_purge_parser.add_argument("note_id")
    medical_purge_parser.add_argument("--yes", action="store_true", help="Confirm permanent deletion")
    medical_purge_parser.set_defaults(func=core.medical_purge)
    medical_purge_bulk_parser = medical_sub.add_parser("purge-bulk", help="Permanently delete multiple receipts already in trash")
    medical_purge_bulk_parser.add_argument("note_ids", nargs="+")
    medical_purge_bulk_parser.add_argument("--yes", action="store_true", help="Confirm permanent deletion")
    medical_purge_bulk_parser.set_defaults(func=core.medical_purge_bulk)

    sp = sub.add_parser("verify", help="Check the index against the files on disk (ids only)")
    sp.add_argument("--deep", action="store_true", help="Also decrypt every encrypted note and attachment and check attachment hashes")
    sp.set_defaults(func=core.verify_vault)

    sp = sub.add_parser("encrypt-all", help="Encrypt every note, attachment and extracted text an older version stored readable")
    sp.add_argument("--dry-run", action="store_true", help="Count what would be encrypted; change nothing")
    sp.set_defaults(func=core.encrypt_vault)

    sp = sub.add_parser("backup", help="Copy the vault into a new dated folder, hard-linking unchanged files, and prune old copies")
    sp.add_argument("--dest", default="", help="Backup folder (default: KAAL_BACKUP_DIR)")
    sp.add_argument("--keep", type=int, default=14, help="How many backups to keep (default 14)")
    sp.set_defaults(func=core.backup_vault)

    sp = sub.add_parser("export", help="Write the whole vault as stored (encrypted parts stay encrypted) to a new tar file")
    sp.add_argument("--output", required=True)
    sp.set_defaults(func=core.export_vault)

    sp = sub.add_parser("restore", help="Put the installation back as a backup holds it (stop Kaal first); what is there now is moved aside, not deleted")
    sp.add_argument("backup", help="The backup folder, for example /srv/kaal/backups/20260101T020000Z")
    sp.set_defaults(func=core.restore_backup)

    sp = sub.add_parser("export-readable", help="Write everything in the vault, decrypted, to a new zip: notes as Markdown, files, receipts.csv")
    sp.add_argument("--output", required=True, help="The zip file to create")
    sp.set_defaults(func=core.export_readable)

    sp = sub.add_parser("import", help="Restore a kaal export into an empty vault, then verify it")
    sp.add_argument("archive")
    sp.set_defaults(func=core.import_vault)

    sp = sub.add_parser("delete", help="Delete note and attachments")
    sp.add_argument("query")
    sp.add_argument("--yes", action="store_true")
    sp.set_defaults(func=core.delete_note)

    return p



def _run(argv: list[str] | None) -> None:
    args = build_parser().parse_args(argv)
    try:
        remote = client.remote_config()
    except client.ClientConfigError as error:
        core.die(str(error))
    if remote is None:
        args.func(args)
    else:
        client.run(remote, args)


def main(argv: list[str] | None = None) -> None:
    try:
        _run(argv)
    except BrokenPipeError:
        # The reader (for example `head`) stopped early. Exit quietly, as other
        # command-line tools do, and keep Python's exit-time flush from complaining.
        devnull = os.open(os.devnull, os.O_WRONLY)
        os.dup2(devnull, sys.stdout.fileno())
        raise SystemExit(141)


if __name__ == "__main__":
    main()
