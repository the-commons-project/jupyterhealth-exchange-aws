"""REDCap Data Entry Trigger -> JupyterHealth Exchange invitation link -> back into REDCap.

REDCap calls this function's URL after a record is saved (a Data Entry Trigger). For that record
the function

  1. checks the shared trigger token in the URL (`?token=...`),
  2. reads the patient's e-mail (from the request when it carries one, else from REDCap's API)
     and stops if the record already has an invitation link,
  3. signs in to JHE with the integration account's API key (client-credentials grant),
  4. finds or creates the patient in the configured organization, and creates an invitation to
     the configured client (USCDI Patient Access), which returns the link,
  5. writes the link into the configured REDCap field.

Python standard library and boto3 (in the Lambda runtime) only, so there is nothing to package.
Nothing secret or patient-identifying is logged: records are named by their REDCap id only.

Secret `jhe/<env>/redcap` (JSON): JHE_API_KEY, REDCAP_API_TOKEN, TRIGGER_TOKEN.
"""

from __future__ import annotations

import base64
import hmac
import json
import logging
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request

logger = logging.getLogger()
logger.setLevel(logging.INFO)

HTTP_TIMEOUT = 10
EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
MAX_PAGES = 5

SECRET_TTL = 300  # seconds a read of the secret is kept: a changed value is used within five minutes

_secret_cache: dict[str, str] | None = None
_secret_loaded_at = 0.0


class Reply(Exception):
    """Stop with this HTTP status and message."""

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


def respond(status: int, body: dict) -> dict:
    return {
        "statusCode": status,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(body),
    }


def settings() -> dict:
    env = os.environ
    return {
        "secret_id": env["SECRET_ID"],
        "jhe_url": env["JHE_BASE_URL"].rstrip("/"),
        "redcap_url": env["REDCAP_API_URL"],
        "email_field": env["EMAIL_FIELD"],
        "link_field": env["LINK_FIELD"],
        "record_id_field": env["RECORD_ID_FIELD"],
        "organization_id": int(env["ORGANIZATION_ID"]),
        "client_name": env["JHE_CLIENT_NAME"],
    }


def load_secret(secret_id: str) -> dict[str, str]:
    """The secret's fields, re-read when the last read is older than SECRET_TTL."""
    global _secret_cache, _secret_loaded_at
    if _secret_cache is None or time.monotonic() - _secret_loaded_at > SECRET_TTL:
        import boto3

        value = boto3.client("secretsmanager").get_secret_value(SecretId=secret_id)["SecretString"]
        _secret_cache = json.loads(value)
        _secret_loaded_at = time.monotonic()
    return _secret_cache


# --- the request ------------------------------------------------------------------------------


def parse_request(event: dict) -> dict[str, str]:
    """The query string and body of a function-URL event as one flat dict (the body wins).
    REDCap posts form fields; a JSON body works too."""
    data: dict[str, str] = {k: v for k, v in (event.get("queryStringParameters") or {}).items()}
    body = event.get("body") or ""
    if event.get("isBase64Encoded"):
        body = base64.b64decode(body).decode("utf-8", "replace")
    if body.strip():
        if body.lstrip().startswith("{"):
            try:
                parsed = json.loads(body)
            except ValueError:
                raise Reply(400, "the body is not valid JSON") from None
            if not isinstance(parsed, dict):
                raise Reply(400, "the body must be a JSON object")
            data.update({str(k): str(v) for k, v in parsed.items() if v is not None})
        else:
            data.update({k: v[-1] for k, v in urllib.parse.parse_qs(body, keep_blank_values=True).items()})
    return data


def check_trigger_token(given: str, secret: dict[str, str]) -> None:
    expected = secret.get("TRIGGER_TOKEN") or ""
    if not expected:
        logger.error("TRIGGER_TOKEN is empty in the secret: refusing every request")
        raise Reply(503, "the integration is not configured")
    if not hmac.compare_digest(given.encode(), expected.encode()):
        raise Reply(403, "forbidden")


# --- HTTP -------------------------------------------------------------------------------------


def http(method: str, url: str, *, headers=None, form=None, payload=None):
    """One request; the decoded JSON answer. A non-2xx answer raises HttpFailure."""
    headers = dict(headers or {})
    data = None
    if form is not None:
        data = urllib.parse.urlencode(form).encode()
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    elif payload is not None:
        data = json.dumps(payload).encode()
        headers["Content-Type"] = "application/json"
    headers.setdefault("Accept", "application/json")
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT) as response:  # noqa: S310 - https only, set by config
            raw = response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        raise HttpFailure(method, url, e.code, e.read().decode("utf-8", "replace")[:300]) from None
    except urllib.error.URLError as e:
        raise HttpFailure(method, url, 0, str(e.reason)) from None
    return json.loads(raw) if raw.strip() else None


class HttpFailure(Exception):
    def __init__(self, method: str, url: str, status: int, detail: str):
        super().__init__(f"{method} {url.split('?')[0]} -> {status}: {detail}")
        self.status = status


# --- REDCap -----------------------------------------------------------------------------------


def redcap(cfg: dict, secret: dict[str, str], form: dict):
    return http(
        "POST", cfg["redcap_url"], form={"token": secret["REDCAP_API_TOKEN"], "format": "json", **form}
    )


def read_record(cfg: dict, secret: dict[str, str], record: str, event: str | None) -> dict:
    """The record's e-mail and link fields (one row; the event's row in a longitudinal project)."""
    rows = redcap(
        cfg,
        secret,
        {
            "content": "record",
            "type": "flat",
            "rawOrLabel": "raw",
            "records[0]": record,
            "fields[0]": cfg["record_id_field"],
            "fields[1]": cfg["email_field"],
            "fields[2]": cfg["link_field"],
        },
    )
    if not isinstance(rows, list) or not rows:
        raise Reply(404, "the record was not found in REDCap")
    if event:
        in_event = [r for r in rows if r.get("redcap_event_name") == event]
        rows = in_event or rows
    return next((r for r in rows if r.get(cfg["email_field"])), rows[0])


def write_link(cfg: dict, secret: dict[str, str], record: str, event: str | None, link: str) -> None:
    row = {cfg["record_id_field"]: record, cfg["link_field"]: link}
    if event:
        row["redcap_event_name"] = event
    redcap(
        cfg,
        secret,
        {
            "content": "record",
            "type": "flat",
            "overwriteBehavior": "normal",
            "forceAutoNumber": "false",
            "returnContent": "count",
            "data": json.dumps([row]),
        },
    )


# --- JHE --------------------------------------------------------------------------------------


class Jhe:
    def __init__(self, base_url: str, api_key: str):
        self.base = base_url
        self.headers = {"Authorization": f"Bearer {self._token(api_key)}"}

    def _token(self, api_key: str) -> str:
        answer = http(
            "POST",
            f"{self.base}/o/token/",
            headers={"Authorization": f"Basic {api_key}"},
            form={"grant_type": "client_credentials"},
        )
        return answer["access_token"]

    def get(self, path: str, **params):
        query = f"?{urllib.parse.urlencode(params)}" if params else ""
        return http("GET", f"{self.base}{path}{query}", headers=self.headers)

    def send(self, method: str, path: str, payload=None, **params):
        query = f"?{urllib.parse.urlencode(params)}" if params else ""
        return http(method, f"{self.base}{path}{query}", headers=self.headers, payload=payload)

    def client_id(self, name: str) -> int:
        """The JHE application id of the patient client with this name."""
        path: str | None = "/api/v1/clients"
        for _ in range(MAX_PAGES):
            page = self.get(path) if path else None
            if page is None:
                break
            items = page["results"] if isinstance(page, dict) else page
            for item in items:
                if item.get("name") == name:
                    return item["id"]
            nxt = page.get("next") if isinstance(page, dict) else None
            path = urllib.parse.urlsplit(nxt)._replace(scheme="", netloc="").geturl() if nxt else None
        raise Reply(502, f"JHE has no client named {name!r}")

    def patient_id(self, email: str, organization_id: int) -> int:
        """The patient with this e-mail (added to the organization if it was not in it), or a new one."""
        found = self.get("/api/v1/patients/global_lookup", email=email)
        if found:
            patient = found[0]
            member_of = {o.get("id") for o in patient.get("organizations") or []}
            if organization_id not in member_of:
                self.send(
                    "PATCH",
                    f"/api/v1/patients/{patient['id']}/global_add_organization",
                    organization_id=organization_id,
                )
            return patient["id"]
        created = self.send(
            "POST", "/api/v1/patients", {"organization_id": organization_id, "telecom_email": email}
        )
        return created["id"]

    def invitation_link(self, patient_id: int, client_id: int) -> str:
        answer = self.send("POST", "/api/v1/invitation", {"patient_id": patient_id, "client_id": client_id})
        link = answer.get("invitationLink") or answer.get("invitation_link")
        if not link:
            raise Reply(502, "JHE returned no invitation link")
        return link


# --- the handler ------------------------------------------------------------------------------


def process(request: dict[str, str], cfg: dict, secret: dict[str, str]) -> dict:
    record = (request.get("record") or "").strip()
    if not record:
        raise Reply(400, "no record in the request")
    event = (request.get("redcap_event_name") or "").strip() or None

    # REDCap's trigger does not carry field values, so the record is read for its e-mail (a request
    # that does carry one wins). The read also says whether the link is already there: the trigger
    # fires on every save, and a record must get one invitation, not one per save.
    row = read_record(cfg, secret, record, event)
    if row.get(cfg["link_field"]):
        return {"status": "skipped", "reason": "the record already has a link", "record": record}
    email = (request.get("email") or row.get(cfg["email_field"]) or "").strip()
    if not EMAIL.match(email):
        raise Reply(422, "the record has no valid e-mail address")

    jhe = Jhe(cfg["jhe_url"], secret["JHE_API_KEY"])
    patient_id = jhe.patient_id(email, cfg["organization_id"])
    link = jhe.invitation_link(patient_id, jhe.client_id(cfg["client_name"]))
    write_link(cfg, secret, record, event, link)
    logger.info("record %s: invitation link written for JHE patient %s", record, patient_id)
    return {"status": "ok", "record": record, "patient_id": patient_id}


def handler(event, context):
    try:
        cfg = settings()
        secret = load_secret(cfg["secret_id"])
        request = parse_request(event)
        check_trigger_token(request.pop("token", ""), secret)
        for name in ("JHE_API_KEY", "REDCAP_API_TOKEN"):
            if not secret.get(name):
                logger.error("%s is empty in the secret", name)
                raise Reply(503, "the integration is not configured")
        return respond(200, process(request, cfg, secret))
    except Reply as r:
        logger.warning("refused: %s %s", r.status, r.message)
        return respond(r.status, {"error": r.message})
    except HttpFailure as e:
        logger.error("upstream call failed: %s", e)
        return respond(502, {"error": "a call to JHE or REDCap failed; see the function's log"})
    except Exception:
        logger.exception("unexpected error")
        return respond(500, {"error": "internal error"})
