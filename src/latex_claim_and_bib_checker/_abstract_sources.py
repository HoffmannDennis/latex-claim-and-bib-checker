"""Built-in abstract sources.

Crossref, OpenAlex and DataCite are the metadata adapters of
:mod:`._metadata_sources`, the same objects the reference check uses. This
module adds arXiv, Springer Nature, Semantic Scholar, CORE and Europe PMC.
Each implements the same ``Source`` protocol and returns the same
``Result`` type.

Status mapping (all adapters): HTTP 404 or an empty answer is ``ABSENT``.
Network failures, 429, 5xx, other unexpected codes, unparseable bodies and
exceptions inside the adapter are ``ERROR``. A query without a usable
identifier is ``SKIPPED``. After 401/403, or when a response reports that
the request allowance is used up (``X-RateLimit-Remaining: 0``), the
endpoint is locked for the rest of the run: later queries get ``ERROR`` with
the same reason and no request is sent. Failed requests are not retried.

``resolved_doi`` is always taken from the record returned by the source (for
arXiv: the arXiv DOI of the returned identifier), never from the query.
"""

from __future__ import annotations

import os
import re
import time
import xml.etree.ElementTree as ET
from typing import Any
from urllib.parse import quote

import requests

from ._http import redact, user_agent
from ._metadata_sources import CrossrefSource, DataCiteSource, OpenAlexSource
from ._types import Query, Result, Status, normalize_doi

ARXIV_API = "https://export.arxiv.org/api/query"
SPRINGER_API = "https://api.springernature.com/meta/v2/json"
S2_API = "https://api.semanticscholar.org/graph/v1"
CORE_API = "https://api.core.ac.uk/v3"
EUROPEPMC_API = "https://www.ebi.ac.uk/europepmc/webservices/rest"
DOI_RESOLVER = "https://doi.org/"


def _doi_url(doi: str | None) -> str | None:
    return f"{DOI_RESOLVER}{doi}" if doi else None


# ---------------------------------------------------------------------------
# arXiv identifiers
# ---------------------------------------------------------------------------

_NEW_ARXIV_RE = re.compile(r"^(\d{4}\.\d{4,5})(?:v\d+)?$")
_OLD_ARXIV_RE = re.compile(r"^([a-z-]+)(?:\.[a-z-]+)?/(\d{7})(?:v\d+)?$")
_ARXIV_DOI_PREFIX = "10.48550/arxiv."


def normalize_arxiv_id(value: str | None) -> str | None:
    """Canonical arXiv identifier: lower case, no ``arXiv:`` prefix, no version.

    Returns ``None`` for anything that is not an arXiv identifier.
    """
    if not value:
        return None
    text = str(value).strip().lower()
    for prefix in ("https://arxiv.org/abs/", "http://arxiv.org/abs/", "arxiv:"):
        if text.startswith(prefix):
            text = text[len(prefix):].strip()
    match = _NEW_ARXIV_RE.match(text)
    if match:
        return match.group(1)
    match = _OLD_ARXIV_RE.match(text)
    if match:
        return f"{match.group(1)}/{match.group(2)}"
    return None


def arxiv_id_from_doi(doi: str | None) -> str | None:
    """The arXiv identifier inside a ``10.48550/arXiv.<id>`` DOI, else ``None``."""
    norm = normalize_doi(doi)
    if not norm or not norm.startswith(_ARXIV_DOI_PREFIX):
        return None
    return normalize_arxiv_id(norm[len(_ARXIV_DOI_PREFIX):])


def arxiv_doi(arxiv_id: str) -> str:
    return f"{_ARXIV_DOI_PREFIX}{arxiv_id.lower()}"


# ---------------------------------------------------------------------------
# Shared adapter behaviour
# ---------------------------------------------------------------------------


class HttpSource:
    """Session handling, throttling, endpoint locks and HTTP status mapping."""

    name: str = ""
    order: int = 0
    required_env: tuple[str, ...] = ()
    min_interval: float = 0.0

    def __init__(self, *, session: Any | None = None, contact_email: str | None = None,
                 timeout: float = 30.0, min_interval: float | None = None) -> None:
        self._session = session
        self._contact_email = (contact_email or "").strip() or None
        self._timeout = timeout
        if min_interval is not None:
            self.min_interval = max(0.0, float(min_interval))
        self._last_request: float | None = None
        self._locks: dict[str, str] = {}

    # protocol ---------------------------------------------------------------

    def available(self) -> bool:
        return True

    def fetch(self, query: Query) -> Result:
        try:
            return self._fetch(query)
        except Exception as exc:  # one faulty answer must not end the run
            return self._error(f"adapter error: {type(exc).__name__}: {self._clean(str(exc))}"[:300])

    def _fetch(self, query: Query) -> Result:  # pragma: no cover - overridden
        raise NotImplementedError

    # results ----------------------------------------------------------------

    def _secrets(self) -> tuple[str | None, ...]:
        return ()

    def _clean(self, text: str) -> str:
        return redact(text, self._secrets())

    def _result(self, status: Status, reason: str | None = None, **fields: Any) -> Result:
        return Result(
            status=status,
            record=fields.get("record"),
            abstract=fields.get("abstract"),
            source=self.name,
            public_url=fields.get("public_url"),
            reason=reason,
            matched_by=fields.get("matched_by"),
            resolved_doi=fields.get("resolved_doi"),
        )

    def _error(self, reason: str) -> Result:
        return self._result(Status.ERROR, self._clean(reason))

    def _absent(self, reason: str, **fields: Any) -> Result:
        return self._result(Status.ABSENT, reason, **fields)

    def _skipped(self, reason: str) -> Result:
        return self._result(Status.SKIPPED, reason)

    # HTTP -------------------------------------------------------------------

    def _headers(self) -> dict[str, str]:
        return {"User-Agent": user_agent(self._contact_email), "Accept": "application/json"}

    def _get_session(self) -> Any:
        if self._session is None:
            self._session = requests.Session()
        return self._session

    def _throttle(self) -> None:
        if self.min_interval and self._last_request is not None:
            wait = self.min_interval - (time.monotonic() - self._last_request)
            if wait > 0:
                time.sleep(wait)
        self._last_request = time.monotonic()

    def _get(self, endpoint: str, url: str, params: dict[str, Any] | None = None,
             headers: dict[str, str] | None = None) -> tuple[Any, Result | None]:
        """GET ``url``; return ``(response, None)`` or ``(None, failure_result)``."""
        locked = self._locks.get(endpoint)
        if locked is not None:
            return None, self._error(locked)
        self._throttle()
        try:
            response = self._get_session().get(
                url, params=params or {}, headers={**self._headers(), **(headers or {})},
                timeout=self._timeout,
            )
        except requests.RequestException as exc:
            detail = self._clean(str(exc))[:200]
            name = type(exc).__name__
            return None, self._error(f"network error ({name}): {detail}" if detail else f"network error ({name})")

        code = response.status_code
        if code == 404:
            return None, self._absent("not found")
        if code in (401, 403):
            reason = f"HTTP {code} (access denied)"
            self._locks[endpoint] = reason
            return None, self._error(reason)
        if code == 429:
            if _allowance_used_up(response):
                reason = "HTTP 429 (rate limited, request allowance used up)"
                self._locks[endpoint] = reason
                return None, self._error(reason)
            return None, self._error("HTTP 429 (rate limited)")
        if code >= 500:
            return None, self._error(f"HTTP {code} (server error)")
        if code == 204:
            return None, self._absent("empty response")
        if not 200 <= code < 300:
            return None, self._error(f"HTTP {code} (unexpected response)")
        if _allowance_used_up(response):
            self._locks[endpoint] = "request allowance used up"
        if not (response.content or b"").strip():
            return None, self._absent("empty response")
        return response, None

    def _get_json(self, endpoint: str, url: str, params: dict[str, Any] | None = None,
                  headers: dict[str, str] | None = None) -> tuple[Any, Result | None]:
        response, failure = self._get(endpoint, url, params, headers)
        if failure is not None:
            return None, failure
        try:
            data = response.json()
        except ValueError:
            return None, self._error("invalid response (not JSON)")
        if data is None or data == {} or data == []:
            return None, self._absent("empty response")
        if not isinstance(data, dict):
            return None, self._error("invalid response (unexpected structure)")
        return data, None


def _allowance_used_up(response: Any) -> bool:
    headers = getattr(response, "headers", None) or {}
    remaining = headers.get("X-RateLimit-Remaining")
    return remaining is not None and str(remaining).strip() == "0"


def _text(value: Any) -> str | None:
    """Plain string from a field that may be a string, a list or a dict of parts."""
    if value is None:
        return None
    if isinstance(value, str):
        return value if value.strip() else None
    if isinstance(value, list):
        parts = [p for p in (_text(v) for v in value) if p]
        return " ".join(parts) if parts else None
    if isinstance(value, dict):
        for key in ("p", "value", "text", "#text"):
            if key in value:
                return _text(value[key])
    return None


# ---------------------------------------------------------------------------
# arXiv
# ---------------------------------------------------------------------------

_ATOM = "{http://www.w3.org/2005/Atom}"
_ARXIV_NS = "{http://arxiv.org/schemas/atom}"


class ArxivSource(HttpSource):
    """arXiv API, for arXiv identifiers and ``10.48550/arXiv.*`` DOIs only.

    At most one request every three seconds.
    """

    name = "arxiv"
    order = 1
    min_interval = 3.0

    def __init__(self, *, base_url: str = ARXIV_API, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._base = base_url

    def _headers(self) -> dict[str, str]:
        return {"User-Agent": user_agent(self._contact_email), "Accept": "application/atom+xml"}

    def _fetch(self, query: Query) -> Result:
        arxiv_id = normalize_arxiv_id(query.arxiv_id)
        matched_by = "arxiv_id"
        if arxiv_id is None:
            arxiv_id = arxiv_id_from_doi(query.doi)
            matched_by = "doi"
        if arxiv_id is None:
            return self._skipped("needs an arXiv identifier or an arXiv DOI")

        response, failure = self._get("query", self._base, {"id_list": arxiv_id, "max_results": 1})
        if failure is not None:
            return failure
        try:
            root = ET.fromstring(response.content)
        except ET.ParseError:
            return self._error("invalid response (not XML)")
        entry = root.find(f"{_ATOM}entry")
        if entry is None:
            return self._absent("not found")
        entry_id = (entry.findtext(f"{_ATOM}id") or "").strip()
        if "/api/errors" in entry_id or "/abs/" not in entry_id:
            return self._absent("not found")
        returned = normalize_arxiv_id(entry_id.split("/abs/", 1)[1])
        if returned != arxiv_id:
            return self._absent("identifier mismatch")
        record = {
            "id": returned,
            "title": " ".join((entry.findtext(f"{_ATOM}title") or "").split()),
            "summary": entry.findtext(f"{_ATOM}summary"),
            "published": entry.findtext(f"{_ATOM}published"),
            "journal_doi": entry.findtext(f"{_ARXIV_NS}doi"),
        }
        return self._result(
            Status.FOUND,
            record=record,
            abstract=record["summary"],
            public_url=f"https://arxiv.org/abs/{returned}",
            matched_by=matched_by,
            resolved_doi=arxiv_doi(returned),
        )


# ---------------------------------------------------------------------------
# Springer Nature Meta API
# ---------------------------------------------------------------------------


class SpringerSource(HttpSource):
    """Springer Nature Meta API (DOI lookup). Needs ``SPRINGER_API_KEY``.

    The API expects the key as a query parameter; it is removed from every
    message the adapter produces.
    """

    name = "springer"
    order = 5
    required_env = ("SPRINGER_API_KEY",)

    def __init__(self, *, api_key: str | None = None, base_url: str = SPRINGER_API, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        if api_key is None:
            api_key = os.environ.get("SPRINGER_API_KEY")
        self._api_key = (api_key or "").strip() or None
        self._base = base_url

    def available(self) -> bool:
        return self._api_key is not None

    def _secrets(self) -> tuple[str | None, ...]:
        return (self._api_key,)

    def _fetch(self, query: Query) -> Result:
        doi = normalize_doi(query.doi)
        if not doi:
            return self._skipped("needs a DOI")
        if self._api_key is None:
            return self._error("SPRINGER_API_KEY is not set")
        data, failure = self._get_json(
            "meta", self._base, {"q": f"doi:{doi}", "p": 1, "api_key": self._api_key}
        )
        if failure is not None:
            return failure
        records = data.get("records")
        if not records:
            return self._absent("not found")
        if not isinstance(records, list) or not isinstance(records[0], dict):
            return self._error("invalid response (unexpected structure)")
        record = records[0]
        resolved = normalize_doi(record.get("doi")) if isinstance(record.get("doi"), str) else None
        if resolved != doi:
            return self._absent("doi mismatch", resolved_doi=resolved, matched_by="doi")
        return self._result(
            Status.FOUND,
            record=record,
            abstract=_text(record.get("abstract")),
            public_url=_doi_url(resolved),
            matched_by="doi",
            resolved_doi=resolved,
        )


# ---------------------------------------------------------------------------
# Semantic Scholar
# ---------------------------------------------------------------------------


class SemanticScholarSource(HttpSource):
    """Semantic Scholar Graph API, by DOI or arXiv identifier.

    Works without a key; ``S2_API_KEY`` is sent as ``x-api-key`` when set.
    """

    name = "s2"
    order = 6
    min_interval = 1.0

    def __init__(self, *, api_key: str | None = None, base_url: str = S2_API, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        if api_key is None:
            api_key = os.environ.get("S2_API_KEY")
        self._api_key = (api_key or "").strip() or None
        self._base = base_url.rstrip("/")

    def _secrets(self) -> tuple[str | None, ...]:
        return (self._api_key,)

    def _fetch(self, query: Query) -> Result:
        doi = normalize_doi(query.doi)
        arxiv_id = normalize_arxiv_id(query.arxiv_id)
        if doi:
            paper_ref, matched_by = f"DOI:{quote(doi, safe='/:()')}", "doi"
        elif arxiv_id:
            paper_ref, matched_by = f"ARXIV:{arxiv_id}", "arxiv_id"
        else:
            return self._skipped("needs a DOI or an arXiv identifier")
        headers = {"x-api-key": self._api_key} if self._api_key else {}
        data, failure = self._get_json(
            "paper", f"{self._base}/paper/{paper_ref}",
            {"fields": "title,abstract,externalIds,url"}, headers,
        )
        if failure is not None:
            return failure
        ids = data.get("externalIds") or {}
        if not isinstance(ids, dict):
            ids = {}
        record_doi = ids.get("DOI")
        resolved = normalize_doi(record_doi) if isinstance(record_doi, str) else None
        if matched_by == "doi" and resolved is not None and resolved != doi:
            return self._absent("doi mismatch", resolved_doi=resolved, matched_by=matched_by)
        if matched_by == "arxiv_id":
            record_arxiv = normalize_arxiv_id(ids.get("ArXiv")) if isinstance(ids.get("ArXiv"), str) else None
            if record_arxiv is not None and record_arxiv != arxiv_id:
                return self._absent("identifier mismatch")
        return self._result(
            Status.FOUND,
            record=data,
            abstract=_text(data.get("abstract")),
            public_url=_doi_url(resolved) or (data.get("url") if isinstance(data.get("url"), str) else None),
            matched_by=matched_by,
            resolved_doi=resolved,
        )


# ---------------------------------------------------------------------------
# CORE
# ---------------------------------------------------------------------------


class CoreSource(HttpSource):
    """CORE API v3 (search by DOI). Works without a key; ``CORE_API_KEY`` is optional.

    A search hit counts only if the record's own DOI equals the requested
    one; otherwise the result is ``ABSENT`` with reason ``doi mismatch``.
    """

    name = "core"
    order = 7
    min_interval = 2.0

    def __init__(self, *, api_key: str | None = None, base_url: str = CORE_API, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        if api_key is None:
            api_key = os.environ.get("CORE_API_KEY")
        self._api_key = (api_key or "").strip() or None
        self._base = base_url.rstrip("/")

    def _secrets(self) -> tuple[str | None, ...]:
        return (self._api_key,)

    def _fetch(self, query: Query) -> Result:
        doi = normalize_doi(query.doi)
        if not doi:
            return self._skipped("needs a DOI")
        headers = {"Authorization": f"Bearer {self._api_key}"} if self._api_key else {}
        data, failure = self._get_json(
            "search", f"{self._base}/search/works", {"q": f'doi:"{doi}"', "limit": 1}, headers
        )
        if failure is not None:
            return failure
        results = data.get("results")
        if not results:
            return self._absent("not found")
        if not isinstance(results, list) or not isinstance(results[0], dict):
            return self._error("invalid response (unexpected structure)")
        record = results[0]
        record_doi = record.get("doi")
        resolved = normalize_doi(record_doi) if isinstance(record_doi, str) else None
        if resolved != doi:
            return self._absent("doi mismatch", resolved_doi=resolved, matched_by="doi")
        return self._result(
            Status.FOUND,
            record=record,
            abstract=_text(record.get("abstract")),
            public_url=_doi_url(resolved),
            matched_by="doi",
            resolved_doi=resolved,
        )


# ---------------------------------------------------------------------------
# Europe PMC
# ---------------------------------------------------------------------------


class EuropePmcSource(HttpSource):
    """Europe PMC REST API (search by DOI); strongest for the life sciences."""

    name = "europepmc"
    order = 8

    def __init__(self, *, base_url: str = EUROPEPMC_API, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._base = base_url.rstrip("/")

    def _fetch(self, query: Query) -> Result:
        doi = normalize_doi(query.doi)
        if not doi:
            return self._skipped("needs a DOI")
        data, failure = self._get_json(
            "search", f"{self._base}/search",
            {"query": f'DOI:"{doi}"', "resultType": "core", "format": "json", "pageSize": 1},
        )
        if failure is not None:
            return failure
        result_list = data.get("resultList") or {}
        results = result_list.get("result") if isinstance(result_list, dict) else None
        if not results:
            return self._absent("not found")
        if not isinstance(results, list) or not isinstance(results[0], dict):
            return self._error("invalid response (unexpected structure)")
        record = results[0]
        record_doi = record.get("doi")
        resolved = normalize_doi(record_doi) if isinstance(record_doi, str) else None
        if resolved != doi:
            return self._absent("doi mismatch", resolved_doi=resolved, matched_by="doi")
        return self._result(
            Status.FOUND,
            record=record,
            abstract=_text(record.get("abstractText")),
            public_url=_doi_url(resolved),
            matched_by="doi",
            resolved_doi=resolved,
        )


# ---------------------------------------------------------------------------
# Construction
# ---------------------------------------------------------------------------


METADATA_MIN_INTERVAL = 0.5  # pause between two requests to the same metadata source


def metadata_sources(session: Any, contact_email: str | None = None,
                     min_interval: float = METADATA_MIN_INTERVAL) -> list[Any]:
    """Crossref, OpenAlex and DataCite, in the order the reference check asks them."""
    contact = (contact_email or "").strip() or None
    args = {"session": session, "contact_email": contact or "", "min_interval": min_interval}
    return [CrossrefSource(**args), OpenAlexSource(**args), DataCiteSource(**args)]


def builtin_sources(session: Any, contact_email: str | None = None,
                    metadata: list[Any] | None = None) -> list[Any]:
    """All built-in abstract sources, sharing one HTTP session.

    ``metadata`` are the Crossref, OpenAlex and DataCite objects of the run
    (see :func:`metadata_sources`). They are shared with the reference
    check, so a request made there is not repeated here.
    """
    contact = (contact_email or "").strip() or None
    if metadata is None:
        metadata = metadata_sources(session, contact)
    own_args = {"session": session, "contact_email": contact}
    return [
        ArxivSource(**own_args),
        *metadata,
        SpringerSource(**own_args),
        SemanticScholarSource(**own_args),
        CoreSource(**own_args),
        EuropePmcSource(**own_args),
    ]
