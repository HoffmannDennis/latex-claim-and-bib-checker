"""The reference check: citation consistency and per-entry metadata verification.

With an abstract database, an entry whose DOI has stored metadata (see
:func:`._db.decode_metadata`) is first compared with that metadata. If at
least one field is compared and every field agrees, the entry is ``MATCH``
with origin ``DB`` and no request is sent. Otherwise the entry is checked
live as without a database, and the live result decides (origin ``API``).
"""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Callable, Sequence

from ._compare import FieldCheck, compare_entry, is_major
from ._db import AbstractDb
from ._lookup import comparable_entry, entry_identifiers
from ._metadata_sources import FIELD_EXTRACTORS
from ._types import Query, Result, Source, Status

SEPARATOR = " · "  # between several reasons


class EntryStatus(StrEnum):
    """Verdict for one bibliography entry.

    ``MATCH``: at least one field was compared and none differs.
    ``MISMATCH_MINOR``: only formatting-level differences (for example
    diacritics, a surname particle or a year that is off by one).
    ``MISMATCH_MAJOR``: at least one substantive difference.
    ``NOT_FOUND_IN_ENABLED_SOURCES``: every applicable enabled source
    answered and none has a matching record.
    ``SOURCE_ERROR``: no record was obtained and at least one source failed
    to answer.
    ``NOT_CHECKED``: no comparison was possible; ``reason`` says why.
    """

    MATCH = "MATCH"
    MISMATCH_MINOR = "MISMATCH_MINOR"
    MISMATCH_MAJOR = "MISMATCH_MAJOR"
    NOT_FOUND_IN_ENABLED_SOURCES = "NOT_FOUND_IN_ENABLED_SOURCES"
    SOURCE_ERROR = "SOURCE_ERROR"
    NOT_CHECKED = "NOT_CHECKED"


REASON_NO_SOURCE = "no applicable source"
REASON_NO_FIELDS = "no comparable fields"


@dataclass
class ConsistencyResult:
    """Cited keys compared with the keys defined in the bibliography."""

    cited_keys: set[str]
    defined_keys: set[str]
    cited_but_undefined: set[str]
    defined_but_uncited: set[str]
    nocite_all: bool
    bib_format: str = "external"

    @property
    def is_consistent(self) -> bool:
        return not self.cited_but_undefined and not self.defined_but_uncited


def check_consistency(cited: set[str], defined: set[str], nocite_all: bool = False) -> ConsistencyResult:
    """Keys cited without a bibliography entry are errors; entries never
    cited are warnings, unless ``\\nocite{*}`` includes everything."""
    return ConsistencyResult(
        cited_keys=set(cited),
        defined_keys=set(defined),
        cited_but_undefined=set(cited) - set(defined),
        defined_but_uncited=set() if nocite_all else set(defined) - set(cited),
        nocite_all=nocite_all,
    )


@dataclass
class EntryResult:
    """Metadata verification outcome for one bibliography entry."""

    key: str
    entry_type: str
    bib_file: str
    doi: str | None
    status: EntryStatus = EntryStatus.NOT_CHECKED
    reason: str | None = None
    checks: list[FieldCheck] = field(default_factory=list)
    lookups: list[Result] = field(default_factory=list)
    origin: str | None = None  # "DB" (stored metadata) or "API" (live lookups)
    metadata_date: str | None = None  # retrieval time of stored metadata (origin DB only)
    stored_source: str | None = None  # source of the stored metadata (origin DB only)

    @property
    def mismatches(self) -> list[FieldCheck]:
        return [c for c in self.checks if not c.match]

    @property
    def used(self) -> list[Result]:
        """The lookups whose records were compared."""
        compared = {c.source for c in self.checks}
        return [r for r in self.lookups if r.status is Status.FOUND and r.source in compared]


def _entry_year(entry: dict[str, str]) -> int | None:
    for name in ("year", "date"):
        match = re.search(r"\d{4}", entry.get(name, "") or "")
        if match:
            return int(match.group(0))
    return None


def entry_status(checks: list[FieldCheck], lookups: list[Result]) -> tuple[EntryStatus, str | None]:
    """Aggregate all lookups and comparisons of one entry into a verdict."""
    if any(r.status is Status.FOUND for r in lookups):
        if not checks:
            return EntryStatus.NOT_CHECKED, REASON_NO_FIELDS
        mismatches = [c for c in checks if not c.match]
        if not mismatches:
            return EntryStatus.MATCH, None
        if any(is_major(c) for c in mismatches):
            return EntryStatus.MISMATCH_MAJOR, None
        return EntryStatus.MISMATCH_MINOR, None

    applicable = [r for r in lookups if r.status is not Status.SKIPPED]
    if not applicable:
        return EntryStatus.NOT_CHECKED, REASON_NO_SOURCE
    errors = [r for r in applicable if r.status is Status.ERROR]
    if errors:
        return EntryStatus.SOURCE_ERROR, SEPARATOR.join(f"{r.source}: {r.reason}" for r in errors)
    return EntryStatus.NOT_FOUND_IN_ENABLED_SOURCES, SEPARATOR.join(
        f"{r.source}: {r.reason}" for r in applicable
    )


def _compare_result(entry: dict[str, str], bib_doi: str | None, result: Result) -> list[FieldCheck]:
    extractor = FIELD_EXTRACTORS.get(result.source)
    if extractor is None or not isinstance(result.record, dict):
        return []
    checks = compare_entry(comparable_entry(entry), extractor(result.record), result.source)
    if result.matched_by == "title" and bib_doi and result.resolved_doi:
        checks.append(FieldCheck("doi", bib_doi, result.resolved_doi,
                                 result.resolved_doi == bib_doi, result.source))
    return checks


def verify_entry(
    entry: dict[str, str],
    sources: Sequence[Source],
    *,
    title_search: Source | None = None,
    on_error: Callable[[Result], None] | None = None,
) -> tuple[list[FieldCheck], list[Result]]:
    """Look an entry up in ``sources`` (in the given order) and compare it.

    Identifier lookups come first. The search stops at the first record
    that yields at least one compared field. A title search through
    ``title_search`` follows only when nothing was found and the entry has
    no DOI or every DOI lookup came back ``ABSENT``.
    """
    bib_doi, arxiv_id = entry_identifiers(entry)
    title = (entry.get("title") or "").strip() or None
    year = _entry_year(entry)
    checks: list[FieldCheck] = []
    lookups: list[Result] = []

    def run(source: Source, query: Query) -> Result:
        try:
            result = source.fetch(query)
        except Exception as exc:  # a third-party source must not end the run
            result = Result(Status.ERROR, None, None, getattr(source, "name", "?"), None,
                            f"adapter error: {type(exc).__name__}")
        lookups.append(result)
        if result.status is Status.ERROR and on_error is not None:
            on_error(result)
        if result.status is Status.FOUND:
            checks.extend(_compare_result(entry, bib_doi, result))
        return result

    query = Query(doi=bib_doi, arxiv_id=arxiv_id)
    for source in sources:
        run(source, query)
        if checks:
            return checks, lookups

    found = any(r.status is Status.FOUND for r in lookups)
    if found or title_search is None or not title:
        return checks, lookups
    doi_answers = [r for r in lookups if r.status is not Status.SKIPPED]
    if bib_doi is None or (doi_answers and all(r.status is Status.ABSENT for r in doi_answers)):
        run(title_search, Query(doi=None, arxiv_id=None, title=title, year=year))
    return checks, lookups


def stored_checks(entry: dict[str, str], db: AbstractDb | None) -> tuple[list[FieldCheck], Any]:
    """Compare ``entry`` with the metadata stored for its DOI: ``(checks, stored)``."""
    doi, _ = entry_identifiers(entry)
    if db is None or not doi:
        return [], None
    stored = db.metadata(doi)
    if stored is None:
        return [], None
    return compare_entry(comparable_entry(entry), stored.fields, stored.source), stored


def verify_entries(
    entries: Sequence[tuple[str, dict[str, str]]],
    sources: Sequence[Source] | None,
    *,
    progress: bool = True,
    db: AbstractDb | None = None,
) -> list[EntryResult]:
    """Verify ``(bib_file_name, entry)`` pairs.

    ``db`` is an optional abstract database whose stored metadata can
    confirm an entry without a request (see the module docstring).
    """
    sources = list(sources or [])
    title_search = next((s for s in sources if s.name == "openalex"), None)
    results: list[EntryResult] = []
    total = len(entries)

    for index, (bib_file, entry) in enumerate(entries, 1):
        result = EntryResult(
            key=entry.get("ID", "?"),
            entry_type=entry.get("ENTRYTYPE", "?"),
            bib_file=bib_file,
            doi=(entry.get("doi") or "").strip() or None,
        )
        def report_error(r: Result, key: str = result.key) -> None:
            if progress:
                print(f"    {key}: {r.source}: {r.reason}", file=sys.stderr)

        checks, stored = stored_checks(entry, db)
        if checks and all(c.match for c in checks):
            result.checks, result.status = checks, EntryStatus.MATCH
            result.origin, result.metadata_date, result.stored_source = "DB", stored.fetched_at, stored.source
        else:
            result.checks, result.lookups = verify_entry(
                entry, sources, title_search=title_search, on_error=report_error
            )
            result.status, result.reason = entry_status(result.checks, result.lookups)
            if result.lookups:
                result.origin = "API"
        if progress:
            print(f"  [{index:>{len(str(total))}}/{total}] {result.key}: {result.status}", file=sys.stderr)
        results.append(result)
    return results


def no_source_answered(results: Sequence[EntryResult]) -> bool:
    """True if lookups were attempted and every single one failed."""
    attempted = [r for e in results for r in e.lookups if r.status is not Status.SKIPPED]
    return bool(attempted) and all(r.status is Status.ERROR for r in attempted)


def compared_with(result: EntryResult) -> str:
    """The records the entry was compared with, as plain text."""
    if result.origin == "DB" and result.stored_source:
        return f"{result.stored_source} (doi, stored)"
    parts = []
    for r in result.used:
        parts.append(f"{r.source} ({r.matched_by})" if r.matched_by else r.source)
    return ", ".join(parts)


def differences(result: EntryResult) -> str:
    """All field differences as one text."""
    return SEPARATOR.join(
        f"{c.field_name} [{c.source}, {'major' if is_major(c) else 'minor'}]: "
        f"bib '{c.bib_value}' vs source '{c.source_value}'"
        for c in result.mismatches
    )


def as_dict(result: EntryResult) -> dict[str, Any]:
    used = result.used
    return {
        "key": result.key,
        "entry_type": result.entry_type,
        "bib_file": result.bib_file,
        "doi": result.doi,
        "status": result.status.value,
        "reason": result.reason,
        "compared_with": compared_with(result),
        "origin": result.origin,
        "metadata_date": result.metadata_date if result.origin == "DB" else None,
        "resolved_doi": next((r.resolved_doi for r in used if r.resolved_doi), None),
        "public_url": next((r.public_url for r in used if r.public_url), None),
        "differences": differences(result),
        "checks": [
            {
                "field": c.field_name,
                "bib_value": c.bib_value,
                "source_value": c.source_value,
                "match": c.match,
                "severity": None if c.match else ("major" if is_major(c) else "minor"),
                "source": c.source,
            }
            for c in result.checks
        ],
        "lookups": [
            {
                "source": r.source,
                "status": r.status.value,
                "reason": r.reason,
                "matched_by": r.matched_by,
                "resolved_doi": r.resolved_doi,
                "public_url": r.public_url,
            }
            for r in result.lookups
        ],
    }
