"""Data types shared by every metadata source.

The objects defined here form the stable interface between the checker core
and the individual sources (the three built-in adapters as well as sources
provided by other packages).
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Protocol, runtime_checkable


class Status(StrEnum):
    """Outcome of one lookup in one source.

    ``FOUND``: the source returned a record for the query.
    ``ABSENT``: the source answered, but has no (matching) record
    (HTTP 404, an empty answer, no title match or a DOI mismatch).
    ``ERROR``: the source could not answer (network problem, rate limit,
    server error, unusable response or an exception inside the adapter).
    ``SKIPPED``: the source cannot work with the fields given in the query.
    """

    FOUND = "FOUND"
    ABSENT = "ABSENT"
    ERROR = "ERROR"
    SKIPPED = "SKIPPED"


@dataclass(frozen=True)
class Query:
    """What to look up. Every field is optional.

    A source uses the fields it understands and returns ``SKIPPED`` when none
    of them is usable. ``year`` only narrows a title search.
    """

    doi: str | None = None
    arxiv_id: str | None = None
    title: str | None = None
    year: int | None = None


@dataclass
class Result:
    """Answer of one source to one :class:`Query`.

    Attributes:
        status: Outcome of the lookup.
        record: The raw record returned by the source (``FOUND`` only).
        abstract: The abstract text exactly as delivered by the source, or
            ``None``. No markup is removed here.
        source: Name of the source that produced this result.
        public_url: Public DOI or landing page of the record
            (e.g. ``https://doi.org/<doi>``); never the request URL.
        reason: Short explanation for ``ABSENT``, ``ERROR`` and ``SKIPPED``.
        matched_by: How the record was found: ``"doi"``, ``"arxiv_id"``,
            ``"title"`` or ``None``.
        resolved_doi: The DOI that the source itself states for the returned
            record, normalised with :func:`normalize_doi`; ``None`` if the
            record carries no DOI. It is never copied from the query.
    """

    status: Status
    record: dict[str, Any] | None
    abstract: str | None
    source: str
    public_url: str | None
    reason: str | None
    matched_by: str | None = None
    resolved_doi: str | None = None


@runtime_checkable
class Source(Protocol):
    """Interface every metadata source implements.

    ``name`` identifies the source in results and reports; ``order`` is a
    sort key for consumers that rank sources by it (ascending, ties broken
    by ``name``). ``available()`` reports whether the source can be used in
    the current environment. ``fetch()`` never raises for expected failures;
    it reports them through :class:`Result`.

    A source may additionally define ``required_env: tuple[str, ...]`` with
    the names of environment variables it needs. Consumers read it with
    ``getattr(source, "required_env", ())``.
    """

    name: str
    order: int

    def available(self) -> bool: ...

    def fetch(self, query: Query) -> Result: ...


_DOI_PREFIXES = (
    "https://doi.org/",
    "http://doi.org/",
    "https://dx.doi.org/",
    "http://dx.doi.org/",
    "doi.org/",
    "doi:",
)


def normalize_doi(doi: str | None) -> str | None:
    """Return a DOI in canonical comparison form, or ``None`` if empty.

    The DOI is lower-cased, surrounding whitespace is removed, and a leading
    resolver URL (``https://doi.org/``, ``http://dx.doi.org/`` ...) or
    ``doi:`` prefix is dropped, as is a trailing slash.
    """
    if doi is None:
        return None
    text = str(doi).strip().lower()
    changed = True
    while changed:
        changed = False
        for prefix in _DOI_PREFIXES:
            if text.startswith(prefix):
                text = text[len(prefix):].strip()
                changed = True
    text = text.rstrip("/")
    return text or None
