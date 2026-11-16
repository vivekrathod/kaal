"""Receipt records: normalised values, family members, reimbursement and the report."""
import json
from datetime import date
from types import SimpleNamespace

import pytest

import kaal.core as kaal
from kaal import receipts


@pytest.fixture()
def vault(tmp_path):
    saved = kaal.VAULT
    kaal.configure_vault(tmp_path / "vault")
    kaal.call_captured(kaal.init_vault, SimpleNamespace())
    yield
    kaal.configure_vault(saved)


def call(handler, **params):
    return kaal.call_captured(handler, SimpleNamespace(**params))[0]


def add_receipt(fields, *, reviewed=False, sources=None, title="Receipt"):
    with kaal.vault_capture_lock():
        meta = kaal.create_note(title, "# Receipt", tags=["medical", "receipt"] + ([] if reviewed else ["inbox"]), sensitivity="public")
        index = kaal.load_index()
        saved = next(note for note in index["notes"] if note["id"] == meta["id"])
        saved["receipt"] = {"fields": dict(fields), "field_sources": dict(sources or {})}
        if reviewed:
            saved["reviewed_at"] = kaal.now_iso()
        kaal.save_index(index)
    return meta["id"]


def stored(note_id):
    return next(note for note in kaal.load_index()["notes"] if note["id"] == note_id)["receipt"]


def report(**filters):
    return call(kaal.medical_report, **{"year": "", "patient_name": "", "from_date": "", "to_date": "", "reimbursed": "all", **filters})


@pytest.mark.parametrize("raw, expected", [
    ("2026-10-16", "2026-10-16"),
    ("2026-1-5", "2026-01-05"),
    ("10/16/2026", "2026-10-16"),
    ("11/12/26", "2026-11-12"),
    ("1-2-2026", "2026-01-02"),
    ("11/12/26 7:43 PM", "2026-11-12"),
    ("10/16/2026          Other", "2026-10-16"),
    ("March 5, 2026", "2026-03-05"),
    ("Sept. 3rd 2026", "2026-09-03"),
    ("5 Mar 2026", "2026-03-05"),
    ("13/40/2026", ""),
    ("02/30/2026", ""),
    ("Member Identifier", ""),
    ("", ""),
])
def test_dates_are_read_month_first_and_invalid_ones_are_rejected(raw, expected):
    assert receipts.normalize_date(raw) == expected


@pytest.mark.parametrize("raw, expected", [
    ("123.45", "123.45"),
    ("$123.45", "123.45"),
    ("$1,234.5", "1234.50"),
    ("12", "12.00"),
    (" 40.00 USD ", "40.00"),
    ("12.345", ""),
    ("-5.00", ""),
    ("1,23.00", ""),
    ("about 40", ""),
    ("", ""),
])
def test_amounts_become_plain_two_decimal_values(raw, expected):
    assert receipts.normalize_amount(raw) == expected


def test_person_matching_ignores_order_case_and_extra_words():
    people = ["Jane Doe", "John Doe"]
    assert receipts.person_for("DOE, JANE Q", people) == "Jane Doe"
    assert receipts.person_for("john doe", people) == "John Doe"
    assert receipts.person_for("Doe", people) == ""
    assert receipts.person_for("Jane John Doe", people) == ""
    assert receipts.person_for("Someone Else", people) == ""
    assert receipts.person_for("Jane Doe", []) == ""


def test_upgrade_keeps_printed_values_flags_unreadable_ones_and_is_idempotent():
    receipt = {"fields": {"service_date": "10/16/2026", "paid_date": "sometime in May", "amount": "$1,200", "patient_name": "Pat Other"}, "field_sources": {"amount": "manual"}}
    assert receipts.upgrade(receipt, reviewed=False, people=["Jane Doe"]) is True
    assert receipt["fields"]["service_date"] == "2026-10-16"
    assert receipt["fields"]["paid_date"] == ""
    assert receipt["fields"]["amount"] == "1200.00"
    assert receipt["field_raw"] == {"service_date": "10/16/2026", "paid_date": "sometime in May", "amount": "$1,200"}
    assert receipt["flags"] == {"paid_date": "unparsed", "patient_name": "unknown_person"}
    assert receipt["field_sources"] == {"amount": "manual"}
    assert (receipt["schema"], receipt["status"], receipt["person"]) == (2, "needs_review", "")
    snapshot = json.dumps(receipt, sort_keys=True)
    assert receipts.upgrade(receipt, reviewed=False, people=["Jane Doe"]) is False
    assert json.dumps(receipt, sort_keys=True) == snapshot


def test_old_records_read_as_upgraded_and_reach_the_disk_with_the_next_write(vault):
    note_id = add_receipt({"amount": "12.50", "service_date": "2026-01-02"})
    index = kaal.read_index_file()
    old = next(note for note in index["notes"] if note["id"] == note_id)
    old["receipt"] = {"fields": {"amount": "12.50", "service_date": "01/02/2026"}, "field_sources": {"service_date": "manual"}}
    kaal.secure_write_json(kaal.INDEX_PATH, index)

    assert stored(note_id)["fields"]["service_date"] == "2026-01-02"
    on_disk = next(note for note in kaal.read_index_file()["notes"] if note["id"] == note_id)
    assert "schema" not in on_disk["receipt"]

    call(kaal.medical_people, add=["Jane Doe"], remove=[])
    on_disk = next(note for note in kaal.read_index_file()["notes"] if note["id"] == note_id)
    assert on_disk["receipt"]["schema"] == 2
    assert on_disk["receipt"]["fields"]["service_date"] == "2026-01-02"
    assert on_disk["receipt"]["field_raw"] == {"service_date": "01/02/2026"}
    assert on_disk["receipt"]["field_sources"] == {"service_date": "manual"}


def test_a_year_report_includes_every_printed_date_format(vault):
    for printed in ("10/16/2026", "11/12/26", "2026-03-01", "March 5, 2026"):
        add_receipt({"amount": "10.00", "service_date": printed})
    add_receipt({"amount": "99.00", "paid_date": "12/31/2025"})
    result = report(year="2026")
    assert result["count"] == 4
    assert result["total_amount"] == "40.00"
    assert result["by_year"] == [{"year": "2026", "count": 4, "total_amount": "40.00", "unreimbursed_amount": "40.00"}]
    assert report(from_date="1/1/2026", to_date="2026-06-30")["count"] == 2
    assert report()["count"] == 5


def test_report_sums_exactly(vault):
    for amount in ("0.10", "0.20", "1,000.05"):
        add_receipt({"amount": amount, "service_date": "2026-01-01"})
    assert report()["total_amount"] == "1000.35"


def test_report_lists_undated_and_amountless_receipts_instead_of_dropping_them(vault):
    dated = add_receipt({"amount": "5.00", "service_date": "2026-02-02"})
    undated = add_receipt({"amount": "7.00"})
    no_amount = add_receipt({"service_date": "2026-02-03"})
    everything = report()
    assert everything["count"] == 3
    assert everything["total_amount"] == "12.00"
    assert everything["undated"] == [undated]
    assert everything["missing_amount"] == [no_amount]
    in_year = report(year="2026")
    assert sorted(row["id"] for row in in_year["receipts"]) == sorted([dated, no_amount])
    assert in_year["undated"] == [undated]
    assert in_year["total_amount"] == "5.00"


def test_report_groups_by_person_and_splits_out_what_is_not_reimbursed(vault):
    call(kaal.medical_people, add=["Jane Doe", "John Doe"], remove=[])
    jane_a = add_receipt({"amount": "100.00", "service_date": "2026-01-10", "patient_name": "DOE, JANE"}, reviewed=True)
    add_receipt({"amount": "50.00", "service_date": "2026-02-10", "patient_name": "Jane Doe"}, reviewed=True)
    add_receipt({"amount": "30.00", "service_date": "2025-03-10", "patient_name": "John Doe"}, reviewed=True)
    add_receipt({"amount": "8.00", "service_date": "2026-04-10", "patient_name": "Unknown Person"})
    call(kaal.medical_reimburse, note_ids=[jane_a], date="2026-05-01", note="HSA withdrawal 1", undo=False)

    result = report()
    assert (result["total_amount"], result["reimbursed_amount"], result["unreimbursed_amount"]) == ("188.00", "100.00", "88.00")
    assert result["needs_review"] == {"count": 1, "amount": "8.00"}
    assert result["by_person"] == [
        {"person": "", "count": 1, "total_amount": "8.00", "unreimbursed_amount": "8.00"},
        {"person": "Jane Doe", "count": 2, "total_amount": "150.00", "unreimbursed_amount": "50.00"},
        {"person": "John Doe", "count": 1, "total_amount": "30.00", "unreimbursed_amount": "30.00"},
    ]
    assert [group["year"] for group in result["by_year"]] == ["2025", "2026"]
    assert report(reimbursed="no")["total_amount"] == "88.00"
    assert [row["id"] for row in report(reimbursed="yes")["receipts"]] == [jane_a]
    assert report(patient_name="jane")["count"] == 2
    assert report(patient_name="unknown")["count"] == 1


def test_report_rejects_filters_it_cannot_read(vault):
    for filters in ({"year": "26"}, {"from_date": "soon"}, {"to_date": "13/45/2026"}, {"reimbursed": "maybe"}):
        with pytest.raises(kaal.KaalError):
            report(**filters)


def test_reimbursement_needs_a_reviewed_receipt_with_an_amount_and_can_be_undone(vault):
    reviewed = add_receipt({"amount": "20.00", "service_date": "2026-01-01"}, reviewed=True)
    in_inbox = add_receipt({"amount": "20.00", "service_date": "2026-01-01"})
    no_amount = add_receipt({"service_date": "2026-01-01"}, reviewed=True)

    with pytest.raises(kaal.KaalError, match="Review"):
        call(kaal.medical_reimburse, note_ids=[reviewed, in_inbox], date="2026-02-01", note="", undo=False)
    assert "reimbursed_at" not in stored(reviewed)
    with pytest.raises(kaal.KaalError, match="amount"):
        call(kaal.medical_reimburse, note_ids=[no_amount], date="", note="", undo=False)
    with pytest.raises(kaal.KaalError, match="date"):
        call(kaal.medical_reimburse, note_ids=[reviewed], date="next week", note="", undo=False)

    result = call(kaal.medical_reimburse, note_ids=[reviewed, reviewed], date="2/1/26", note="ref 42", undo=False)
    assert result == {"status": "reimbursed", "count": 1, "ids": [reviewed], "reimbursed_at": "2026-02-01", "total_amount": "20.00"}
    assert (stored(reviewed)["reimbursed_at"], stored(reviewed)["reimbursement_note"]) == ("2026-02-01", "ref 42")

    assert call(kaal.medical_reimburse, note_ids=[reviewed], date="", note="", undo=False)["reimbursed_at"] == date.today().isoformat()
    assert "reimbursement_note" not in stored(reviewed)

    assert call(kaal.medical_reimburse, note_ids=[reviewed], date="", note="", undo=True)["status"] == "unreimbursed"
    assert "reimbursed_at" not in stored(reviewed)


def test_reextraction_keeps_confirmed_values_and_the_reimbursement(vault):
    prior = {
        "fields": {"amount": "12.34", "service_date": "2026-01-01", "provider": "Auto Clinic"},
        "field_raw": {"service_date": "01/01/2026"},
        "field_sources": {"amount": "confirmed", "service_date": "manual", "provider": "labelled-text"},
        "reimbursed_at": "2026-03-01",
        "reimbursement_note": "ref 7",
    }
    fresh = kaal.build_receipt_state("Amount Paid: $99.00\nDate of Service: 02/02/2026\n", prior)
    assert fresh["fields"]["amount"] == "12.34"
    assert fresh["fields"]["service_date"] == "2026-01-01"
    assert fresh["field_sources"]["amount"] == "confirmed"
    assert fresh["field_raw"]["service_date"] == "01/01/2026"
    assert fresh["fields"]["provider"] == ""
    assert (fresh["reimbursed_at"], fresh["reimbursement_note"]) == ("2026-03-01", "ref 7")


def test_changing_the_family_list_rematches_existing_receipts(vault):
    note_id = add_receipt({"amount": "5.00", "patient_name": "DOE, JANE"})
    assert stored(note_id)["person"] == ""
    assert stored(note_id)["flags"] == {}

    assert call(kaal.medical_people, add=["Jane  Doe", "jane doe", "John Doe"], remove=[])["people"] == ["Jane Doe", "John Doe"]
    assert stored(note_id)["person"] == "Jane Doe"
    assert call(kaal.medical_people, add=[], remove=[]) == {"status": "ok", "people": ["Jane Doe", "John Doe"]}

    call(kaal.medical_people, add=[], remove=["JANE DOE"])
    assert stored(note_id)["person"] == ""
    assert stored(note_id)["flags"] == {"patient_name": "unknown_person"}
    with pytest.raises(kaal.KaalError):
        call(kaal.medical_people, add=["  "], remove=[])


def test_manual_corrections_are_normalised_and_unreadable_ones_refused(vault):
    note_id = add_receipt({"amount": "1.00"})
    result = call(kaal.medical_update, note_id=note_id, fields_json=json.dumps({"service_date": "3/4/26", "amount": "$1,250.5", "provider": " Clinic "}))
    assert result["fields"]["service_date"] == "2026-03-04"
    assert result["fields"]["amount"] == "1250.50"
    assert result["fields"]["provider"] == "Clinic"
    receipt = stored(note_id)
    assert receipt["field_raw"] == {"service_date": "3/4/26", "amount": "$1,250.5"}
    assert receipt["field_sources"] == {"service_date": "manual", "amount": "manual", "provider": "manual"}

    for bad in ({"paid_date": "last Tuesday"}, {"amount": "twelve"}):
        with pytest.raises(kaal.KaalError):
            call(kaal.medical_update, note_id=note_id, fields_json=json.dumps(bad))
    assert stored(note_id)["fields"].get("paid_date", "") == ""
    assert stored(note_id)["fields"]["amount"] == "1250.50"

    call(kaal.medical_update, note_id=note_id, fields_json=json.dumps({"service_date": ""}))
    receipt = stored(note_id)
    assert receipt["fields"]["service_date"] == ""
    assert "service_date" not in receipt["field_raw"]
    assert "service_date" not in receipt["field_sources"]


def test_status_follows_review_and_reopen(vault):
    note_id = add_receipt({"amount": "12.34", "service_date": "01/01/2026"})
    assert stored(note_id)["status"] == "needs_review"
    call(kaal.medical_review, note_id=note_id)
    assert stored(note_id)["status"] == "reviewed"
    assert stored(note_id)["field_sources"] == {"amount": "confirmed", "service_date": "confirmed"}
    call(kaal.medical_reopen, note_id=note_id)
    assert stored(note_id)["status"] == "needs_review"
