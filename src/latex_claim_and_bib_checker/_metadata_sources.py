"""The built-in metadata sources: Crossref, OpenAlex and DataCite.

Each adapter implements the :class:`~latex_claim_and_bib_checker.Source`
protocol. Adapters never raise for expected failures. Every outcome is a
:class:`~latex_claim_and_bib_checker.Result`.

HTTP outcomes map to statuses as follows: 404 or an empty answer is
``ABSENT``. Network failures, 429, 5xx, other unexpected codes and
unparseable bodies are ``ERROR`` (with no retries). After a 401 or 403 an
endpoint is locked for the lifetime of the adapter object: it answers
``ERROR`` with the same reason and sends no further requests. The OpenAlex
title-search endpoint is also locked after a 429, because a 429 there means
the search allowance is used up.

One adapter object serves both the reference check and the claim check of
a run. It remembers, in memory only, every answer with status 2xx, 204 or
404 per endpoint, URL and parameters, so the same request is sent at most
once per run. Errors are never remembered, and nothing is written to disk.
"""

from __future__ import annotations

import os
import re
import time
from typing import Any, Callable
from urllib.parse import quote

import requests

from ._compare import clean_latex, title_similarity
from ._http import USER_AGENT, redact, user_agent
from ._types import Query, Result, Status, normalize_doi

DOI_RESOLVER = "https://doi.org/"

CROSSREF_API = "https://api.crossref.org"
OPENALEX_API = "https://api.openalex.org"
DATACITE_API = "https://api.datacite.org"

TITLE_MATCH_THRESHOLD = 0.60
TITLE_SEARCH_RESULTS = 5

__all__ = ["USER_AGENT", "redact", "CrossrefSource", "OpenAlexSource", "DataCiteSource"]


def _doi_url(doi: str | None) -> str | None:
    return f"{DOI_RESOLVER}{doi}" if doi else None


def _quote_doi(doi: str) -> str:
    return quote(doi, safe="/:()")


# --------------------------------------------------------------------------
# Shared HTTP behaviour
# --------------------------------------------------------------------------


class _HttpSource:
    """Common plumbing: session, headers, throttling, locks and error mapping."""

    name: str = ""
    order: int = 0
    required_env: tuple[str, ...] = ()
    _lock_on_429: frozenset[str] = frozenset()

    def __init__(
        self,
        *,
        session: Any | None = None,
        contact_email: str | None = None,
        timeout: float = 30.0,
        min_interval: float = 0.0,
    ) -> None:
        if contact_email is None:
            contact_email = os.environ.get("CONTACT_EMAIL")
        self._contact_email = (contact_email or "").strip() or None
        self._session = session
        self._timeout = timeout
        self._min_interval = max(0.0, float(min_interval))
        self._last_request: float | None = None
        self._locks: dict[str, str] = {}
        self._memo: dict[tuple, tuple[str, Any]] = {}  # answers remembered for this run
        self._pending: dict[tuple, tuple[str, Any]] | None = None

    # -- protocol -----------------------------------------------------------

    def available(self) -> bool:
        return True

    def fetch(self, query: Query) -> Result:
        self._pending = {}
        try:
            result = self._fetch(query)
        except Exception as exc:  # an adapter must never take the run down
            result = self._error(f"adapter error: {type(exc).__name__}: {self._clean(str(exc))}"[:300])
        if result.status is not Status.ERROR:  # an invalid answer is never remembered
            self._memo.update(self._pending)
        self._pending = None
        return result

    def _fetch(self, query: Query) -> Result:  # pragma: no cover - overridden
        raise NotImplementedError

    # -- helpers --------------------------------------------------------------

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

    def _headers(self) -> dict[str, str]:
        return {"User-Agent": user_agent(self._contact_email), "Accept": "application/json"}

    def _params(self) -> dict[str, str]:
        return {}

    def _get_session(self) -> Any:
        if self._session is None:
            self._session = requests.Session()
        return self._session

    def _throttle(self) -> None:
        if self._min_interval and self._last_request is not None:
            wait = self._min_interval - (time.monotonic() - self._last_request)
            if wait > 0:
                time.sleep(wait)
        self._last_request = time.monotonic()

    def _remember(self, key: tuple, kind: str, value: Any) -> None:
        if self._pending is not None:
            self._pending[key] = (kind, value)

    def _get_json(self, endpoint: str, url: str, params: dict[str, Any] | None = None) -> tuple[Any, Result | None]:
        """GET ``url`` and return ``(data, None)`` or ``(None, failure_result)``."""
        locked = self._locks.get(endpoint)
        if locked is not None:
            return None, self._error(locked)

        all_params = {**self._params(), **(params or {})}
        key = (endpoint, url, tuple(sorted((str(k), str(v)) for k, v in all_params.items())))
        remembered = self._memo.get(key)
        if remembered is not None:
            kind, value = remembered
            return (value, None) if kind == "data" else (None, self._absent(value))

        self._throttle()
        try:
            response = self._get_session().get(
                url, params=all_params, headers=self._headers(), timeout=self._timeout
            )
        except requests.RequestException as exc:
            detail = self._clean(str(exc))[:200]
            return None, self._error(f"network error ({type(exc).__name__}): {detail}" if detail
                                     else f"network error ({type(exc).__name__})")

        code = response.status_code
        if code == 404:
            self._remember(key, "absent", "not found")
            return None, self._absent("not found")
        if code in (401, 403):
            reason = f"HTTP {code} (access denied)"
            self._locks[endpoint] = reason
            return None, self._error(reason)
        if code == 429:
            if endpoint in self._lock_on_429:
                reason = "HTTP 429 (rate limited, request allowance used up)"
                self._locks[endpoint] = reason
            else:
                reason = "HTTP 429 (rate limited)"
            return None, self._error(reason)
        if code >= 500:
            return None, self._error(f"HTTP {code} (server error)")
        if code == 204:
            self._remember(key, "absent", "empty response")
            return None, self._absent("empty response")
        if not 200 <= code < 300:
            return None, self._error(f"HTTP {code} (unexpected response)")

        if not (response.content or b"").strip():
            self._remember(key, "absent", "empty response")
            return None, self._absent("empty response")
        try:
            data = response.json()
        except ValueError:
            return None, self._error("invalid response (not JSON)")
        if data is None or data == {} or data == []:
            self._remember(key, "absent", "empty response")
            return None, self._absent("empty response")
        self._remember(key, "data", data)
        return data, None

    def _check_doi(self, requested: str, record_doi: Any, build: Callable[[str | None], Result],
                   matched_by: str) -> Result:
        """Apply the DOI rule: a record whose own DOI differs is ``ABSENT``."""
        resolved = normalize_doi(record_doi) if isinstance(record_doi, str) else None
        if resolved is not None and resolved != requested:
            return self._absent("doi mismatch", resolved_doi=resolved, matched_by=matched_by)
        return build(resolved)


# --------------------------------------------------------------------------
# Crossref
# --------------------------------------------------------------------------


class CrossrefSource(_HttpSource):
    """Crossref REST API, looked up by DOI only."""

    name = "crossref"
    order = 2

    def __init__(self, *, session: Any | None = None, contact_email: str | None = None,
                 timeout: float = 30.0, min_interval: float = 0.0, base_url: str = CROSSREF_API) -> None:
        super().__init__(session=session, contact_email=contact_email, timeout=timeout,
                         min_interval=min_interval)
        self._base = base_url.rstrip("/")

    def _params(self) -> dict[str, str]:
        return {"mailto": self._contact_email} if self._contact_email else {}

    def _fetch(self, query: Query) -> Result:
        doi = normalize_doi(query.doi)
        if not doi:
            return self._skipped("needs a DOI")
        data, failure = self._get_json("works", f"{self._base}/works/{_quote_doi(doi)}")
        if failure is not None:
            return failure
        if not isinstance(data, dict):
            return self._error("invalid response (unexpected structure)")
        message = data.get("message")
        if message in (None, {}):
            return self._absent("empty response")
        if not isinstance(message, dict):
            return self._error("invalid response (unexpected structure)")

        def build(resolved: str | None) -> Result:
            return self._result(
                Status.FOUND,
                record=message,
                abstract=message.get("abstract") or None,
                public_url=_doi_url(resolved) or message.get("URL"),
                matched_by="doi",
                resolved_doi=resolved,
            )

        return self._check_doi(doi, message.get("DOI"), build, "doi")


# --------------------------------------------------------------------------
# OpenAlex
# --------------------------------------------------------------------------


def _openalex_abstract(index: Any) -> str | None:
    """Rebuild the plain abstract from OpenAlex's inverted word index."""
    if not isinstance(index, dict) or not index:
        return None
    placed: list[tuple[int, str]] = []
    for word, positions in index.items():
        for position in positions or ():
            placed.append((int(position), str(word)))
    if not placed:
        return None
    placed.sort(key=lambda item: item[0])
    return " ".join(word for _, word in placed)


class OpenAlexSource(_HttpSource):
    """OpenAlex works API: lookup by DOI, or title search when no DOI is given.

    An API key is optional; it is taken from ``api_key`` or the environment
    variable ``OPENALEX_API_KEY`` and sent as the ``api_key`` query
    parameter.
    """

    name = "openalex"
    order = 3
    _lock_on_429 = frozenset({"search"})

    def __init__(self, *, session: Any | None = None, contact_email: str | None = None,
                 api_key: str | None = None, timeout: float = 30.0, min_interval: float = 0.0,
                 base_url: str = OPENALEX_API) -> None:
        super().__init__(session=session, contact_email=contact_email, timeout=timeout,
                         min_interval=min_interval)
        if api_key is None:
            api_key = os.environ.get("OPENALEX_API_KEY")
        self._api_key = (api_key or "").strip() or None
        self._base = base_url.rstrip("/")

    def _secrets(self) -> tuple[str | None, ...]:
        return (self._api_key,)

    def _params(self) -> dict[str, str]:
        params: dict[str, str] = {}
        if self._contact_email:
            params["mailto"] = self._contact_email
        if self._api_key:
            params["api_key"] = self._api_key
        return params

    def _found(self, work: dict[str, Any], resolved: str | None, matched_by: str) -> Result:
        return self._result(
            Status.FOUND,
            record=work,
            abstract=_openalex_abstract(work.get("abstract_inverted_index")),
            public_url=_doi_url(resolved) or work.get("id"),
            matched_by=matched_by,
            resolved_doi=resolved,
        )

    def _fetch(self, query: Query) -> Result:
        doi = normalize_doi(query.doi)
        if doi:
            return self._by_doi(doi)
        if query.title and query.title.strip():
            return self._by_title(query.title, query.year)
        return self._skipped("needs a DOI or a title")

    def _by_doi(self, doi: str) -> Result:
        data, failure = self._get_json("work", f"{self._base}/works/doi:{_quote_doi(doi)}")
        if failure is not None:
            return failure
        if not isinstance(data, dict):
            return self._error("invalid response (unexpected structure)")
        return self._check_doi(doi, data.get("doi"), lambda r: self._found(data, r, "doi"), "doi")

    def _by_title(self, title: str, year: Any) -> Result:
        wanted = clean_latex(title)
        # Commas and bars are filter syntax in OpenAlex; they carry no meaning in a title search.
        search_text = re.sub(r"\s+", " ", re.sub(r"[,|]", " ", wanted)).strip()
        if not search_text:
            return self._skipped("needs a DOI or a title")
        filters = [f"title.search:{search_text}"]
        year_match = re.search(r"\d{4}", str(year)) if year is not None else None
        if year_match:
            filters.append(f"publication_year:{year_match.group(0)}")
        data, failure = self._get_json(
            "search",
            f"{self._base}/works",
            {"filter": ",".join(filters), "per_page": TITLE_SEARCH_RESULTS},
        )
        if failure is not None:
            return failure
        if not isinstance(data, dict) or not isinstance(data.get("results", []), list):
            return self._error("invalid response (unexpected structure)")
        best: dict[str, Any] | None = None
        best_ratio = 0.0
        for work in data.get("results", [])[:TITLE_SEARCH_RESULTS]:
            if not isinstance(work, dict):
                continue
            ratio = title_similarity(wanted, str(work.get("title") or ""))
            if ratio > best_ratio:
                best, best_ratio = work, ratio
        if best is None or best_ratio < TITLE_MATCH_THRESHOLD:
            return self._absent("no title match")
        doi = best.get("doi")
        return self._found(best, normalize_doi(doi) if isinstance(doi, str) else None, "title")


# --------------------------------------------------------------------------
# DataCite
# --------------------------------------------------------------------------

_ARXIV_ID_RE = re.compile(r"^(?:arxiv:)?\s*(.+?)(?:v\d+)?$", re.IGNORECASE)


def arxiv_doi(arxiv_id: str) -> str | None:
    """DataCite DOI registered for an arXiv identifier (version suffix dropped)."""
    match = _ARXIV_ID_RE.match(arxiv_id.strip())
    if not match or not match.group(1).strip():
        return None
    return f"10.48550/arxiv.{match.group(1).strip().lower()}"


def _datacite_abstract(attributes: dict[str, Any]) -> str | None:
    for item in attributes.get("descriptions") or ():
        if isinstance(item, dict) and item.get("descriptionType") == "Abstract":
            text = item.get("description")
            if isinstance(text, str) and text.strip():
                return text
    return None


class DataCiteSource(_HttpSource):
    """DataCite REST API, looked up by DOI or by arXiv identifier."""

    name = "datacite"
    order = 4

    def __init__(self, *, session: Any | None = None, contact_email: str | None = None,
                 timeout: float = 30.0, min_interval: float = 0.0, base_url: str = DATACITE_API) -> None:
        super().__init__(session=session, contact_email=contact_email, timeout=timeout,
                         min_interval=min_interval)
        self._base = base_url.rstrip("/")

    def _fetch(self, query: Query) -> Result:
        doi = normalize_doi(query.doi)
        matched_by = "doi"
        if not doi and query.arxiv_id:
            doi = arxiv_doi(query.arxiv_id)
            matched_by = "arxiv_id"
        if not doi:
            return self._skipped("needs a DOI or an arXiv identifier")
        data, failure = self._get_json("dois", f"{self._base}/dois/{_quote_doi(doi)}")
        if failure is not None:
            return failure
        if not isinstance(data, dict):
            return self._error("invalid response (unexpected structure)")
        record = data.get("data")
        if record in (None, {}):
            return self._absent("empty response")
        if not isinstance(record, dict):
            return self._error("invalid response (unexpected structure)")
        attributes = record.get("attributes") or {}
        if not isinstance(attributes, dict):
            return self._error("invalid response (unexpected structure)")

        def build(resolved: str | None) -> Result:
            return self._result(
                Status.FOUND,
                record=record,
                abstract=_datacite_abstract(attributes),
                public_url=_doi_url(resolved),
                matched_by=matched_by,
                resolved_doi=resolved,
            )

        return self._check_doi(doi, attributes.get("doi") or record.get("id"), build, matched_by)


# --------------------------------------------------------------------------
# Comparable fields per source (used by the checker core, not by consumers)
# --------------------------------------------------------------------------


def _first_text(values: Any) -> str:
    if isinstance(values, list):
        for value in values:
            if isinstance(value, str) and value.strip():
                return value
        return ""
    return values if isinstance(values, str) else ""


def crossref_fields(message: dict[str, Any]) -> dict[str, Any]:
    fields: dict[str, Any] = {}
    title = _first_text(message.get("title"))
    if title:
        fields["title"] = title
    names = []
    for person in message.get("author") or ():
        family = (person or {}).get("family", "")
        given = (person or {}).get("given", "")
        if family:
            names.append(f"{family}, {given}" if given else family)
    if names:
        fields["author_names"] = names
    parts = (message.get("issued") or {}).get("date-parts") or [[]]
    if parts and parts[0] and parts[0][0] is not None:
        fields["year"] = str(parts[0][0])
    journal = _first_text(message.get("container-title"))
    if journal:
        fields["journal"] = journal
    for key, target in (("volume", "volume"), ("issue", "issue"), ("page", "pages")):
        if message.get(key):
            fields[target] = str(message[key])
    return fields


def openalex_fields(work: dict[str, Any]) -> dict[str, Any]:
    fields: dict[str, Any] = {}
    if work.get("title"):
        fields["title"] = work["title"]
    names = [
        ((a or {}).get("author") or {}).get("display_name", "")
        for a in work.get("authorships") or ()
    ]
    names = [n for n in names if n]
    if names:
        fields["author_names"] = names
    if work.get("publication_year"):
        fields["year"] = str(work["publication_year"])
    venue = ((work.get("primary_location") or {}).get("source") or {}).get("display_name")
    if venue:
        fields["journal"] = venue
    biblio = work.get("biblio") or {}
    if biblio.get("volume"):
        fields["volume"] = str(biblio["volume"])
    if biblio.get("issue"):
        fields["issue"] = str(biblio["issue"])
    first, last = biblio.get("first_page") or "", biblio.get("last_page") or ""
    if first:
        fields["pages"] = f"{first}-{last}" if last and last != first else str(first)
    return fields


def datacite_fields(record: dict[str, Any]) -> dict[str, Any]:
    attributes = record.get("attributes") or {}
    fields: dict[str, Any] = {}
    titles = attributes.get("titles") or []
    main = [t.get("title") for t in titles if isinstance(t, dict) and not t.get("titleType") and t.get("title")]
    title = _first_text(main) or _first_text([t.get("title") for t in titles if isinstance(t, dict)])
    if title:
        fields["title"] = title
    names = []
    for creator in attributes.get("creators") or ():
        creator = creator or {}
        family, given = creator.get("familyName", ""), creator.get("givenName", "")
        if family:
            names.append(f"{family}, {given}" if given else family)
        elif creator.get("name"):
            names.append(creator["name"])
    if names:
        fields["author_names"] = names
    if attributes.get("publicationYear"):
        fields["year"] = str(attributes["publicationYear"])
    container = attributes.get("container") or {}
    if container.get("title"):
        fields["journal"] = container["title"]
    if container.get("volume"):
        fields["volume"] = str(container["volume"])
    if container.get("issue"):
        fields["issue"] = str(container["issue"])
    first, last = container.get("firstPage") or "", container.get("lastPage") or ""
    if first:
        fields["pages"] = f"{first}-{last}" if last and last != first else str(first)
    return fields


FIELD_EXTRACTORS: dict[str, Callable[[dict[str, Any]], dict[str, Any]]] = {
    "crossref": crossref_fields,
    "openalex": openalex_fields,
    "datacite": datacite_fields,
}
