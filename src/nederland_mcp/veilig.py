"""Security headers: on every response, and for each page a policy that only runs that page's own scripts."""

import base64
import hashlib
import re
from typing import Any

# On every response: only over HTTPS from now on, no guessing what a file is, never in a frame on another site,
# only the domain as referrer for a link to elsewhere, and no camera, microphone or location.
HEADERS = [
    (b"strict-transport-security", b"max-age=31536000"),
    (b"x-content-type-options", b"nosniff"),
    (b"x-frame-options", b"DENY"),
    (b"referrer-policy", b"strict-origin-when-cross-origin"),
    (b"permissions-policy", b"camera=(), microphone=(), geolocation=(), payment=(), usb=()"),
    (b"cross-origin-opener-policy", b"same-origin"),
]
# A response that is not a page loads nothing at all.
NOTHING = b"default-src 'none'; frame-ancestors 'none'; base-uri 'none'"
SCRIPT = re.compile(r"<script(?:\s[^>]*)?>(.*?)</script>", re.S)


def csp(page: str) -> str:
    """The policy for a page: everything from this site itself, and of its own scripts only these exact ones, by
    their hash, so a script that gets into the page any other way does not run. A browser hashes a script with its
    line ends as LF."""
    hashes = [
        "'sha256-" + base64.b64encode(hashlib.sha256(_lf(script).encode()).digest()).decode() + "'"
        for script in SCRIPT.findall(page)
    ]
    scripts = " ".join(hashes) or "'none'"
    return (
        f"default-src 'self'; script-src {scripts}; object-src 'none'; base-uri 'none'; form-action 'self'; "
        "frame-ancestors 'none'"
    )


def _lf(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n")


def headers(app: Any) -> Any:
    """Around the whole app: HEADERS on every response, and NOTHING as policy where a route sets none."""

    async def secured(scope: dict, receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await app(scope, receive, send)
            return

        async def sending(message: dict) -> None:
            if message["type"] == "http.response.start":
                given = list(message.get("headers", []))
                names = {name.lower() for name, _ in given}
                extra = [header for header in HEADERS if header[0] not in names]
                if b"content-security-policy" not in names:
                    extra.append((b"content-security-policy", NOTHING))
                message = {**message, "headers": given + extra}
            await send(message)

        await app(scope, receive, sending)

    return secured
