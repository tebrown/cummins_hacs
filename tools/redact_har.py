#!/usr/bin/env python3
"""
Redact a .har capture from bootstrap_login.py --har before sharing it.

Strips anything secret (password, OAuth code/tokens, session cookies, auth
headers, Salesforce session IDs) while leaving the Aura login call shapes
(fwuid, context, controller descriptors, params structure) intact — that's
the part needed to reimplement the Salesforce login in pure `requests`, and
none of it is sensitive; it's served to any anonymous visitor of the login
page.

Two redaction passes run on every string found anywhere in the HAR (headers,
cookies, query params, POST bodies, response bodies, AND the raw request/
response URLs — Chromium's SAML/frontdoor redirect chain carries a live
Salesforce session ID and the OAuth code as bare query-string values on
those URLs, not as their own named field, so a key-only pass misses them):

  1. by field NAME  (password, refresh_token, cookie, sid, ...)
  2. by VALUE shape (`sid=...`, `code=...` in any URL; Salesforce's
     `00Dxxxxxxxxxxxxx!yyyy...` session-id shape wherever it appears)

USAGE:
    python tools/redact_har.py login_capture.har login_capture.redacted.har
"""

from __future__ import annotations

import json
import re
import sys
import urllib.parse

SENSITIVE_KEYS = re.compile(
    r"pass(word)?|pwd|refresh_token|access_token|id_token|^code$|client_secret"
    r"|authorization|cookie|set-cookie|sid|session[_-]?id"
    r"|fedid|username|^email$|samlresponse|relaystate",
    re.IGNORECASE,
)

# Raw HTML <input type="hidden" name="X" value="Y"> fields (e.g. the
# auto-submitting SAML form after login) aren't JSON or form-urlencoded, so
# neither _redact_json_value nor _redact_form_encoded ever sees them — a
# completed SAMLResponse assertion is exactly this shape and slipped through
# entirely on the first pass.
HTML_HIDDEN_INPUT = re.compile(
    r'(<input[^>]*type="hidden"[^>]*name="([^"]+)"[^>]*value=")([^"]*)(")',
    re.IGNORECASE,
)
# Deliberately NOT in SENSITIVE_KEYS: "location". A blanket redact would hide
# the *target* of every redirect, which is exactly the thing being
# reverse-engineered here — _redact_string's value-shape scan (sid=, code=,
# the Salesforce session-id shape) already strips what's actually secret out
# of a Location header's value while leaving the rest of the URL visible.

SENSITIVE_URL_PARAMS = re.compile(
    r"(?i)\b(sid|code|access_token|id_token|refresh_token|password|pwd)=([^&#\s\"'>]+)"
)
# Salesforce session IDs (e.g. from frontdoor.jsp) have a recognizable shape
# even when not attached to a `sid=` param name.
SALESFORCE_SID_SHAPE = re.compile(r"\b00D[0-9A-Za-z]{12,15}![0-9A-Za-z._-]{20,}")

REDACTED = "***REDACTED***"


MAX_UNQUOTE_LAYERS = 4


def _redact_html_hidden_inputs(text: str) -> str:
    def _sub(m: re.Match) -> str:
        prefix, name, _value, suffix = m.groups()
        if SENSITIVE_KEYS.search(name):
            return f"{prefix}{REDACTED}{suffix}"
        return m.group(0)

    return HTML_HIDDEN_INPUT.sub(_sub, text)


def _redact_string(value: str) -> str:
    """Redact secret-shaped substrings, re-checking after each decode layer.

    A single regex pass over the raw text misses a secret hiding behind
    percent-encoding — e.g. a Salesforce sid's "!" arriving as "%21", or
    doubly-encoded as "%2521" when it's itself a query value nested inside
    another URL (retURL=...). Progressively unquoting and re-matching after
    each layer catches it regardless of how many layers deep it's buried,
    at the cost of also decoding unrelated percent-encoded text in the same
    string — acceptable here since this output is for sharing/analysis, not
    for replay.
    """
    if not isinstance(value, str):
        return value
    value = _redact_html_hidden_inputs(value)
    for _ in range(MAX_UNQUOTE_LAYERS):
        value = SENSITIVE_URL_PARAMS.sub(lambda m: f"{m.group(1)}={REDACTED}", value)
        value = SALESFORCE_SID_SHAPE.sub(REDACTED, value)
        decoded = urllib.parse.unquote(value)
        if decoded == value:
            break
        value = decoded
    value = SENSITIVE_URL_PARAMS.sub(lambda m: f"{m.group(1)}={REDACTED}", value)
    value = SALESFORCE_SID_SHAPE.sub(REDACTED, value)
    return value


def _redact_pairs(pairs: list[dict]) -> None:
    """Redact HAR name/value pair lists (headers, cookies, query, params).

    A pair's value can itself be a JSON blob (e.g. a `postData.params` entry
    named "message" whose value is Aura's action JSON) — check for that
    regardless of whether the outer field name looks sensitive.
    """
    for pair in pairs:
        name = pair.get("name", "")
        value = pair.get("value")
        if SENSITIVE_KEYS.search(name):
            pair["value"] = REDACTED
            continue
        if not isinstance(value, str):
            continue
        try:
            parsed = json.loads(value)
        except (json.JSONDecodeError, TypeError):
            pair["value"] = _redact_string(value)
        else:
            pair["value"] = json.dumps(_redact_json_value(parsed))


def _redact_json_value(obj):
    if isinstance(obj, dict):
        for key, val in list(obj.items()):
            if SENSITIVE_KEYS.search(str(key)):
                obj[key] = REDACTED
            else:
                obj[key] = _redact_json_value(val)
        return obj
    if isinstance(obj, list):
        return [_redact_json_value(v) for v in obj]
    if isinstance(obj, str):
        return _redact_string(obj)
    return obj


def _redact_form_encoded(text: str) -> str:
    """Redact an application/x-www-form-urlencoded body.

    Aura's actual login call (getDoLogin) ships fedID/password inside a
    *form field* named `message` whose value is itself a JSON blob — e.g.
    `message=%7B...%22password%22%3A%22...%22...%7D&aura.context=...`. A
    plain top-level json.loads() on the whole body fails (it isn't JSON,
    the body is form-encoded), so each field needs decoding individually
    and, where a field's value is itself JSON, recursing into that too.
    """
    pairs = urllib.parse.parse_qsl(text, keep_blank_values=True, strict_parsing=False)
    redacted_pairs = []
    for key, value in pairs:
        if SENSITIVE_KEYS.search(key):
            redacted_pairs.append((key, REDACTED))
            continue
        try:
            parsed = json.loads(value)
        except (json.JSONDecodeError, TypeError):
            redacted_pairs.append((key, _redact_string(value)))
        else:
            redacted_pairs.append((key, json.dumps(_redact_json_value(parsed))))
    return urllib.parse.urlencode(redacted_pairs, quote_via=urllib.parse.quote)


def _redact_body_text(text: str, mime_type: str = "") -> str:
    """Redact a request/response body: JSON, form-urlencoded, or plain text."""
    if "form-urlencoded" in mime_type.lower():
        return _redact_form_encoded(text)
    try:
        parsed = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return _redact_string(text)
    return json.dumps(_redact_json_value(parsed))


def redact_har(har: dict) -> dict:
    for entry in har.get("log", {}).get("entries", []):
        request = entry.get("request", {})
        if "url" in request:
            request["url"] = _redact_string(request["url"])
        _redact_pairs(request.get("headers", []))
        _redact_pairs(request.get("cookies", []))
        _redact_pairs(request.get("queryString", []))
        post_data = request.get("postData")
        if post_data:
            params = post_data.get("params")
            if params:
                _redact_pairs(params)
            if post_data.get("text"):
                post_data["text"] = _redact_body_text(
                    post_data["text"], post_data.get("mimeType", "")
                )

        response = entry.get("response", {})
        if response.get("redirectURL"):
            response["redirectURL"] = _redact_string(response["redirectURL"])
        _redact_pairs(response.get("headers", []))
        _redact_pairs(response.get("cookies", []))
        content = response.get("content")
        if content and content.get("text"):
            content["text"] = _redact_body_text(
                content["text"], content.get("mimeType", "")
            )
    return har


def _redact_header_dict(headers: dict) -> None:
    for key, value in list(headers.items()):
        if not isinstance(value, str):
            continue
        headers[key] = REDACTED if SENSITIVE_KEYS.search(key) else _redact_string(value)


def redact_aura_log(entries: list[dict]) -> list[dict]:
    """Redact the flat {url, request body, response body/headers} capture
    formats: bootstrap_login.py --har's `*.aura.json` sidecar (keys
    request_post_data/response_text) and capture_via_mitm.py's output (keys
    request_body/response_body/response_headers). Request bodies along this
    chain are always form-urlencoded when present; response bodies are JSON
    for Aura/token calls but plain HTML for the login-page hops, so those
    fall through _redact_body_text's string-shape pass instead.
    """
    for entry in entries:
        if entry.get("url"):
            entry["url"] = _redact_string(entry["url"])
        for req_key in ("request_post_data", "request_body"):
            if entry.get(req_key):
                entry[req_key] = _redact_body_text(
                    entry[req_key], "application/x-www-form-urlencoded"
                )
        for resp_key in ("response_text", "response_body"):
            if entry.get(resp_key):
                entry[resp_key] = _redact_body_text(entry[resp_key], "application/json")
        if entry.get("response_headers"):
            _redact_header_dict(entry["response_headers"])
    return entries


def main() -> None:
    if len(sys.argv) != 3:
        sys.exit(f"Usage: {sys.argv[0]} <input.har> <output.redacted.har>")
    src, dst = sys.argv[1], sys.argv[2]

    with open(src, encoding="utf-8") as f:
        data = json.load(f)

    if isinstance(data, list):
        redact_aura_log(data)
    else:
        redact_har(data)

    with open(dst, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)

    print(f"Wrote {dst}. Skim it yourself before sharing — this strips known")
    print("secret field names AND secret-shaped values (session IDs, OAuth")
    print("codes) wherever they appear, but double-check for anything")
    print("account-specific (email address, internal account IDs) you'd")
    print("rather not share.")


if __name__ == "__main__":
    main()
