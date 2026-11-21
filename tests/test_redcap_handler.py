"""The REDCap trigger function: the request it accepts, the calls it makes to JHE and REDCap, and what it
refuses. The two HTTP services are replaced by a fake that records every call."""

from __future__ import annotations

import base64
import importlib.util
import json
import time
import urllib.parse
from pathlib import Path

import pytest

SPEC = importlib.util.spec_from_file_location(
    "redcap_det_handler", Path(__file__).resolve().parents[1] / "functions" / "redcap_det" / "handler.py"
)
handler = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(handler)

SECRET = {"JHE_API_KEY": "a2V5", "REDCAP_API_TOKEN": "rc-token", "TRIGGER_TOKEN": "trigger-secret"}
ENV = {
    "SECRET_ID": "jhe/test/redcap",
    "JHE_BASE_URL": "https://jhe.example.org",
    "REDCAP_API_URL": "https://redcap.example.org/api/",
    "EMAIL_FIELD": "email",
    "LINK_FIELD": "jhe_link",
    "RECORD_ID_FIELD": "record_id",
    "ORGANIZATION_ID": "1",
    "JHE_CLIENT_NAME": "USCDI Patient Access",
}
LINK = "https://jhe.example.org/clients/uscdi-patient-access/?code=abc"


class Fake:
    """JHE and REDCap, as `handler.http` sees them."""

    def __init__(self, *, row=None, rows=None, lookup=None, clients=None, fail=None):
        self.row = {"record_id": "7", "email": "pat@example.org", "jhe_link": ""} if row is None else row
        self.rows = rows  # replaces the one row REDCap answers with, e.g. [] for no such record
        self.lookup = [] if lookup is None else lookup
        self.clients = [{"id": 3, "name": "USCDI Patient Access"}] if clients is None else clients
        self.fail = fail
        self.calls: list[tuple[str, str, dict]] = []
        self.jhe_calls: list[str] = []

    def __call__(self, method, url, *, headers=None, form=None, payload=None):
        parsed = urllib.parse.urlsplit(url)
        path = parsed.path
        self.calls.append(
            (method, path + (f"?{parsed.query}" if parsed.query else ""), form or payload or {})
        )
        if parsed.netloc == "jhe.example.org":
            self.jhe_calls.append(path)
        if self.fail and self.fail in path:
            raise handler.HttpFailure(method, url, 500, "boom")
        if parsed.netloc == "redcap.example.org":
            if "data" in form:
                return {"count": 1}
            return [self.row] if self.rows is None else self.rows
        if path == "/o/token/":
            assert headers["Authorization"] == "Basic a2V5"
            return {"access_token": "tok", "token_type": "Bearer"}
        assert headers["Authorization"] == "Bearer tok"
        if path == "/api/v1/clients":
            return {"count": len(self.clients), "next": None, "results": self.clients}
        if path == "/api/v1/patients/global_lookup":
            return self.lookup
        if path == "/api/v1/patients" and method == "POST":
            return {"id": 11}
        if path.endswith("/global_add_organization"):
            return {"id": 5}
        if path == "/api/v1/invitation":
            return {"id": 1, "invitationLink": LINK}
        raise AssertionError(f"unexpected call {method} {url}")

    def written(self):
        return [json.loads(f["data"]) for _, _, f in self.calls if isinstance(f, dict) and "data" in f]


@pytest.fixture
def env(monkeypatch):
    for k, v in ENV.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setattr(handler, "_secret_cache", dict(SECRET))
    monkeypatch.setattr(handler, "_secret_loaded_at", time.monotonic())


def call(fake, monkeypatch, *, query=None, body=None, encode=False):
    monkeypatch.setattr(handler, "http", fake)
    event = {"queryStringParameters": {"token": "trigger-secret", **(query or {})}}
    if body is not None:
        event["body"] = base64.b64encode(body.encode()).decode() if encode else body
        event["isBase64Encoded"] = encode
    out = handler.handler(event, None)
    return out["statusCode"], json.loads(out["body"])


FORM = "project_id=1&record=7&instrument=enrol&redcap_event_name=&enrol_complete=2"


def test_creates_the_patient_and_writes_the_link_back(env, monkeypatch):
    fake = Fake()
    status, body = call(fake, monkeypatch, body=FORM)
    assert (status, body) == (200, {"status": "ok", "record": "7", "patient_id": 11})
    create = next(f for m, p, f in fake.calls if (m, p) == ("POST", "/api/v1/patients"))
    assert create == {"organization_id": 1, "telecom_email": "pat@example.org"}
    invite = next(f for m, p, f in fake.calls if p == "/api/v1/invitation")
    assert invite == {"patient_id": 11, "client_id": 3}
    assert fake.written() == [[{"record_id": "7", "jhe_link": LINK}]]


def test_an_existing_patient_in_the_organization_is_reused(env, monkeypatch):
    fake = Fake(lookup=[{"id": 5, "organizations": [{"id": 1}]}])
    status, body = call(fake, monkeypatch, body=FORM)
    assert status == 200 and body["patient_id"] == 5
    paths = [p for _, p, _ in fake.calls]
    assert "/api/v1/patients" not in paths and not any(p.endswith("global_add_organization") for p in paths)


def test_an_existing_patient_elsewhere_is_added_to_the_organization(env, monkeypatch):
    fake = Fake(lookup=[{"id": 5, "organizations": [{"id": 9}]}])
    status, _ = call(fake, monkeypatch, body=FORM)
    assert status == 200
    assert ("PATCH", "/api/v1/patients/5/global_add_organization?organization_id=1") in [
        (m, p) for m, p, _ in fake.calls
    ]


def test_a_record_that_has_a_link_is_left_alone(env, monkeypatch):
    fake = Fake(row={"record_id": "7", "email": "pat@example.org", "jhe_link": "https://already"})
    status, body = call(fake, monkeypatch, body=FORM)
    assert (status, body["status"]) == (200, "skipped")
    assert fake.jhe_calls == []  # REDCap was read; JHE was never called
    assert fake.written() == []


def test_an_email_in_the_request_wins_and_a_longitudinal_event_is_kept(env, monkeypatch):
    fake = Fake(row={"record_id": "7", "email": "old@example.org", "jhe_link": ""})
    body = json.dumps({"record": "7", "redcap_event_name": "enrol_arm_1", "email": "new@example.org"})
    status, _ = call(fake, monkeypatch, body=body, encode=True)
    assert status == 200
    create = next(f for m, p, f in fake.calls if (m, p) == ("POST", "/api/v1/patients"))
    assert create["telecom_email"] == "new@example.org"
    assert fake.written() == [[{"record_id": "7", "jhe_link": LINK, "redcap_event_name": "enrol_arm_1"}]]


@pytest.mark.parametrize("token", ["", "wrong", "trigger-secreT"])
def test_a_wrong_or_missing_token_is_refused_before_anything_is_called(env, monkeypatch, token):
    fake = Fake()
    monkeypatch.setattr(handler, "http", fake)
    event = {"queryStringParameters": {"token": token} if token else {}, "body": FORM}
    out = handler.handler(event, None)
    assert out["statusCode"] == 403
    assert fake.calls == []


def test_the_secret_is_re_read_after_its_ttl(env, monkeypatch):
    reads = []

    class Client:
        def get_secret_value(self, SecretId):
            reads.append(SecretId)
            return {"SecretString": json.dumps(SECRET)}

    import boto3

    monkeypatch.setattr(boto3, "client", lambda name: Client())
    handler.load_secret("jhe/test/redcap")  # cached by the fixture: no read
    assert reads == []
    monkeypatch.setattr(handler, "_secret_loaded_at", time.monotonic() - handler.SECRET_TTL - 1)
    handler.load_secret("jhe/test/redcap")
    assert reads == ["jhe/test/redcap"]


def test_an_empty_trigger_token_in_the_secret_refuses_everything(env, monkeypatch):
    monkeypatch.setattr(handler, "_secret_cache", {**SECRET, "TRIGGER_TOKEN": ""})
    fake = Fake()
    monkeypatch.setattr(handler, "http", fake)
    out = handler.handler({"queryStringParameters": {"token": ""}, "body": FORM}, None)
    assert out["statusCode"] == 503 and fake.calls == []


def test_unfilled_api_credentials_answer_503(env, monkeypatch):
    monkeypatch.setattr(handler, "_secret_cache", {**SECRET, "JHE_API_KEY": ""})
    fake = Fake()
    assert call(fake, monkeypatch, body=FORM)[0] == 503
    assert fake.calls == []


def test_no_record_is_a_400(env, monkeypatch):
    assert call(Fake(), monkeypatch, body="project_id=1")[0] == 400


@pytest.mark.parametrize("email", ["", "not-an-email", "a@b"])
def test_a_record_without_a_valid_email_is_a_422_and_creates_nothing(env, monkeypatch, email):
    fake = Fake(row={"record_id": "7", "email": email, "jhe_link": ""})
    assert call(fake, monkeypatch, body=FORM)[0] == 422
    assert fake.jhe_calls == []


def test_a_record_redcap_does_not_have_is_a_404(env, monkeypatch):
    fake = Fake(rows=[])
    assert call(fake, monkeypatch, body=FORM)[0] == 404
    assert fake.jhe_calls == []


def test_a_client_jhe_does_not_have_is_a_502(env, monkeypatch):
    fake = Fake(clients=[{"id": 4, "name": "Something else"}])
    assert call(fake, monkeypatch, body=FORM)[0] == 502
    assert fake.written() == []


def test_a_failed_jhe_call_is_a_502_without_the_detail_and_writes_nothing(env, monkeypatch):
    fake = Fake(fail="/api/v1/invitation")
    status, body = call(fake, monkeypatch, body=FORM)
    assert status == 502 and "boom" not in json.dumps(body)
    assert fake.written() == []


def test_bad_json_is_a_400(env, monkeypatch):
    assert call(Fake(), monkeypatch, body="{nope")[0] == 400
