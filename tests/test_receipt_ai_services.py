"""Reading receipts through a model service: Anthropic's API, an OpenAI-compatible service, or Ollama.

No test reaches a real service. A stand-in HTTP server on this machine answers
the way each service does and records what it was sent. The keys are made up.
"""
import base64
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import pytest

import kaal.core as kaal
from kaal import receipt_ai

ANSWER = {"patient_name": "Jane Madeup", "provider": "Made-up Clinic", "insurer": "", "service_date": "03/04/2026",
          "paid_date": "03/04/2026", "amount": "$40.00", "amount_due": ""}
MADE_UP_KEY = "made-up-key-0123456789"
PICTURE = ("image/png", b"\x89PNG\r\n\x1a\nmade-up picture")


class StandIn:
    """Answers like a model service. ``reply`` maps a path to (status, body, extra headers)."""

    def __init__(self):
        self.seen = []
        self.reply = {}
        self.delay = 0.0
        stand_in = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length)
                stand_in.seen.append(SimpleNamespace(path=self.path, headers=dict(self.headers), json=json.loads(body or b"null")))
                if stand_in.delay:
                    time.sleep(stand_in.delay)
                status, payload, headers = stand_in.reply.get(self.path, (404, {"error": "no such path"}, {}))
                data = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                for name, value in headers.items():
                    self.send_header(name, value)
                self.end_headers()
                self.wfile.write(data)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture()
def service():
    stand_in = StandIn()
    yield stand_in
    stand_in.close()


@pytest.fixture(autouse=True)
def fresh_count(monkeypatch):
    monkeypatch.setattr(receipt_ai, "_usage", {"day": "", "count": 0})


@pytest.fixture()
def key_file(tmp_path):
    path = tmp_path / "service.key"
    path.write_text(MADE_UP_KEY + "\n")
    path.chmod(0o600)
    return str(path)


def ollama_reply(answer=ANSWER):
    return (200, {"model": "m", "message": {"role": "assistant", "content": json.dumps(answer)}, "done": True}, {})


def openai_reply(answer=ANSWER, **message):
    return (200, {"choices": [{"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": json.dumps(answer), **message}}]}, {})


def anthropic_reply(answer=ANSWER, stop_reason="end_turn"):
    return (200, {"id": "msg_madeup", "type": "message", "role": "assistant", "model": "claude-haiku-5-5",
                  "content": [{"type": "text", "text": json.dumps(answer)}], "stop_reason": stop_reason, "stop_sequence": None,
                  "usage": {"input_tokens": 10, "output_tokens": 10}}, {})


# --- settings

def test_each_service_has_safe_defaults():
    anthropic = receipt_ai.settings_from_env({"KAAL_RECEIPT_AI": "anthropic"})
    assert (anthropic.model, anthropic.url, anthropic.daily_limit, anthropic.effort) == ("claude-haiku-5-5", "", 300, "low")
    ollama = receipt_ai.settings_from_env({"KAAL_RECEIPT_AI": "ollama"})
    assert (ollama.model, ollama.url, ollama.daily_limit) == ("qwen2.5vl:3b", "http://127.0.0.1:11434", 0)
    openai = receipt_ai.settings_from_env({"KAAL_RECEIPT_AI": "openai", "KAAL_RECEIPT_AI_MODEL": "made-up-model"})
    assert (openai.url, openai.daily_limit) == ("https://api.openai.com/v1", 300)
    assert receipt_ai.settings_from_env({"KAAL_RECEIPT_AI": "openai", "KAAL_RECEIPT_AI_MODEL": "m", "KAAL_RECEIPT_AI_DAILY_LIMIT": "0"}).daily_limit == 0
    assert receipt_ai.settings_from_env({"KAAL_RECEIPT_AI": "anthropic", "KAAL_RECEIPT_AI_EFFORT": ""}).effort == ""


@pytest.mark.parametrize("environment", [
    {"KAAL_RECEIPT_AI": "openai"},                                                          # no model named
    {"KAAL_RECEIPT_AI": "ollama", "KAAL_RECEIPT_AI_DAILY_LIMIT": "-1"},
    {"KAAL_RECEIPT_AI": "ollama", "KAAL_RECEIPT_AI_DAILY_LIMIT": "lots"},
    {"KAAL_RECEIPT_AI": "anthropic", "KAAL_RECEIPT_AI_EFFORT": "extreme"},
    {"KAAL_RECEIPT_AI": "ollama", "KAAL_RECEIPT_AI_URL": "http://models.example.com:11434"},  # plain http to the internet
    {"KAAL_RECEIPT_AI": "ollama", "KAAL_RECEIPT_AI_URL": "https://user:pass@models.example.com"},
    {"KAAL_RECEIPT_AI": "ollama", "KAAL_RECEIPT_AI_URL": "https://models.example.com/?key=x"},
    {"KAAL_RECEIPT_AI": "ollama", "KAAL_RECEIPT_AI_URL": "ftp://models.example.com"},
])
def test_poor_settings_are_refused(environment):
    with pytest.raises(receipt_ai.ReceiptAIError) as refused:
        receipt_ai.settings_from_env(environment)
    assert refused.value.kind == "misconfigured"


@pytest.mark.parametrize("url", ["http://localhost:11434", "http://127.0.0.1:11434", "http://192.168.1.20:11434", "http://10.0.0.5",
                                 "http://ollama:11434", "http://host.docker.internal:11434", "http://box.local:11434", "https://models.example.com/v1/"])
def test_plain_http_is_allowed_only_inside_private_networks(url):
    settings = receipt_ai.settings_from_env({"KAAL_RECEIPT_AI": "ollama", "KAAL_RECEIPT_AI_URL": url})
    assert not settings.url.endswith("/")


def test_a_key_file_others_can_read_is_refused_and_no_error_shows_the_key(tmp_path, key_file):
    loose = tmp_path / "loose.key"
    loose.write_text(MADE_UP_KEY)
    loose.chmod(0o644)
    settings = receipt_ai.Settings(backend="anthropic", model="m", key_file=str(loose))
    with pytest.raises(receipt_ai.ReceiptAIError) as refused:
        receipt_ai.read_key(settings)
    assert refused.value.kind == "misconfigured" and MADE_UP_KEY not in str(refused.value)
    assert receipt_ai.read_key(receipt_ai.Settings(key_file=key_file)) == MADE_UP_KEY


def test_status_says_where_the_key_comes_from_but_never_shows_it(key_file, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    told = receipt_ai.describe(receipt_ai.Settings(backend="anthropic", model="claude-haiku-5-5", key_file=key_file, daily_limit=300))
    assert told["key"] == "file" and told["usable"] is True and told["daily_limit"] == 300
    assert MADE_UP_KEY not in json.dumps(told)
    missing = receipt_ai.describe(receipt_ai.Settings(backend="anthropic", model="claude-haiku-5-5"))
    assert missing["usable"] is False and "no API key" in missing["problem"]
    monkeypatch.setenv("ANTHROPIC_API_KEY", MADE_UP_KEY)
    from_environment = receipt_ai.describe(receipt_ai.Settings(backend="anthropic", model="claude-haiku-5-5"))
    assert from_environment["key"] == "environment" and MADE_UP_KEY not in json.dumps(from_environment)


# --- Ollama

def test_ollama_is_sent_the_schema_the_prompt_and_the_pictures(service):
    service.reply["/api/chat"] = ollama_reply()
    settings = receipt_ai.Settings(backend="ollama", model="qwen2.5vl:3b", url=service.url, timeout=10)
    assert receipt_ai.extract("Amount paid $40.00", [PICTURE], settings)["amount"] == "$40.00"
    sent = service.seen[0].json
    assert sent["model"] == "qwen2.5vl:3b" and sent["stream"] is False and sent["format"] == receipt_ai.SCHEMA
    assert sent["options"] == {"temperature": 0}
    system, user = sent["messages"]
    assert system == {"role": "system", "content": receipt_ai.PROMPT}
    assert "Amount paid $40.00" in user["content"]
    assert user["images"] == [base64.b64encode(PICTURE[1]).decode()]


# --- OpenAI-compatible

def test_an_openai_compatible_service_gets_a_strict_schema_and_the_key(service, key_file):
    service.reply["/v1/chat/completions"] = openai_reply()
    settings = receipt_ai.Settings(backend="openai", model="made-up-model", url=service.url + "/v1", key_file=key_file, timeout=10)
    assert receipt_ai.extract("text", [PICTURE], settings)["provider"] == "Made-up Clinic"
    seen = service.seen[0]
    assert seen.headers["Authorization"] == f"Bearer {MADE_UP_KEY}"
    assert seen.json["response_format"]["json_schema"]["strict"] is True
    assert seen.json["response_format"]["json_schema"]["schema"] == receipt_ai.SCHEMA
    picture, words = seen.json["messages"][1]["content"]
    assert picture["image_url"]["url"].startswith("data:image/png;base64,") and words["type"] == "text"


def test_a_service_on_this_machine_needs_no_key(service, monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    service.reply["/v1/chat/completions"] = openai_reply()
    settings = receipt_ai.Settings(backend="openai", model="m", url=service.url + "/v1", timeout=10)
    receipt_ai.extract("text", [], settings)
    assert "Authorization" not in service.seen[0].headers


@pytest.mark.parametrize("reply, kind", [
    (openai_reply(refusal="I can't help with that."), "refused"),
    ((200, {"choices": [{"finish_reason": "length", "message": {"content": "{\"amou"}}]}, {}), "bad_answer"),
    ((200, {"choices": []}, {}), "bad_answer"),
    ((200, {"choices": [{"message": {"content": "not json"}}]}, {}), "bad_answer"),
    ((401, {"error": {"message": "bad key"}}, {}), "not_signed_in"),
    ((429, {"error": {"message": "slow down"}}, {}), "rate_limited"),
    ((500, {"error": {}}, {}), "unavailable"),
    ((302, {}, {"Location": "https://elsewhere.example.com/"}), "failed"),
    ((200, b"x" * (receipt_ai.MAX_ANSWER_BYTES + 10), {}), "bad_answer"),
])
def test_an_openai_compatible_service_failing(service, key_file, reply, kind):
    service.reply["/v1/chat/completions"] = reply
    settings = receipt_ai.Settings(backend="openai", model="m", url=service.url + "/v1", key_file=key_file, timeout=10)
    with pytest.raises(receipt_ai.ReceiptAIError) as failed:
        receipt_ai.extract("text", [], settings)
    assert failed.value.kind == kind
    assert MADE_UP_KEY not in str(failed.value)
    assert len(service.seen) == 1           # a redirect is not followed


def test_a_slow_service_times_out(service):
    service.reply["/api/chat"] = ollama_reply()
    service.delay = 3
    with pytest.raises(receipt_ai.ReceiptAIError) as failed:
        receipt_ai.extract("text", [], receipt_ai.Settings(backend="ollama", model="m", url=service.url, timeout=1))
    assert failed.value.kind == "timeout"


def test_a_service_that_is_not_there(service):
    url = service.url
    service.close()
    with pytest.raises(receipt_ai.ReceiptAIError) as failed:
        receipt_ai.extract("text", [], receipt_ai.Settings(backend="ollama", model="m", url=url, timeout=5))
    assert failed.value.kind == "unavailable"


# --- Anthropic, through its official package

anthropic = pytest.importorskip("anthropic")


def test_claude_is_asked_through_the_official_package_with_a_json_schema(service, key_file):
    service.reply["/v1/messages"] = anthropic_reply()
    settings = receipt_ai.Settings(backend="anthropic", model="claude-haiku-5-5", url=service.url, key_file=key_file, effort="low", timeout=10)
    assert receipt_ai.extract("Amount paid $40.00", [PICTURE], settings)["patient_name"] == "Jane Madeup"
    seen = service.seen[0]
    # The package decides how the key travels (an x-api-key or an Authorization header); it must arrive either way.
    assert seen.headers.get("x-api-key") == MADE_UP_KEY or seen.headers.get("Authorization") == f"Bearer {MADE_UP_KEY}"
    sent = seen.json
    assert sent["model"] == "claude-haiku-5-5" and sent["system"] == receipt_ai.PROMPT
    assert sent["output_config"] == {"format": {"type": "json_schema", "schema": receipt_ai.SCHEMA}, "effort": "low"}
    picture, words = sent["messages"][0]["content"]
    assert picture["source"] == {"type": "base64", "media_type": "image/png", "data": base64.b64encode(PICTURE[1]).decode()}
    assert words["type"] == "text" and "Amount paid $40.00" in words["text"]
    assert not any(name in sent for name in ("temperature", "top_p", "top_k", "thinking"))


def test_without_an_effort_setting_none_is_sent(service, key_file):
    service.reply["/v1/messages"] = anthropic_reply()
    receipt_ai.extract("text", [], receipt_ai.Settings(backend="anthropic", model="claude-older", url=service.url, key_file=key_file, timeout=10))
    assert service.seen[0].json["output_config"] == {"format": {"type": "json_schema", "schema": receipt_ai.SCHEMA}}


@pytest.mark.parametrize("reply, kind", [
    (anthropic_reply(stop_reason="refusal"), "refused"),
    (anthropic_reply(stop_reason="max_tokens"), "bad_answer"),
    ((401, {"type": "error", "error": {"type": "authentication_error", "message": "invalid x-api-key"}}, {}), "not_signed_in"),
    ((404, {"type": "error", "error": {"type": "not_found_error", "message": "model: made-up"}}, {}), "unavailable"),
    ((400, {"type": "error", "error": {"type": "invalid_request_error", "message": "bad"}}, {}), "failed"),
])
def test_claude_failing(service, key_file, reply, kind):
    service.reply["/v1/messages"] = reply
    settings = receipt_ai.Settings(backend="anthropic", model="claude-haiku-5-5", url=service.url, key_file=key_file, timeout=10)
    with pytest.raises(receipt_ai.ReceiptAIError) as failed:
        receipt_ai.extract("text", [], settings)
    assert failed.value.kind == kind and MADE_UP_KEY not in str(failed.value)


def test_claude_with_no_key_is_not_asked(service, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with pytest.raises(receipt_ai.ReceiptAIError) as failed:
        receipt_ai.extract("text", [], receipt_ai.Settings(backend="anthropic", model="claude-haiku-5-5", url=service.url, timeout=10))
    assert failed.value.kind == "not_signed_in" and service.seen == []


# --- the daily limit

def test_the_daily_limit_stops_calls_until_the_next_day(service, monkeypatch):
    service.reply["/api/chat"] = ollama_reply()
    settings = receipt_ai.Settings(backend="ollama", model="m", url=service.url, timeout=10, daily_limit=2)
    receipt_ai.extract("one", [], settings)
    receipt_ai.extract("two", [], settings)
    with pytest.raises(receipt_ai.ReceiptAIError) as refused:
        receipt_ai.extract("three", [], settings)
    assert refused.value.kind == "daily_limit" and len(service.seen) == 2
    assert receipt_ai.used_today() == 2
    monkeypatch.setattr(receipt_ai, "_today", lambda: "2999-01-01")
    receipt_ai.extract("next day", [], settings)
    assert len(service.seen) == 3


# --- through capture, as the server does it

@pytest.fixture()
def vault(tmp_path):
    saved = kaal.VAULT
    kaal.configure_vault(tmp_path / "vault")
    kaal.call_captured(kaal.init_vault, SimpleNamespace())
    yield tmp_path
    kaal.configure_vault(saved)


def test_a_captured_receipt_is_read_by_ollama_and_what_it_says_is_grounded(vault, service, monkeypatch):
    service.reply["/api/chat"] = ollama_reply({**ANSWER, "insurer": "Made-up Insurer Nowhere On The Receipt"})
    monkeypatch.setenv("KAAL_RECEIPT_AI", "ollama")
    monkeypatch.setenv("KAAL_RECEIPT_AI_URL", service.url)
    source = vault / "receipt.txt"
    source.write_text("MADE-UP CLINIC\nPatient: Jane Madeup\nDate of service: 03/04/2026\nAmount paid: $40.00\n")
    made = kaal.call_captured(kaal.medical_capture, SimpleNamespace(file=str(source), title="", source_url="", source_title="", captured_at=""))[0]
    receipt = next(note for note in kaal.load_index()["notes"] if note["id"] == made["id"])["receipt"]
    assert receipt["ai"]["backend"] == "ollama"
    assert receipt["fields"]["amount"] == "40.00" and receipt["field_sources"]["amount"] in {"ai-grounded", "labelled-text"}
    assert receipt["field_sources"].get("insurer") == "ai-unverified"


def test_a_failing_service_leaves_the_receipt_to_the_label_rules(vault, service, monkeypatch):
    service.reply["/api/chat"] = (500, {"error": "made-up failure"}, {})
    monkeypatch.setenv("KAAL_RECEIPT_AI", "ollama")
    monkeypatch.setenv("KAAL_RECEIPT_AI_URL", service.url)
    source = vault / "receipt.txt"
    source.write_text("Patient: Jane Madeup\nAmount paid: $40.00\n")
    made = kaal.call_captured(kaal.medical_capture, SimpleNamespace(file=str(source), title="", source_url="", source_title="", captured_at=""))[0]
    receipt = next(note for note in kaal.load_index()["notes"] if note["id"] == made["id"])["receipt"]
    assert receipt["ai"]["error"] == "unavailable"
