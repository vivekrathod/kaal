"""kaal medical evaluate: extraction measured against confirmed values, with no values in the output."""
import json
from types import SimpleNamespace

import pytest

import kaal.core as kaal
from kaal import receipts


@pytest.fixture()
def vault(tmp_path):
    saved = kaal.VAULT
    kaal.configure_vault(tmp_path / "vault")
    kaal.call_captured(kaal.init_vault, SimpleNamespace())
    yield tmp_path
    kaal.configure_vault(saved)


def call(handler, **params):
    return kaal.call_captured(handler, SimpleNamespace(**params))[0]


def capture(directory, text, *, name="receipt.txt", confirmed=None):
    source = directory / name
    source.write_text(text)
    note_id = call(kaal.medical_capture, file=str(source), title="", source_url="", source_title="", captured_at="")["id"]
    if confirmed:
        call(kaal.medical_update, note_id=note_id, fields_json=json.dumps(confirmed))
    return note_id


def evaluate(**params):
    return call(kaal.medical_evaluate, **{"ids": [], "limit": 0, **params})


@pytest.mark.parametrize("name, truth, candidate, expected", [
    ("amount", "40.00", "$40.00", "correct"),
    ("amount", "40.00", "45.00", "wrong"),
    ("amount", "40.00", "forty", "wrong"),
    ("amount", "40.00", "", "missing"),
    ("service_date", "2026-03-04", "3/4/26", "correct"),
    ("service_date", "2026-03-04", "2026-03-05", "wrong"),
    ("paid_date", "2026-03-04", "  ", "missing"),
    ("patient_name", "Jane Doe", "DOE, JANE Q", "correct"),
    ("patient_name", "Jane Doe", "John Doe", "wrong"),
    ("provider", "Example Health Care", "Example Health", "correct"),
    ("provider", "Example Health Care", "Health", "wrong"),
    ("provider", "Example Health", "EXAMPLE HEALTH CARE", "correct"),
    ("insurer", "Acme", "Other Insurance", "wrong"),
])
def test_values_are_compared_after_normalising(name, truth, candidate, expected):
    assert receipts.compare_field(name, truth, candidate) == expected


def test_evaluate_measures_the_real_extractor_on_synthetic_receipts(vault):
    good = capture(vault, "Patient: DOE, JANE\nAmount paid: $40.00\nService date: 03/04/2026\n",
                   confirmed={"amount": "40.00", "service_date": "2026-03-04", "patient_name": "Jane Doe", "provider": "Example Clinic"})
    off = capture(vault, "Amount paid: $12.00\nPayment date: 05/06/2026\n", name="other.txt",
                  confirmed={"amount": "21.00", "paid_date": "2026-05-06"})
    unconfirmed = capture(vault, "Amount paid: $5.00\nService date: 01/01/2026\n", name="auto.txt")

    result = evaluate()
    assert result["evaluated"] == 2
    assert result["skipped"] == [{"id": unconfirmed, "reason": "no confirmed values"}]
    by_id = {row["id"]: row for row in result["receipts"]}
    assert by_id[good]["results"] == {"amount": "correct", "service_date": "correct", "patient_name": "correct", "provider": "missing"}
    assert by_id[off]["results"] == {"amount": "wrong", "paid_date": "correct"}
    assert (by_id[good]["kind"], by_id[good]["method"]) == ("other", "text")
    assert result["overall"]["fields"]["amount"] == {"truth": 2, "correct": 1, "wrong": 1, "missing": 0, "accuracy": "1/2"}
    assert result["overall"]["fields"]["provider"]["accuracy"] == "0/1"
    assert "insurer" not in result["overall"]["fields"]
    assert (result["overall"]["correct"], result["overall"]["truth"], result["overall"]["percent"]) == (4, 6, 67)
    assert result["by_kind"]["other"]["truth"] == 6


def test_output_holds_ids_and_outcomes_but_no_values_and_the_vault_is_untouched(vault, monkeypatch):
    note_id = capture(vault, "Amount paid: $987.65\nService date: 07/08/2026\n", name="scan.jpg",
                      confirmed={"amount": "987.65", "service_date": "2026-07-08", "patient_name": "Zelda Quixote", "insurer": "Umbrella Mutual"})
    seen = {}

    def fake(path):
        seen["suffix"], seen["bytes"] = path.suffix, path.read_bytes()
        return {"amount": "987.65", "service_date": "2026-07-09", "patient_name": "QUIXOTE, ZELDA"}, "fake-model"

    monkeypatch.setattr(kaal, "receipt_extractor", fake)
    before = kaal.INDEX_PATH.read_bytes()
    result = evaluate()
    assert kaal.INDEX_PATH.read_bytes() == before
    assert (seen["suffix"], seen["bytes"]) == (".jpg", b"Amount paid: $987.65\nService date: 07/08/2026\n")
    assert result["receipts"] == [{"id": note_id, "kind": "image", "method": "fake-model", "seconds": result["receipts"][0]["seconds"],
                                   "results": {"amount": "correct", "service_date": "wrong", "patient_name": "correct", "insurer": "missing"}}]
    assert set(result["by_kind"]) == {"image"}
    printed = json.dumps(result)
    for secret in ("987.65", "2026-07-08", "Zelda", "Quixote", "Umbrella"):
        assert secret not in printed


def test_evaluate_can_be_limited_and_reports_what_it_could_not_run(vault, monkeypatch):
    first = capture(vault, "Amount paid: $1.00\n", name="a.txt", confirmed={"amount": "1.00"})
    second = capture(vault, "Amount paid: $2.00\n", name="b.txt", confirmed={"amount": "2.00"})
    trashed = capture(vault, "Amount paid: $3.00\n", name="c.txt", confirmed={"amount": "3.00"})
    call(kaal.medical_trash, note_id=trashed)

    assert evaluate(limit=1)["evaluated"] == 1
    assert [row["id"] for row in evaluate(ids=[second])["receipts"]] == [second]
    with pytest.raises(kaal.KaalError):
        evaluate(ids=[trashed])
    with pytest.raises(kaal.KaalError):
        evaluate(ids=["nope"])

    def broken(path):
        raise RuntimeError("extractor fell over")

    monkeypatch.setattr(kaal, "receipt_extractor", broken)
    result = evaluate()
    assert result["evaluated"] == 0
    assert sorted(item["id"] for item in result["skipped"]) == sorted([first, second])
    assert {item["reason"] for item in result["skipped"]} == {"extraction failed: RuntimeError"}
