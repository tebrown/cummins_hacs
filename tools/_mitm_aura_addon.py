"""
mitmproxy addon for capture_via_mitm.py — dumps every request/response along
the SAML/Aura/OAuth login chain (skipping static assets: JS bundles, CSS,
fonts, images) to a JSON file, including response headers so redirect-style
hops (Location, or lack thereof) are visible even when there's no body.

Why mitmproxy at all: Playwright's CDP-based Network.getResponseBody (used by
both the passive .har recorder and a live page.on("response") handler, as
bootstrap_login.py tried first) unreliably fails for Cache-Control: no-store
responses — several hops in this chain use exactly that — once the page's
own JS has already drained the body. mitmproxy sees the raw decrypted HTTPS
bytes independently of what the browser does with them afterward, so it
doesn't have that race.

Not meant to be run directly — loaded via:
    mitmdump -s tools/_mitm_aura_addon.py --set aura_out=<path>
"""

from __future__ import annotations

import json

from mitmproxy import ctx, http

# Dynamic, login-relevant endpoints only. Everything else (aura_prod.js,
# app.js, fonts, CSS, images, iconSvgTemplates, ...) is static framework
# boilerplate we already know isn't needed to replicate the login.
RELEVANT_PATH_MARKERS = (
    "/clw/idp/",
    "/clw/s/login",
    "/clw/secur/frontdoor.jsp",
    "/clw/s/sfsites/aura",
    "/saml2/idpresponse",
    "/oauth2/authorize",
    "/oauth2/token",
)


def _is_relevant(url: str) -> bool:
    return any(marker in url for marker in RELEVANT_PATH_MARKERS)


class AuraDump:
    def __init__(self) -> None:
        self.entries: list[dict] = []

    def load(self, loader) -> None:
        loader.add_option(
            name="aura_out",
            typespec=str,
            default="login_capture.mitm.json",
            help="Output JSON file for captured login-chain calls",
        )

    def response(self, flow: http.HTTPFlow) -> None:
        url = flow.request.pretty_url
        if not _is_relevant(url):
            return
        resp = flow.response
        self.entries.append(
            {
                "url": url,
                "method": flow.request.method,
                "status": resp.status_code if resp else None,
                "request_body": flow.request.get_text(strict=False),
                "response_headers": dict(resp.headers) if resp else None,
                "response_body": resp.get_text(strict=False) if resp else None,
            }
        )

    def done(self) -> None:
        with open(ctx.options.aura_out, "w", encoding="utf-8") as f:
            json.dump(self.entries, f, indent=2)
        ctx.log.info(f"Wrote {len(self.entries)} captured call(s) to {ctx.options.aura_out}")


addons = [AuraDump()]
