"""Pure-HTTP Salesforce/Cognito login for Cummins Connect Cloud.

Replicates, without a browser, the SSO dance tools/bootstrap_login.py drives
via Playwright: Cognito hosted UI -> SAML federation into Cummins'
Salesforce Experience Cloud site (mylogin.cummins.com) -> a custom Apex login
bridge (IAM_VisualforceToLightning.getDoLogin, invoked over the Aura
protocol) -> SAML response back to Cognito -> connectcloud:// callback with
an authorization code -> token exchange.

This exists so the config flow can ask for username/password directly and
work on every Home Assistant install type, including HAOS, which cannot run
a bundled browser at all. Confirmed viable by capturing the full chain with
mitmproxy (see docs/DESIGN.md and tools/capture_via_mitm.py): every hop that
looked like it might need real JS turned out to be either a static,
server-rendered redirect (`window.location.replace('<url>')` with the target
already baked into the HTML — nothing computed client-side) or a standard
auto-submitting SAML form. Nothing in this chain requires executing
JavaScript, just parsing it out with regexes.

Known limitations (same as bootstrap_login.py):
  * No MFA support — Cummins' identity bridge doesn't appear to prompt for
    it today, but if a given account has it enabled this will fail at
    getDoLogin with an unhelpful error rather than a clear "needs MFA" one.
  * The response SHAPE for a *wrong* password was never captured (only a
    successful login was available to reverse-engineer from) — any
    non-success shape from getDoLogin is treated as AuraLoginError with
    whatever diagnostic text is available, rather than pattern-matching a
    specific "bad password" response we haven't actually seen.
  * Whether getWWIDLoginInstructions/getAppMappingDetails need to be called
    before getDoLogin (the real browser flow calls them, seemingly just to
    render UI text) is unconfirmed — this skips them. If getDoLogin starts
    failing where the browser flow still works, calling those first is the
    first thing to try.
  * `r=0` is sent as the Aura request-sequence counter on every call rather
    than a real incrementing count; this is client-side bookkeeping in the
    real app, not something the server appears to validate.
"""

from __future__ import annotations

import base64
import hashlib
import html
import json
import re
import secrets
import urllib.parse
from typing import Any

import requests

from .const import AUTHORIZE_URL, CLIENT_ID, REDIRECT_URI, SCOPE, TOKEN_URL, USER_AGENT

LOGIN_TIMEOUT = 30


class AuraLoginError(Exception):
    """Raised when the username/password login fails at any step."""


def _diagnostics(resp: requests.Response) -> str:
    """A short, log-line-friendly dump of an unexpected response — status,
    final URL (redirects can land somewhere unexpected), and a text snippet
    (long enough to reveal a captcha/WAF-block/error page, short enough to
    stay readable in a log line).
    """
    snippet = re.sub(r"\s+", " ", resp.text[:500]).strip()
    return f"status={resp.status_code} url={resp.url} body[:500]={snippet!r}"


def _pkce() -> tuple[str, str]:
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode()
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()
    ).rstrip(b"=").decode()
    return verifier, challenge


def _extract_js_redirect(html_text: str, base_url: str) -> str:
    """Pull the target out of one of Salesforce's two static bounce-page
    templates and resolve it against the page it came from. Both hops that
    use this (the pre-login SAML 401 bounce, and the post-login
    frontdoor.jsp bounce) turned out to use different templates: one
    assigns the target to a `var url = '...'` before calling
    window.location.replace(url + escapedHash), the other passes a literal
    straight to window.location.replace('...'). Neither actually computes
    anything dynamic — try the `var url` form first since when present it's
    the real target, falling back to the direct-literal form.
    """
    match = re.search(r"var\s+url\s*=\s*'([^']+)'", html_text)
    if not match:
        match = re.search(r"window\.location\.replace\('([^']+)'\)", html_text)
    if not match:
        raise AuraLoginError(
            "Expected a static JS redirect but didn't find one — Cummins' "
            "login page may have changed."
        )
    return urllib.parse.urljoin(base_url, match.group(1))


def _extract_login_context(html_text: str) -> dict[str, Any]:
    """Pull fwuid/app/loaded out of the login page's embedded Aura bootstrap
    config (a JSON blob living inside a <script src="/clw/s/sfsites/l/{...}/
    ..."> tag) — not exposed anywhere else on this page.
    """
    fwuid_m = re.search(r'"fwuid":"([^"]+)"', html_text)
    app_m = re.search(r'"app":"([^"]+)"', html_text)
    loaded_m = re.search(r'"loaded":\{"([^"]+)":"([^"]+)"\}', html_text)
    if not (fwuid_m and app_m and loaded_m):
        raise AuraLoginError(
            "Could not find the Aura fwuid/app/loaded bootstrap on the "
            "login page — Cummins' login page may have changed."
        )
    return {
        "mode": "PROD",
        "fwuid": fwuid_m.group(1),
        "app": app_m.group(1),
        "loaded": {loaded_m.group(1): loaded_m.group(2)},
        "dn": [],
        "globals": {},
        "uad": True,
    }


def _extract_saml_form(html_text: str) -> tuple[str, dict[str, str]]:
    """Pull the auto-submitting SAML form's action + hidden inputs out of the
    now-authenticated /clw/idp/login page. Standard Salesforce SP-initiated
    SAML template — attribute values are HTML-entity-encoded (e.g. &#x3a; for
    ':'), hence the html.unescape() calls.
    """
    action_m = re.search(r'<form[^>]*action="([^"]+)"', html_text, re.IGNORECASE)
    if not action_m:
        raise AuraLoginError(
            "Could not find the SAML auto-submit form after login — "
            "Cummins' login page may have changed."
        )
    action_url = html.unescape(action_m.group(1))
    fields = {
        html.unescape(name): html.unescape(value)
        for name, value in re.findall(
            r'<input[^>]*type="hidden"[^>]*name="([^"]+)"[^>]*value="([^"]*)"',
            html_text,
            re.IGNORECASE,
        )
    }
    if "SAMLResponse" not in fields:
        raise AuraLoginError(
            "SAML form found but missing SAMLResponse — Cummins' login "
            "page may have changed."
        )
    return action_url, fields


def _aura_action(method: str, params: dict[str, Any]) -> dict:
    return {
        "id": "0;a",
        "descriptor": "aura://ApexActionController/ACTION$execute",
        "callingDescriptor": "UNKNOWN",
        "params": {
            "namespace": "",
            "classname": "IAM_VisualforceToLightning",
            "method": method,
            "params": params,
            "cacheable": False,
            "isContinuation": False,
        },
    }


def _post_aura(
    session: requests.Session,
    aura_endpoint: str,
    action: dict,
    context: dict,
    page_uri: str,
) -> Any:
    data = {
        "message": json.dumps({"actions": [action]}),
        "aura.context": json.dumps(context),
        "aura.pageURI": page_uri,
        "aura.token": "null",
    }
    resp = session.post(
        aura_endpoint,
        params={"r": "0", "aura.ApexAction.execute": "1"},
        data=data,
        headers={"user-agent": USER_AGENT},
        timeout=LOGIN_TIMEOUT,
    )
    resp.raise_for_status()
    body = resp.json()
    if "actions" not in body:
        raise AuraLoginError(f"Unexpected Aura response shape: {body!r}"[:300])
    result = body["actions"][0]
    if result.get("state") != "SUCCESS":
        raise AuraLoginError(
            (f"Aura action {action['params']['method']} failed: "
             f"{result.get('error') or result}")[:300]
        )
    return result["returnValue"]


def login(username: str, password: str) -> dict:
    """Log in with a username/password, no browser.

    Returns the Cognito token response dict (access_token, id_token,
    refresh_token, expires_in). Raises AuraLoginError on any failure along
    the chain — wrong credentials and "Cummins changed something" both
    surface this way; there's no reliable way yet to tell them apart (see
    module docstring).
    """
    verifier, challenge = _pkce()
    state = secrets.token_urlsafe(32)
    session = requests.Session()
    session.headers.update(
        {
            "user-agent": USER_AGENT,
            "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "accept-language": "en-US,en;q=0.9",
        }
    )

    authorize_url = f"{AUTHORIZE_URL}?" + urllib.parse.urlencode(
        {
            "response_type": "code",
            "client_id": CLIENT_ID,
            "redirect_uri": REDIRECT_URI,
            "scope": SCOPE,
            "state": state,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        }
    )

    # 1. Kick off the SAML dance. `requests` auto-follows the leading 302s
    #    and lands on an anonymous /clw/idp/login hit that 401s with a
    #    static bounce page (a 401 isn't something requests auto-follows).
    resp = session.get(authorize_url, timeout=LOGIN_TIMEOUT)
    if resp.status_code != 401:
        raise AuraLoginError(
            "Expected the initial SAML hop to land on a 401 bounce page, "
            f"got {resp.status_code} at {resp.url}"
        )
    try:
        bounce_url = _extract_js_redirect(resp.text, resp.url)
    except AuraLoginError as err:
        raise AuraLoginError(f"{err} [{_diagnostics(resp)}]") from err

    # The bounce URL's own `startURL` query param is exactly the value
    # Cummins' custom login Apex controller wants back later (both
    # getAppMappingDetails and getDoLogin use it) — a single source of
    # truth, no need to separately reconstruct it.
    inner_start_url = urllib.parse.parse_qs(urllib.parse.urlparse(bounce_url).query)[
        "startURL"
    ][0]

    # 2. Follow the bounce to the actual (guest-session) login page.
    resp = session.get(bounce_url, timeout=LOGIN_TIMEOUT)
    resp.raise_for_status()
    parsed_login_page = urllib.parse.urlparse(resp.url)
    page_uri = parsed_login_page.path + (
        f"?{parsed_login_page.query}" if parsed_login_page.query else ""
    )
    try:
        context = _extract_login_context(resp.text)
    except AuraLoginError as err:
        raise AuraLoginError(f"{err} [{_diagnostics(resp)}]") from err
    aura_endpoint = f"{parsed_login_page.scheme}://{parsed_login_page.netloc}/clw/s/sfsites/aura"

    # 3. Submit credentials via the same Apex bridge the app's browser
    #    login uses (IAM_VisualforceToLightning.getDoLogin).
    action = _aura_action(
        "getDoLogin",
        {
            "fedID": username,
            "password": password,
            "startURL": inner_start_url,
            "resourceURL": None,
            "appID": None,
            "lang": "en_US",
        },
    )
    return_value = _post_aura(session, aura_endpoint, action, context, page_uri)
    frontdoor_url = return_value.get("returnValue") if isinstance(return_value, dict) else None
    if not isinstance(frontdoor_url, str) or "frontdoor.jsp" not in frontdoor_url:
        raise AuraLoginError(
            "getDoLogin succeeded but didn't return a usable frontdoor "
            "URL — wrong username/password, or Cummins' login flow changed."
        )

    # 4. Redeem the frontdoor URL for a real (authenticated) session, then
    #    follow its own bounce back into the SAML SP flow.
    resp = session.get(frontdoor_url, timeout=LOGIN_TIMEOUT)
    resp.raise_for_status()
    try:
        saml_continue_url = _extract_js_redirect(resp.text, resp.url)
    except AuraLoginError as err:
        raise AuraLoginError(f"{err} [{_diagnostics(resp)}]") from err
    resp = session.get(saml_continue_url, timeout=LOGIN_TIMEOUT)
    resp.raise_for_status()

    # 5. Now-authenticated /clw/idp/login returns an auto-submitting SAML
    #    form; POST it to Cognito ourselves instead of running its JS.
    try:
        saml_action_url, saml_fields = _extract_saml_form(resp.text)
    except AuraLoginError as err:
        raise AuraLoginError(f"{err} [{_diagnostics(resp)}]") from err
    resp = session.post(
        saml_action_url, data=saml_fields, timeout=LOGIN_TIMEOUT, allow_redirects=False
    )
    if resp.status_code not in (301, 302, 303, 307, 308):
        raise AuraLoginError(
            f"Expected Cognito to redirect after the SAML POST, got {resp.status_code}"
        )
    callback_url = resp.headers.get("location", "")
    if not callback_url.startswith(REDIRECT_URI):
        raise AuraLoginError(
            f"Unexpected redirect target after SAML POST: {callback_url[:200]}"
        )

    # 6. Extract + validate the authorization code, exchange it for tokens.
    query = urllib.parse.parse_qs(urllib.parse.urlparse(callback_url).query)
    code = query.get("code", [""])[0]
    returned_state = query.get("state", [None])[0]
    if returned_state and returned_state != state:
        raise AuraLoginError(
            "State mismatch on the OAuth callback — possible cross-session "
            "confusion, please retry."
        )
    if not code:
        raise AuraLoginError(f"No authorization code in callback: {callback_url[:200]}")

    token_resp = requests.post(
        TOKEN_URL,
        data={
            "grant_type": "authorization_code",
            "client_id": CLIENT_ID,
            "code": code,
            "redirect_uri": REDIRECT_URI,
            "code_verifier": verifier,
        },
        headers={
            "content-type": "application/x-www-form-urlencoded; charset=utf-8",
            "user-agent": USER_AGENT,
        },
        timeout=LOGIN_TIMEOUT,
    )
    if token_resp.status_code != 200:
        raise AuraLoginError(
            f"Token exchange failed ({token_resp.status_code}): {token_resp.text[:300]}"
        )
    return token_resp.json()
