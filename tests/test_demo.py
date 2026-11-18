"""deploy/demo: the made-up data, put in through Kaal's own API, and the files around it.

The seed runs against a real Kaal (a TestClient server with a throwaway home),
and kaal-server's own command makes the setup links, as in the container.
run.sh runs with a stand-in for docker that only records what it is asked.

The last test builds and runs the real demo with Docker, which takes a few
minutes, so it is off unless KAAL_DEMO_DOCKER=1 is set.
"""
import base64
import contextlib
import hashlib
import importlib.util
import io
import json
import os
import re
import secrets
import shutil
import socket
import stat
import subprocess
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import kaal.core as kaal
from kaal import server

ROOT = Path(__file__).resolve().parents[1]
DEMO = ROOT / "deploy" / "demo"
TODAY = date(2026, 11, 17)
REVIEWER_PASSWORD = "made-up-reviewer-1"
ADMIN_PASSWORD = "made-up-admin-1"


def _load():
    spec = importlib.util.spec_from_file_location("kaal_demo", DEMO / "demo.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules["kaal_demo"] = module     # dataclasses look their module up while it loads
    spec.loader.exec_module(module)
    return module


demo = _load()
needs_pdftotext = pytest.mark.skipif(not shutil.which("pdftotext"), reason="needs pdftotext")


@pytest.fixture()
def kaal_server(tmp_path, monkeypatch):
    """A new installation, as the container starts it: a key, an empty vault, and kaal-server."""
    saved = kaal.VAULT
    kaal.configure_vault(tmp_path / "data")
    kaal.call_captured(kaal.init_vault, SimpleNamespace())
    monkeypatch.setenv("KAAL_HOME", str(tmp_path))
    monkeypatch.setenv("KAAL_VAULT", str(tmp_path / "data"))
    monkeypatch.setenv("KAAL_PUBLIC_URL", "https://demo.example.org")
    settings = server.Settings(home=tmp_path, vault=tmp_path / "data", tokens_file=tmp_path / "tokens.json", tmp_dir=tmp_path / "tmp", rate_per_minute=100_000)
    app = server.create_app(settings)
    # The vault's clock, which records seconds, moves on two seconds at each of the
    # seed's pauses instead of the seed sleeping: the order of each list is then
    # exactly what it is in the container, without the wait.
    ahead = {"seconds": 0}
    monkeypatch.setattr(kaal, "now_iso", lambda: (datetime.now(timezone.utc) + timedelta(seconds=ahead["seconds"])).isoformat(timespec="seconds"))

    def pause():
        ahead["seconds"] += 2

    def connect():
        client = TestClient(app, base_url="https://testserver")

        def send(method, path, headers, data):
            client.cookies.clear()       # the session travels only as demo.Kaal sends it
            answer = client.request(method, path, headers=headers, content=data)
            return answer.status_code, answer.headers.get_list("set-cookie"), answer.content

        return demo.Kaal(send)

    def run(args):
        printed = io.StringIO()
        monkeypatch.setattr(sys, "argv", ["kaal-server", *args])
        with contextlib.redirect_stdout(printed), contextlib.redirect_stderr(io.StringIO()):
            server.main()
        return printed.getvalue()

    def signed_in(login, password):
        person = connect()
        person.call("POST", "/v1/sign-in", {"login": login, "password": password})
        return person

    yield SimpleNamespace(app=app, connect=connect, run=run, signed_in=signed_in, home=tmp_path, pause=pause)
    kaal.configure_vault(saved)


@pytest.fixture()
def seeded(kaal_server):
    kaal_server.made = demo.seed(kaal_server.connect, kaal_server.run, reviewer_password=REVIEWER_PASSWORD,
                                 admin_password=ADMIN_PASSWORD, today=TODAY, reset_at="03:00", pause=kaal_server.pause)
    return kaal_server


def by_title(listing):
    return {note["title"]: note for note in listing}


def test_the_made_up_notes_are_in_with_their_picture_and_pdfs(seeded):
    jane = seeded.signed_in("reviewer", REVIEWER_PASSWORD)
    listing = jane.call("GET", "/v1/library/notes")
    assert listing[0]["title"] == demo.NOTICE_TITLE, "the demo notice is at the top"
    expected = {note.title for note in demo.notes(TODAY) if not note.trash} | {demo.NOTICE_TITLE}
    assert {note["title"] for note in listing} == expected
    assert all(note["title"].startswith(("Made-up", "Read me")) for note in listing)
    assert [note["title"] for note in jane.call("GET", "/v1/library/notes?trash=true")] == ["Made-up packing list (old)"]
    notes = by_title(listing)

    # The note the iPhone app's screen-by-screen tests open.
    house = jane.call("GET", f"/v1/library/notes/{notes['Made-up house notes']['id']}")["note"]
    for shown in ("## Boiler", "**Pressure should be 1.5 bar**", "| Hall |", "radiators"):
        assert shown in house["body"]
    assert [file["name"] for file in house["attachments"]] == ["northside.pdf"]
    assert "attachment:" not in house["body"], "the PDF is listed under the note, not placed in it"
    found = jane.call("POST", "/v1/library/search", {"query": "radiators"})
    assert "Made-up house notes" in {note["title"] for note in found} and "Made-up shopping list" not in {note["title"] for note in found}

    garden = jane.call("GET", f"/v1/library/notes/{notes['Made-up garden plan']['id']}")["note"]
    (picture,) = garden["attachments"]
    assert f"![Garden plan](attachment:{picture['id']})" in garden["body"]
    client = TestClient(seeded.app, base_url="https://testserver", headers={"X-Kaal-Request": "1", "Cookie": f"kaal_session={jane.session}"})
    shown = client.get(f"/v1/notes/{garden['id']}/attachments/{picture['id']}/view")
    assert shown.status_code == 200 and shown.headers["content-type"] == "image/png"

    insurance = jane.call("GET", f"/v1/library/notes/{notes['Made-up home insurance summary']['id']}")["note"]
    (summary,) = insurance["attachments"]
    assert f"\n[insurance-summary.pdf](attachment:{summary['id']})\n" in insurance["body"], "a PDF on a line of its own shows as a card"
    pdf = client.get(f"/v1/notes/{insurance['id']}/attachments/{summary['id']}/view")
    assert pdf.status_code == 200 and pdf.content.startswith(b"%PDF-")

    notice = jane.call("GET", f"/v1/library/notes/{notes[demo.NOTICE_TITLE]['id']}")["note"]
    assert "Anything you add is removed every night" in notice["body"] and "03:00 UTC" in notice["body"]


def test_the_made_up_receipts_are_waiting_reviewed_and_reimbursed(seeded):
    jane = seeded.signed_in("reviewer", REVIEWER_PASSWORD)
    assert jane.call("GET", "/v1/medical/people")["people"] == demo.FAMILY
    waiting = jane.call("GET", "/v1/medical/receipts?inbox=true&sort=updated")
    reviewed = jane.call("GET", "/v1/medical/receipts?status=reviewed&sort=date")
    assert len(waiting) == 4 and {receipt["receipt"]["status"] for receipt in waiting} == {"needs_review"}
    assert len(reviewed) == 4
    assert [receipt["title"] for receipt in jane.call("GET", "/v1/medical/receipts?trash=true")] == ["Example Dental Group: cleaning (saved twice)"]
    assert waiting[0]["title"] == "Example Family Clinic: office visit", "the last one captured is at the top"

    paid_back = [receipt for receipt in reviewed if receipt["receipt"].get("reimbursed_at")]
    assert [receipt["receipt"]["fields"]["amount"] for receipt in paid_back] == ["25.00"]
    assert paid_back[0]["receipt"]["reimbursement_note"] == "HSA withdrawal (made up)"
    values = {receipt["title"]: receipt["receipt"] for receipt in reviewed}
    assert values["Physical therapy copay"]["fields"]["provider"] == "Example Physical Therapy"
    assert values["Physical therapy copay"]["field_sources"]["provider"] == "manual", "what Kaal could not read was typed in"
    assert values["Example Dental Group: cleaning"]["person"] == "John Madeup", "MADEUP, JOHN is matched to the family"
    assert values["Example Dental Group: cleaning"]["field_sources"]["amount"] == "confirmed"

    report = jane.call("POST", "/v1/medical/report", {})
    assert report["reimbursed_amount"] == "25.00"
    assert [group["year"] for group in report["by_year"]] == ["2025", "2026"]
    assert {group["person"] for group in report["by_person"]} >= {"Jane Madeup", "John Madeup", "Robin Madeup"}
    hosts = jane.call("GET", "/v1/medical/sources")["hosts"]
    assert hosts and all(re.fullmatch(r"[a-z.]+\.example\.(org|com|net)", host) for host in hosts)


@needs_pdftotext
def test_the_receipts_waiting_show_what_kaal_read_from_them(seeded):
    jane = seeded.signed_in("reviewer", REVIEWER_PASSWORD)
    waiting = {receipt["title"]: receipt["receipt"] for receipt in jane.call("GET", "/v1/medical/receipts?inbox=true")}
    clinic = waiting["Example Family Clinic: office visit"]
    # The iPhone app's screen-by-screen tests look for a $45.00 receipt to check.
    assert clinic["fields"]["amount"] == "45.00" and clinic["field_sources"]["amount"] == "labelled-text"
    assert clinic["person"] == "Jane Madeup" and clinic["fields"]["service_date"] == "2026-11-08"
    card = waiting["Example Street Pharmacy"]
    assert card["fields"]["amount"] == "12.80" and card["fields"]["paid_date"] == "2026-11-01" and not card["fields"]["patient_name"]
    assert not waiting["Example Imaging Center: statement"]["fields"]["amount"], "a bill with nothing paid has no amount to count"
    every = jane.call("GET", "/v1/medical/receipts") + jane.call("GET", "/v1/medical/receipts?trash=true")
    assert sum(receipt["receipt"]["fields"].get("amount") == "45.00" for receipt in every) == 1


@needs_pdftotext
def test_kaals_rules_read_each_made_up_pdf_as_intended(tmp_path):
    for receipt in demo.receipts(TODAY):
        media, content = receipt.content()
        if media != "application/pdf":
            continue
        path = tmp_path / receipt.file
        path.write_bytes(content)
        fields, method = kaal.extract_receipt_fields(path)
        assert method == "pdftotext-layout", receipt.file
        for name, value in receipt.expected.items():
            # The physical therapist prints no provider label, so the seed types it in.
            assert fields[name] == ("" if receipt.file == "example-pt-copay.pdf" and name == "provider" else value), (receipt.file, name)


def test_people_vaults_and_sign_up_are_as_app_review_needs(seeded):
    jane = seeded.signed_in("reviewer", REVIEWER_PASSWORD)
    account = jane.call("GET", "/v1/account")
    assert account["account"] == {"login": "reviewer", "name": "Jane Madeup", "admin": False}
    assert account["vault"] == {"id": account["vault"]["id"], "name": demo.VAULT_NAME, "role": "owner"}
    assert [vault["name"] for vault in account["vaults"]] == [demo.VAULT_NAME, demo.HOUSEHOLD]
    assert [session["current"] for session in account["sessions"]] == [True], "the seed signed itself out"
    assert account["has_password"] and account["recovery_codes_left"] == 8
    assert {(member["login"], member["role"]) for member in jane.call("GET", "/v1/vault/members")["members"]} == {("reviewer", "owner"), ("john", "viewer")}
    assert [token["name"] for token in jane.call("GET", "/v1/account/tokens")["tokens"]] == ["Kaal for Chrome"]

    jane.call("POST", "/v1/session/vault", {"vault": account["vaults"][1]["id"]})
    assert {note["title"] for note in jane.call("GET", "/v1/library/notes")} == {note.title for note in demo.household_notes(TODAY)}
    assert ("john", "editor") in {(member["login"], member["role"]) for member in jane.call("GET", "/v1/vault/members")["members"]}

    admin = seeded.signed_in("owner", ADMIN_PASSWORD)
    people = admin.call("GET", "/v1/admin/people")
    assert people["signup"] == "invitation"
    assert {person["login"]: person["admin"] for person in people["people"]} == {"owner": True, "reviewer": False, "john": False}
    assert seeded.connect().call("GET", "/v1/sign-in/options")["signup"] == "invitation"
    with pytest.raises(demo.DemoError, match="401"):
        seeded.signed_in("john", "anything-at-all")


def test_a_changed_reviewer_password_is_put_back(seeded):
    jane = seeded.signed_in("reviewer", REVIEWER_PASSWORD)
    assert demo.keep_password(seeded.connect, seeded.run, REVIEWER_PASSWORD) is False
    jane.call("POST", "/v1/account/password", {"current": REVIEWER_PASSWORD, "new": "someone-else-chose-this"})
    assert not demo.password_holds("reviewer", REVIEWER_PASSWORD)

    assert demo.keep_password(seeded.connect, seeded.run, REVIEWER_PASSWORD) is True
    assert demo.password_holds("reviewer", REVIEWER_PASSWORD)
    seeded.signed_in("reviewer", REVIEWER_PASSWORD)
    with pytest.raises(demo.DemoError, match="401"):
        seeded.signed_in("reviewer", "someone-else-chose-this")
    with pytest.raises(demo.DemoError, match="401"):
        jane.call("GET", "/v1/account")     # putting it back signed everyone out
    assert len(seeded.signed_in("reviewer", REVIEWER_PASSWORD).call("GET", "/v1/library/notes")) == 8, "the notes stay until the nightly reset"


def test_the_seed_refuses_passwords_that_could_not_be_typed_or_kept(kaal_server):
    for weak in ("short", "has spaces in it", "dollar$signs-here", ""):
        with pytest.raises(demo.DemoError, match="password"):
            demo.seed(kaal_server.connect, kaal_server.run, reviewer_password=weak, admin_password=ADMIN_PASSWORD, today=TODAY, pause=kaal_server.pause)
    assert kaal_server.connect().call("GET", "/v1/sign-in/options")["signup"] == "invitation"


def test_the_demo_writes_none_of_kaals_files_and_logs_no_password():
    source = (DEMO / "demo.py").read_text()
    assert "index.json" not in source and "accounts.db" not in source
    assert "print(" not in source.replace("print(__doc__", ""), "nothing is printed but the help"
    for line in source.splitlines():
        if "log." in line:
            assert "_password" not in line and "run(" not in line, line


def test_the_next_reset_is_the_next_time_the_clock_reads_it():
    at = lambda text: datetime.fromisoformat(text).replace(tzinfo=timezone.utc)  # noqa: E731
    assert demo.next_reset(at("2026-11-17T01:00:00"), "03:00") == at("2026-11-17T03:00:00")
    assert demo.next_reset(at("2026-11-17T03:00:00"), "03:00") == at("2026-11-18T03:00:00")
    assert demo.next_reset(at("2026-12-31T23:30:00"), "0:15") == at("2027-01-01T00:15:00")
    for bad in ("3am", "24:00", "03:60", ""):
        with pytest.raises(demo.DemoError):
            demo.next_reset(at("2026-11-17T01:00:00"), bad)


def test_only_folders_held_in_memory_are_ever_emptied(tmp_path):
    mounts = "tmpfs /srv/kaal tmpfs rw,nosuid,nodev,noexec,size=262144k,mode=700,uid=999,gid=999 0 0\n" \
             "tmpfs /etc/kaal tmpfs rw,size=1024k,mode=700 0 0\n/dev/vda1 /srv/other ext4 rw 0 0\n"
    demo.refuse_unless_in_memory([Path("/srv/kaal"), Path("/etc/kaal")], mounts)
    for lasting in (Path("/srv/other"), Path("/srv/elsewhere")):
        with pytest.raises(demo.DemoError, match="must be in memory"):
            demo.refuse_unless_in_memory([Path("/srv/kaal"), lasting], mounts)
    (tmp_path / "data" / "notes").mkdir(parents=True)
    (tmp_path / "data" / "notes" / "a.json").write_text("{}")
    (tmp_path / "master.key").write_text("made-up")
    demo.empty(tmp_path)
    assert list(tmp_path.iterdir()) == []


def test_the_health_check_waits_for_the_made_up_data(tmp_path, monkeypatch):
    monkeypatch.setattr(demo, "READY", tmp_path / "ready")
    monkeypatch.setattr(demo, "_answers", lambda: True)
    assert demo.main(["demo.py", "health"]) == 1
    (tmp_path / "ready").write_text("made-up\n")
    assert demo.main(["demo.py", "health"]) == 0
    monkeypatch.setattr(demo, "_answers", lambda: False)
    assert demo.main(["demo.py", "health"]) == 1


def test_the_setup_link_code_is_read_from_what_kaal_server_prints():
    assert demo.setup_code("https://demo.example.org/#/setup/AbC_12-x\n") == "AbC_12-x"
    with pytest.raises(demo.DemoError):
        demo.setup_code("ERROR: No account 'nobody'.")


# --- run.sh, with a docker that only records what it is asked

DOCKER = r"""#!/bin/sh
printf '%s\n' "$*" >> "$DOCKER_CALLS"
case "$*" in "compose -p "*) env | grep -E '^(KAAL_|COMPOSE_)' >> "$DOCKER_CALLS.env" ;; esac
case "$*" in
info) exit 0 ;;
"compose version --short") echo "${COMPOSE_VERSION:-2.29.1}" ;;
*" ps -q kaal") echo made-up-container ;;
"inspect --format {{.State.Health.Status}} made-up-container") echo healthy ;;
*"exec -T kaal cat /tmp/kaal-demo-ready")
    count=$(cat "$DOCKER_CALLS.marker" 2>/dev/null || echo 0); count=$((count + 1)); echo "$count" > "$DOCKER_CALLS.marker"
    echo "made-up-marker-$((count / 2))" ;;
esac
exit 0
"""


@pytest.fixture()
def demo_folder(tmp_path):
    """A copy of deploy/demo to run, so that settings files land in a throwaway folder."""
    folder = tmp_path / "deploy" / "demo"
    shutil.copytree(DEMO, folder, ignore=shutil.ignore_patterns("*.env", "__pycache__"))
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "docker").write_text(DOCKER)
    (bin_dir / "docker").chmod(0o755)
    calls = tmp_path / "docker-calls"

    def run_sh(*args, **env):
        environment = {"PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}", "DOCKER_CALLS": str(calls), "HOME": str(tmp_path), **env}
        calls.write_text("")
        Path(f"{calls}.env").write_text("")
        done = subprocess.run(["sh", str(folder / "run.sh"), *args], env=environment, text=True, capture_output=True, timeout=60)
        return done, calls.read_text().splitlines()

    return SimpleNamespace(folder=folder, run=run_sh, settings=lambda project="kaal-demo": folder / f"{project}.env",
                           docker_saw=lambda: Path(f"{calls}.env").read_text())


def read_settings(path):
    return dict(line.split("=", 1) for line in path.read_text().splitlines() if line and not line.startswith("#"))


def test_run_sh_starts_a_local_demo_with_its_own_names_and_prints_the_password_once(demo_folder):
    done, calls = demo_folder.run("--local", "--port", "8797")
    assert done.returncode == 0, done.stderr
    stored = read_settings(demo_folder.settings())
    assert stat.S_IMODE(demo_folder.settings().stat().st_mode) == 0o600
    assert stored["KAAL_PUBLIC_URL"] == "http://localhost:8797" and stored["KAAL_LISTEN"] == "127.0.0.1:8797"
    assert stored["KAAL_COOKIE_SECURE"] == "0" and stored["COMPOSE_PROFILES"] == "" and stored["KAAL_DEMO_RESET_AT"] == "03:00"
    assert stored["KAAL_IMAGE"] == "kaal-demo:local", "never kaal:local, which a regular installation runs"
    for password in (stored["KAAL_DEMO_PASSWORD"], stored["KAAL_DEMO_ADMIN_PASSWORD"]):
        assert re.fullmatch(r"[a-z2-9]{4}(-[a-z2-9]{4}){3}", password)
        assert password in done.stdout
    assert stored["KAAL_DEMO_PASSWORD"] != stored["KAAL_DEMO_ADMIN_PASSWORD"]
    composed = [call for call in calls if call.startswith("compose -p")]
    assert composed and all(call.startswith("compose -p kaal-demo --env-file kaal-demo.env -f compose.yaml ") for call in composed)
    assert any(call.endswith(" build --quiet kaal") for call in composed) and any(" up -d " in call for call in composed)

    again, _calls = demo_folder.run()
    assert again.returncode == 0, again.stderr
    assert read_settings(demo_folder.settings()) == stored, "the passwords and the address stay as they were"
    assert stored["KAAL_DEMO_PASSWORD"] not in again.stdout and stored["KAAL_DEMO_ADMIN_PASSWORD"] not in again.stdout
    assert "unchanged" in again.stdout


def test_run_sh_keeps_settings_it_does_not_write(demo_folder):
    demo_folder.run("--local")
    settings = demo_folder.settings()
    settings.write_text(settings.read_text() + "KAAL_DEMO_STORAGE=512m\n")
    done, _calls = demo_folder.run("--port", "8798")
    assert done.returncode == 0, done.stderr
    stored = read_settings(settings)
    assert stored["KAAL_DEMO_STORAGE"] == "512m" and stored["KAAL_LISTEN"] == "127.0.0.1:8798"
    assert stored["KAAL_PUBLIC_URL"] == "http://localhost:8798", "still the local demo, now on the new port"
    assert settings.read_text().count("KAAL_LISTEN=") == 1 and settings.read_text().count("# Written by") == 1


def test_run_sh_lets_only_its_settings_file_decide(demo_folder):
    done, _calls = demo_folder.run("--local", KAAL_PUBLIC_URL="https://elsewhere.example.org", KAAL_IMAGE="someone-else:latest",
                                   KAAL_DEMO_ADMIN_PASSWORD="made-up-other-1", COMPOSE_PROFILES="https")
    assert done.returncode == 0, done.stderr
    stored = read_settings(demo_folder.settings())
    assert stored["KAAL_PUBLIC_URL"] == "http://localhost:8770" and stored["KAAL_IMAGE"] == "kaal-demo:local"
    assert stored["KAAL_DEMO_ADMIN_PASSWORD"] != "made-up-other-1" and stored["COMPOSE_PROFILES"] == ""
    seen = demo_folder.docker_saw()
    for elsewhere in ("elsewhere.example.org", "someone-else", "made-up-other-1", "COMPOSE_PROFILES=https"):
        assert elsewhere not in seen, "Docker Compose would have preferred it to the settings file"


def test_the_demo_settings_are_never_committed():
    assert "/deploy/demo/*.env" in (ROOT / ".gitignore").read_text().splitlines()


def test_run_sh_takes_the_reviewer_password_from_the_environment(demo_folder):
    done, _calls = demo_folder.run("--local", KAAL_DEMO_PASSWORD="App-Review-2026")
    assert done.returncode == 0, done.stderr
    assert read_settings(demo_folder.settings())["KAAL_DEMO_PASSWORD"] == "App-Review-2026"
    assert "App-Review-2026" not in done.stdout
    for bad in ("short", "has a space in it", "dollar$sign-in-it"):
        refused, _calls = demo_folder.run(KAAL_DEMO_PASSWORD=bad)
        assert refused.returncode == 1 and "KAAL_DEMO_PASSWORD must be" in refused.stderr
    assert read_settings(demo_folder.settings())["KAAL_DEMO_PASSWORD"] == "App-Review-2026"


def test_run_sh_sets_up_https_for_a_domain_of_its_own(demo_folder):
    done, _calls = demo_folder.run("--domain", "HTTPS://Demo.Example.org/", "--reset-at", "04:30")
    assert done.returncode == 0, done.stderr
    stored = read_settings(demo_folder.settings())
    assert stored["KAAL_PUBLIC_URL"] == "https://demo.example.org" and stored["KAAL_DOMAIN"] == "demo.example.org"
    assert stored["COMPOSE_PROFILES"] == "https" and stored["KAAL_COOKIE_SECURE"] == "1" and stored["KAAL_DEMO_RESET_AT"] == "04:30"


@pytest.mark.parametrize(("args", "env", "says"), [
    ((), {}, "say where people will open the demo"),
    (("--local",), {"KAAL_DEMO_PROJECT": "kaal"}, "regular installation"),
    (("--local",), {"KAAL_DEMO_PROJECT": "Bad Name"}, "KAAL_DEMO_PROJECT may hold only"),
    (("--local", "--port", "80"), {}, "--port must be"),
    (("--local", "--reset-at", "25:00"), {}, "--reset-at must be"),
    (("--domain", "not a domain"), {}, "does not look like a domain"),
    (("--behind", "http://plain.example.org"), {}, "should look like https://"),
    (("--local",), {"COMPOSE_VERSION": "2.20.0"}, "too old"),
    (("reset",), {}, "not set up here yet"),
])
def test_run_sh_refuses_what_it_cannot_do_safely(demo_folder, args, env, says):
    done, calls = demo_folder.run(*args, **env)
    assert done.returncode == 1 and says in done.stderr
    assert not any(" up " in call or " build " in call for call in calls)


def test_run_sh_resets_and_removes_only_its_own_project(demo_folder):
    demo_folder.run("--local", KAAL_DEMO_PROJECT="kaal-test-made-up")
    done, calls = demo_folder.run("reset", KAAL_DEMO_PROJECT="kaal-test-made-up")
    assert done.returncode == 0, done.stderr
    assert "compose -p kaal-test-made-up --env-file kaal-test-made-up.env -f compose.yaml kill -s HUP kaal" in calls
    assert "made-up data is back" in done.stdout

    done, calls = demo_folder.run("remove", KAAL_DEMO_PROJECT="kaal-test-made-up")
    assert done.returncode == 0, done.stderr
    assert "compose -p kaal-test-made-up --env-file kaal-test-made-up.env -f compose.yaml --profile https down --volumes --remove-orphans" in calls
    assert "image rm kaal-test-made-up:local" in calls
    assert not any("kaal:local" in call or "compose -p kaal " in call for call in calls)


def test_run_sh_is_plain_sh():
    text = (DEMO / "run.sh").read_text()
    assert text.startswith("#!/bin/sh\n") and os.access(DEMO / "run.sh", os.X_OK)
    assert subprocess.run(["sh", "-n", str(DEMO / "run.sh")], capture_output=True).returncode == 0
    for bashism in ("[[", "function ", "<<<", "echo -e", "pipefail"):
        assert bashism not in text, f"run.sh uses {bashism!r}, which plain sh does not have"
    assert not any(line.strip().startswith("local ") for line in text.splitlines())


# --- compose.yaml: the regular setup's hardening, with nothing kept on disk


def test_the_demo_reuses_the_regular_setup_and_keeps_nothing_on_disk():
    text = (DEMO / "compose.yaml").read_text()
    assert text.count("file: ../docker/compose.yaml") == 2
    assert "env_file: !reset []" in text and "volumes: !override" in text
    assert "kaal-data" not in text and "kaal-key" not in text and "backup" not in text.split("services:")[1]
    assert re.search(r"- /srv/kaal:size=\$\{KAAL_DEMO_STORAGE:-256m\},uid=999,gid=999,mode=0700", text)
    assert re.search(r"- /etc/kaal:size=1m,uid=999,gid=999,mode=0700", text)
    assert 'KAAL_RECEIPT_AI: "off"' in text and 'KAAL_SIGNUPS_PER_HOUR: "0"' in text


@pytest.mark.skipif(not shutil.which("docker"), reason="needs docker")
def test_compose_resolves_the_demo_with_the_regular_hardening(tmp_path):
    settings = tmp_path / "made-up.env"
    settings.write_text("KAAL_IMAGE=kaal-test-config:local\nKAAL_PUBLIC_URL=http://localhost:8797\nKAAL_LISTEN=127.0.0.1:8797\n"
                        "KAAL_COOKIE_SECURE=0\nKAAL_DEMO_PASSWORD=made-up-pass-1\nKAAL_DEMO_ADMIN_PASSWORD=made-up-pass-2\n")
    done = subprocess.run(["docker", "compose", "-p", "kaal-test-config", "--env-file", str(settings), "-f", str(DEMO / "compose.yaml"),
                           "config", "--format", "json"], capture_output=True, text=True, timeout=60)
    if done.returncode != 0 and "unknown" in done.stderr.lower():
        pytest.skip("this Docker has no compose plugin")
    assert done.returncode == 0, done.stderr
    resolved = json.loads(done.stdout)
    assert set(resolved["services"]) == {"kaal"}, "no HTTPS proxy and no backups unless asked for"
    kaal_service = resolved["services"]["kaal"]
    assert kaal_service["image"] == "kaal-test-config:local" and kaal_service["read_only"] is True
    assert kaal_service["cap_drop"] == ["ALL"] and kaal_service["security_opt"] == ["no-new-privileges:true"]
    assert [(mount["type"], mount["target"]) for mount in kaal_service["volumes"]] == [("bind", "/opt/kaal-demo/demo.py")]
    assert all(mount["read_only"] for mount in kaal_service["volumes"])
    assert {entry.split(":")[0] for entry in kaal_service["tmpfs"]} == {"/tmp", "/srv/kaal", "/etc/kaal"}
    assert [(port["host_ip"], port["published"]) for port in kaal_service["ports"]] == [("127.0.0.1", "8797")]
    environment = kaal_service["environment"]
    assert environment["KAAL_RECEIPT_AI"] == "off" and environment["KAAL_SIGNUPS_PER_HOUR"] == "0"
    assert environment["KAAL_MAX_UPLOAD_BYTES"] == "10485760" and environment["KAAL_COOKIE_SECURE"] == "0"
    assert kaal_service["command"] == ["python", "/opt/kaal-demo/demo.py", "serve"]
    assert kaal_service["healthcheck"]["test"] == ["CMD", "python", "/opt/kaal-demo/demo.py", "health"]
    assert int(kaal_service["mem_limit"]) == 1024 ** 3 and kaal_service["pids_limit"] == 256
    assert "kaal.env" not in json.dumps(kaal_service), "a regular installation's own settings never apply"


# --- the real demo, built and run with Docker (KAAL_DEMO_DOCKER=1)

PROJECT = "kaal-test-demo"
REVIEW = "made-up-review-pass-1"
# What the image is built from, copied so that the demo's settings file lands in a throwaway folder.
BUILD_INPUTS = ["Dockerfile", ".dockerignore", "pyproject.toml", "README.md", "LICENSE", "NOTICE", "kaal", "deploy/docker", "deploy/demo"]


def _free_port():
    while True:
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        if port not in {8765, 8791, 8792}:
            return port


def _project_things(project):
    """The containers, volumes and networks Docker holds for one compose project."""
    label = f"label=com.docker.compose.project={project}"
    found = []
    for kind in (["ps", "-a"], ["volume", "ls"], ["network", "ls"]):
        listed = subprocess.run(["docker", *kind, "-q", "--filter", label], capture_output=True, text=True, timeout=60)
        found += [f"{kind[0]} {line}" for line in listed.stdout.split()]
    return sorted(found)


@pytest.mark.skipif(os.environ.get("KAAL_DEMO_DOCKER") != "1", reason="builds and runs the demo with Docker; set KAAL_DEMO_DOCKER=1")
def test_the_real_demo_shows_the_made_up_data_and_puts_it_back(tmp_path):
    copy = tmp_path / "kaal"
    for name in BUILD_INPUTS:
        if (ROOT / name).is_dir():
            shutil.copytree(ROOT / name, copy / name, ignore=shutil.ignore_patterns("__pycache__", "*.env", ".env"))
        else:
            (copy / name).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(ROOT / name, copy / name)
    run_sh = copy / "deploy" / "demo" / "run.sh"
    port = _free_port()
    env = {**os.environ, "KAAL_DEMO_PROJECT": PROJECT, "KAAL_DEMO_PASSWORD": REVIEW}

    def run_demo(*args, timeout=600):
        done = subprocess.run(["sh", str(run_sh), *args], env=env, capture_output=True, text=True, timeout=timeout)
        assert done.returncode == 0, done.stdout + done.stderr
        return done.stdout

    send = demo.http_sender(f"http://127.0.0.1:{port}")

    def sign_in(login, password):
        person = demo.Kaal(send)
        person.call("POST", "/v1/sign-in", {"login": login, "password": password})
        return person

    def fetch(person, path):
        status, _cookies, body = send("GET", path, {"Cookie": f"kaal_session={person.session}", "X-Kaal-Request": "1"}, b"")
        return status, body

    seeded_notes = {note.title for note in demo.notes(TODAY) if not note.trash} | {demo.NOTICE_TITLE}

    def the_seed_is_there(person):
        listing = person.call("GET", "/v1/library/notes")
        assert listing[0]["title"] == demo.NOTICE_TITLE and {note["title"] for note in listing} == seeded_notes
        waiting = person.call("GET", "/v1/medical/receipts?inbox=true")
        reviewed = person.call("GET", "/v1/medical/receipts?status=reviewed")
        assert len(waiting) == 4 and {receipt["receipt"]["status"] for receipt in waiting} == {"needs_review"}
        assert waiting[0]["title"] == "Example Family Clinic: office visit"
        assert waiting[0]["receipt"]["fields"].get("amount") == "45.00", "the receipt the iPhone app's screen tests check"
        assert len(reviewed) == 4 and sum(bool(receipt["receipt"].get("reimbursed_at")) for receipt in reviewed) == 1
        assert len(person.call("GET", "/v1/medical/receipts?trash=true")) == 1
        assert len(person.call("GET", "/v1/library/notes?trash=true")) == 1
        assert person.call("GET", "/v1/medical/people")["people"] == demo.FAMILY
        return listing, waiting

    regular = _project_things("kaal")
    try:
        started = run_demo("--local", "--port", str(port), timeout=1800)
        assert f"http://localhost:{port}" in started and REVIEW not in started
        admin_password = read_settings(copy / "deploy" / "demo" / f"{PROJECT}.env")["KAAL_DEMO_ADMIN_PASSWORD"]

        # The reviewer, signed in with a password, then as the iPhone app signs in.
        jane = sign_in("reviewer", REVIEW)
        account = jane.call("GET", "/v1/account")
        assert account["account"] == {"login": "reviewer", "name": demo.REVIEWER_NAME, "admin": False}
        assert account["vault"]["name"] == demo.VAULT_NAME and len(account["vaults"]) == 2
        verifier = secrets.token_urlsafe(48)
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
        code = jane.call("POST", "/v1/account/app-sign-ins", {"challenge": challenge})["code"]
        phone = demo.Kaal(send)
        phone.session = phone.call("POST", "/v1/app-sign-ins", {"code": code, "verifier": verifier, "device": "Kaal app on iPhone"})["session"]
        listing, waiting = the_seed_is_there(phone)
        garden = phone.call("GET", f"/v1/library/notes/{by_title(listing)['Made-up garden plan']['id']}")["note"]
        status, picture = fetch(phone, f"/v1/notes/{garden['id']}/attachments/{garden['attachments'][0]['id']}/view")
        assert status == 200 and picture.startswith(b"\x89PNG")
        clinic = waiting[0]
        status, page = fetch(phone, f"/v1/medical/receipts/{clinic['id']}/attachments/{clinic['attachments'][0]['id']}/preview")
        assert status == 200 and len(page) > 1000, "a picture of the receipt to check the values against"

        # The administrator is someone else, and nobody can make an account.
        owner = sign_in("owner", admin_password)
        assert owner.call("GET", "/v1/account")["account"]["admin"] is True
        assert demo.Kaal(send).call("GET", "/v1/sign-in/options")["signup"] == "invitation"
        with pytest.raises(demo.DemoError, match="403"):
            demo.Kaal(send).call("POST", "/v1/sign-up", {"login": "stranger", "password": "made-up-stranger-1"})
        # Over the demo's 10 MiB, though a regular installation takes up to 100. (A body
        # over 11 MiB is refused by its stated length before it is even read.)
        with pytest.raises(demo.DemoError, match="413"):
            jane.call("POST", f"/v1/library/notes/{garden['id']}/attachments", files=[("file", "big.bin", "application/octet-stream", b"0" * (10 * 1024 * 1024 + 500_000))])

        # What a visitor changes, the reset puts back, and the password App Review was given still works.
        jane.call("POST", "/v1/library/notes", {"title": "Made-up note the reset removes", "body": "made up", "tags": []})
        jane.call("POST", f"/v1/medical/receipts/{clinic['id']}/trash")
        jane.call("POST", "/v1/account/password", {"current": REVIEW, "new": "someone-else-chose-this"})
        assert f"http://localhost:{port}" in run_demo("status")
        assert "made-up data is back" in run_demo("reset")
        with pytest.raises(demo.DemoError, match="401"):
            sign_in("reviewer", "someone-else-chose-this")
        with pytest.raises(demo.DemoError, match="401"):
            phone.call("GET", "/v1/library/notes")
        the_seed_is_there(sign_in("reviewer", REVIEW))

        # Stopped and started again, it comes back the same, with the same passwords.
        run_demo("stop", timeout=120)
        run_demo("start", timeout=600)
        the_seed_is_there(sign_in("reviewer", REVIEW))
        sign_in("owner", admin_password)
        logs = subprocess.run(["sh", str(run_sh), "logs"], env=env, capture_output=True, text=True, timeout=120)
        assert "ready: 9 notes and 9 receipts" in logs.stdout + logs.stderr
        assert REVIEW not in logs.stdout + logs.stderr and admin_password not in logs.stdout + logs.stderr
    finally:
        subprocess.run(["sh", str(run_sh), "remove"], env=env, capture_output=True, text=True, timeout=300)
    assert _project_things(PROJECT) == [], "nothing of the demo is left: no containers, volumes or networks"
    assert subprocess.run(["docker", "image", "inspect", f"{PROJECT}:local"], capture_output=True).returncode != 0
    assert _project_things("kaal") == regular, "a regular installation on this machine is left alone"
