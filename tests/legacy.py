"""Put stored content back into the layout Kaal used before everything was encrypted.

Older vaults hold non-sensitive notes as ``notes/<id>.md``, their attachments
as raw files, extracted text as ``extracted.md``, and the index and attachment
metadata as readable JSON. Kaal no longer writes that layout, but it must still
read it and migrate it, so tests build it here.
"""
import json
import os

import kaal.core as kaal


def make_plaintext(note_id: str) -> None:
    """Rewrite one note, its attachments, their sidecars and the index as readable files."""
    index = kaal.read_index_file()
    meta = next(note for note in index["notes"] if note["id"] == note_id)
    note = kaal.load_note(meta)
    kaal.encrypted_note_path(note_id).unlink()
    path = kaal.plaintext_note_path(note_id)
    path.write_text(note["body"], encoding="utf-8")
    os.chmod(path, 0o600)
    meta["storage"], meta["sensitivity"] = "plaintext", "public"
    for attachment in meta.get("attachments", []):
        folder = kaal.attachment_note_dir(note_id) / attachment["id"]
        data = kaal.attachment_original_bytes(meta, attachment)
        text = kaal.read_attachment_extracted_sidecar(meta, attachment)
        docling = kaal.read_attachment_docling_sidecar(meta, attachment)
        for name in ("original.json", "extracted.md.json", "docling.json.enc"):
            (folder / name).unlink(missing_ok=True)
        files = {attachment.get("stored_name", attachment["name"]): data}
        if text:
            files["extracted.md"] = text.encode()
        if docling:
            files["docling.json"] = json.dumps(docling).encode()
        for name, content in files.items():
            (folder / name).write_bytes(content)
            os.chmod(folder / name, 0o600)
        attachment["storage"], attachment["sensitivity"] = "plaintext", "public"
        kaal.secure_write_json(folder / "metadata.json", attachment)
    kaal.secure_write_json(kaal.INDEX_PATH, index)
