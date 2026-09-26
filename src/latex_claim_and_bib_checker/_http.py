"""HTTP plumbing shared by the built-in abstract sources and the model clients.

Only ``requests`` is used. Secrets are sent in headers wherever the remote
API allows it; :func:`redact` removes them from any text that could reach
the terminal or a report.
"""

from __future__ import annotations

import re
from typing import Any, Iterable
from urllib.parse import quote

import requests

from ._version import __version__

USER_AGENT = f"latex-claim-and-bib-checker/{__version__}"


def new_session() -> Any:
    """Create the HTTP session for one run (tests replace this function)."""
    return requests.Session()


def user_agent(contact_email: str | None = None) -> str:
    return f"{USER_AGENT} (mailto:{contact_email})" if contact_email else USER_AGENT


_URL_USERINFO_RE = re.compile(r"(?i)\b([a-z][a-z0-9+.-]*://)[^/\s@'\"<>]+@")
_QUERY_STRING_RE = re.compile(r"\?[A-Za-z0-9_.\[\]-]+=[^\s'\"()<>]*")
_SECRET_PARAM_RE = re.compile(
    r"(?i)\b([a-z0-9_-]*(?:key|token|secret|password|signature)[a-z0-9_-]*)=([^&\s'\"()<>]+)"
)
_AUTH_HEADER_RE = re.compile(
    r"(?i)\b(authorization|proxy-authorization|x-[a-z0-9-]*(?:key|token))"
    r"(['\"]?\s*[:=]\s*['\"]?)(?:(bearer|basic|token)\s+)?([^\s'\",}]+)"
)


def redact(text: str, secrets: Iterable[str | None] = ()) -> str:
    """Remove credentials from ``text``.

    Known secret values are replaced wherever they occur (plain and
    URL-encoded), query strings are dropped from URLs, and values of
    key/token parameters, authorisation headers and user information in
    URLs (``scheme://user:password@host``) are masked. Redact a text
    completely before cutting it, so that no part of a secret survives.
    """
    for secret in secrets:
        if secret:
            text = text.replace(secret, "***")
            text = text.replace(quote(secret, safe=""), "***")
    text = _URL_USERINFO_RE.sub(lambda m: f"{m.group(1)}***@", text)
    text = _SECRET_PARAM_RE.sub(lambda m: f"{m.group(1)}=***", text)
    text = _AUTH_HEADER_RE.sub(
        lambda m: f"{m.group(1)}{m.group(2)}{(m.group(3) + ' ') if m.group(3) else ''}***", text
    )
    return _QUERY_STRING_RE.sub("?...", text)
