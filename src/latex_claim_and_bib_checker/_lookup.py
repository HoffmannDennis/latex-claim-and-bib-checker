"""Abstract lookup for every cited bibliography entry.

For each cited key (in order of first citation) the lookup runs:

1. the optional abstract database, by the entry's DOI;
2. every active source in (order, name) order with
   ``Query(doi, arxiv_id)``, stopping at the first usable abstract;
3. the OpenAlex title search with ``Query(title, year)``, only if the entry
   has no DOI or every DOI lookup came back ``ABSENT`` (a record found by the
   DOI, even without a usable abstract, and a failed lookup both prevent it).

A newly found abstract is added to the database only when the DOI stated by
the source for its record equals the entry's DOI. When the file has a
``raw_json`` column, the new row carries the metadata of a Crossref,
OpenAlex or DataCite record of this run that was found by the same DOI and
whose title agrees with the entry (the record the reference check compared
first, otherwise the record that delivered the abstract).

A stored abstract whose stored metadata names another title is not used,
and the sources are asked instead. An abstract found by identifier is used
only if the title of the record that delivered it (when it has one) agrees
with the entry's title (see :func:`titles_agree`). Otherwise the next source
is asked. A title-search hit must have a title that agrees, must not state
another DOI than the entry and must not differ substantively from the entry
in the reference check's comparison (for example in authors or year).
A source that reports a used-up request allowance is not queried again in
the run.
"""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass
from typing import Any, Callable

from ._abstract_sources import normalize_arxiv_id
from ._compare import compare_entry, is_major
from ._db import AbstractDb, decode_metadata, encode_metadata
from ._http import redact
from ._metadata_sources import FIELD_EXTRACTORS, datacite_fields
from ._plugins import SourceInfo
from ._text import plain_latex, titles_agree, usable_abstract
from ._types import Query, Result, Status, normalize_doi

FOUND = "found"
ABSENT = "absent"
SOURCE_ERROR = "source_error"
ALLOWANCE_USED_UP = "request allowance used up"
STORED_TITLE_MISMATCH = "stored record title does not match the entry"
SEPARATOR = " · "  # between several reasons


@dataclass
class Evidence:
    """What the lookup found for one bibliography entry."""

    lookup_status: str  # found | absent | source_error
    reason: str | None = None
    abstract: str | None = None  # cleaned text, usable by the text rule
    source: str | None = None  # Result.source, or the provider stored in the database
    origin: str | None = None  # "DB" or "API"
    match: str | None = None  # "doi", "arxiv_id" or "title"
    public_url: str | None = None
    resolved_doi: str | None = None


@dataclass
class LookupStats:
    db_hits: int = 0
    answered: int = 0  # live results that were FOUND or ABSENT
    errors: int = 0  # live results that were ERROR


def entry_identifiers(entry: dict[str, str]) -> tuple[str | None, str | None]:
    """``(normalised DOI, arXiv identifier)`` of a BibTeX entry."""
    raw_doi = (entry.get("doi") or "").replace("\\_", "_").replace("{", "").replace("}", "")
    doi = normalize_doi(raw_doi) if raw_doi.strip() else None
    arxiv_id = None
    prefix = (entry.get("archiveprefix") or entry.get("eprinttype") or "").strip().lower()
    if prefix == "arxiv" and entry.get("eprint"):
        arxiv_id = normalize_arxiv_id(entry["eprint"])
    return doi, arxiv_id


def entry_year(entry: dict[str, str]) -> int | None:
    for field in ("year", "date"):
        match = re.search(r"\d{4}", entry.get(field) or "")
        if match:
            return int(match.group(0))
    return None


def comparable_entry(entry: dict[str, str]) -> dict[str, str]:
    """Map biblatex field names onto the BibTeX names used for comparison."""
    fields = dict(entry)
    if not fields.get("journal") and fields.get("journaltitle"):
        fields["journal"] = fields["journaltitle"]
    if not fields.get("year") and fields.get("date"):
        year = entry_year({"date": fields["date"]})
        if year is not None:
            fields["year"] = str(year)
    return fields


def _other_work(entry: dict[str, str], doi: str | None, result: Result) -> bool:
    """True if a title-search hit is not the entry's work.

    That is the case when the hit states another DOI than the entry, or when
    the reference check would grade it ``MISMATCH_MAJOR``.
    """
    if doi and result.resolved_doi and normalize_doi(result.resolved_doi) != doi:
        return True
    extract = FIELD_EXTRACTORS.get(result.source or "")
    if extract is None or not isinstance(result.record, dict):
        return False
    checks = compare_entry(comparable_entry(entry), extract(result.record), result.source)
    return any(not c.match and is_major(c) for c in checks)


def _record_title(record: Any) -> str | None:
    """The record's top-level ``title`` (first non-empty string of a list), if any."""
    value = record.get("title") if isinstance(record, dict) else None
    if isinstance(value, list):
        value = next((v for v in value if isinstance(v, str) and v.strip()), None)
    return value if isinstance(value, str) and value.strip() else None


def _abstract_title(result: Result, info: SourceInfo) -> str | None:
    """Title of the record that delivered the abstract.

    For the built-in DataCite source it comes from the record's titles (the
    same extraction the reference check uses). Every other source provides
    it as the record's top-level ``title``.
    """
    if info.plugin is None and info.name == "datacite" and isinstance(result.record, dict):
        title = datacite_fields(result.record).get("title")
        return title if isinstance(title, str) and title.strip() else None
    return _record_title(result.record)


def _coerce(result: Any, info: SourceInfo) -> Result:
    """Turn whatever a source returned into a valid ``Result`` (or an ERROR)."""
    if not isinstance(result, Result):
        return Result(Status.ERROR, None, None, info.name, None, "invalid result from source")
    try:
        status = Status(str(result.status))
    except ValueError:
        return Result(Status.ERROR, None, None, info.name, None, "invalid result status from source")
    if status is not result.status:
        result.status = status
    if not isinstance(result.source, str) or not result.source.strip():
        result.source = info.name
    return result


class Lookup:
    """Runs the lookup for all entries of one document."""

    def __init__(self, sources: list[SourceInfo], db: AbstractDb | None = None,
                 warn: Callable[[str], None] | None = None,
                 records: dict[str, list[Result]] | None = None) -> None:
        self._sources = sources
        self._db = db
        self._records = records or {}  # per key: the records the reference check compared
        self._warn = warn or (lambda message: print(message, file=sys.stderr, flush=True))
        self._openalex = next((s for s in sources if s.name == "openalex" and s.plugin is None), None)
        self._title_search_stopped = False
        self._exhausted: set[str] = set()  # sources whose request allowance is used up
        self.stats = LookupStats()

    # -- one source call ------------------------------------------------------

    def _call(self, info: SourceInfo, query: Query, key: str) -> Result:
        try:
            result = _coerce(info.source.fetch(query), info)
        except Exception as exc:  # a faulty source must not end the run
            result = Result(Status.ERROR, None, None, info.name, None,
                            redact(f"source raised {type(exc).__name__}: {exc}")[:300])
        if result.reason:
            result.reason = redact(str(result.reason))
        if result.status is Status.ERROR:
            self.stats.errors += 1
            self._warn(f"note: {result.source}: {result.reason or 'error'} ({key}), trying the next source")
        elif result.status in (Status.FOUND, Status.ABSENT):
            self.stats.answered += 1
        return result

    # -- one entry -----------------------------------------------------------

    def lookup(self, key: str, entry: dict[str, str] | None) -> Evidence:
        if entry is None:
            return Evidence(ABSENT, "no bibliography entry for this key")
        doi, arxiv_id = entry_identifiers(entry)
        title = plain_latex(entry.get("title"))
        notes: list[str] = []
        errors: list[str] = []

        if self._db is not None and doi:
            row = self._db.row(doi)
            if row is not None:
                text = usable_abstract(row.abstract)
                if text is not None:
                    stored = decode_metadata(row.raw_json, doi)
                    if stored is not None and not titles_agree(title, stored.fields.get("title")):
                        notes.append(f"database: {STORED_TITLE_MISMATCH}")
                    else:
                        self.stats.db_hits += 1
                        return Evidence(FOUND, None, text, row.provider, "DB", "doi",
                                        f"https://doi.org/{doi}", doi)

        doi_lookups_absent = True

        if doi or arxiv_id:
            query = Query(doi=doi, arxiv_id=arxiv_id)
            for info in self._sources:
                if info.name in self._exhausted:
                    errors.append(f"{info.name}: not queried ({ALLOWANCE_USED_UP} earlier in this run)")
                    if doi:
                        doi_lookups_absent = False  # as for the ERROR it stands for
                    continue
                result = self._call(info, query, key)
                if result.status is Status.ERROR and ALLOWANCE_USED_UP in (result.reason or ""):
                    self._exhausted.add(info.name)
                    self._warn(f"note: {info.name}: {ALLOWANCE_USED_UP}, not queried again in this run")
                evidence = self._evaluate(result, query, notes, errors, info, title)
                if evidence is not None:
                    self._store(key, doi, title, result, info, evidence)
                    return self._with_notes(evidence, notes)
                if doi and result.status in (Status.FOUND, Status.ERROR):
                    doi_lookups_absent = False

        if title and (doi is None or doi_lookups_absent) and self._openalex is not None \
                and not self._title_search_stopped:
            query = Query(title=title, year=entry_year(entry))
            result = self._call(self._openalex, query, key)
            if result.status is Status.ERROR and "429" in (result.reason or ""):
                self._title_search_stopped = True
            if result.status is Status.FOUND and _other_work(entry, doi, result):
                notes.append(f"{result.source}: title search hit is another work")
                evidence = None
            else:
                evidence = self._evaluate(result, query, notes, errors, self._openalex, title)
            if evidence is not None:
                self._store(key, doi, title, result, self._openalex, evidence)
                return self._with_notes(evidence, notes)

        if errors:
            return Evidence(SOURCE_ERROR, SEPARATOR.join(errors + [n for n in notes if n.startswith("database:")]))
        if notes:
            return Evidence(ABSENT, SEPARATOR.join(notes))
        if not (doi or arxiv_id or title):
            return Evidence(ABSENT, "no DOI, arXiv identifier or title")
        return Evidence(ABSENT, "no applicable source")

    @staticmethod
    def _with_notes(evidence: Evidence, notes: list[str]) -> Evidence:
        """Keep the reason why a stored abstract was not used on the live evidence."""
        stored = [n for n in notes if n.startswith("database:")]
        if stored:
            evidence.reason = SEPARATOR.join(stored)
        return evidence

    def _evaluate(self, result: Result, query: Query, notes: list[str], errors: list[str],
                  info: SourceInfo, entry_title: str | None = None) -> Evidence | None:
        """Evidence from a usable ``result``, else ``None`` with a note or error recorded.

        With ``entry_title`` an abstract is used only if the title of its own
        record agrees with it. A record without a title passes only when it
        was found by identifier.
        """
        if result.status is Status.FOUND:
            text = usable_abstract(result.abstract)
            record_title = _abstract_title(result, info)
            if text is None:
                notes.append(f"{result.source}: no usable abstract")
            elif entry_title and (not titles_agree(entry_title, record_title)
                                  or (query.title and not record_title)):
                notes.append(f"{result.source}: record title does not match the entry")
            else:
                match = result.matched_by or ("doi" if query.doi else "arxiv_id" if query.arxiv_id else "title")
                return Evidence(FOUND, None, text, result.source, "API", match,
                                result.public_url, normalize_doi(result.resolved_doi))
        elif result.status is Status.ABSENT:
            notes.append(f"{result.source}: {result.reason or 'not found'}")
        elif result.status is Status.ERROR:
            errors.append(f"{result.source}: {result.reason or 'error'}")
        return None

    def _metadata_for(self, key: str, bib_doi: str, title: str | None, result: Result,
                      info: SourceInfo) -> str | None:
        """Compact metadata for a new row (see the module docstring), or ``None``."""
        candidates = list(self._records.get(key) or ())
        if info.plugin is None:
            candidates.append(result)
        for record in candidates:
            extract = FIELD_EXTRACTORS.get(record.source or "")
            if extract is None or record.status is not Status.FOUND or record.matched_by != "doi":
                continue
            if normalize_doi(record.resolved_doi) != bib_doi or not isinstance(record.record, dict):
                continue
            fields = extract(record.record)
            # The record must show a title, and it must agree with the entry. A record
            # without a title proves nothing about the work and is not stored.
            if not title or not fields.get("title") or not titles_agree(title, fields.get("title")):
                continue
            encoded = encode_metadata(record.source, bib_doi, fields)
            if encoded is not None:
                return encoded
        return None

    def _store(self, key: str, bib_doi: str | None, title: str | None, result: Result, info: SourceInfo,
               evidence: Evidence) -> None:
        """Add a newly found abstract to the database if the DOIs agree."""
        if self._db is None or self._db.readonly or not bib_doi or not bib_doi.startswith("10."):
            return
        if normalize_doi(result.resolved_doi) != bib_doi or evidence.abstract is None:
            return
        raw_json = self._metadata_for(key, bib_doi, title, result, info) if self._db.has_metadata else None
        if not self._db.add(bib_doi, evidence.abstract, evidence.source or result.source, raw_json):
            self._warn("warning: the abstract database is locked by another process, continuing")
