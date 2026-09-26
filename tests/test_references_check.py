"""Per-entry status of the reference check."""

from __future__ import annotations

import pytest
import requests
from conftest import EXAMPLES, FakeHttp, crossref_work, make_response

from latex_claim_and_bib_checker import Result, Status
from latex_claim_and_bib_checker._compare import FieldCheck, _title_match, is_major
from latex_claim_and_bib_checker._metadata_sources import CrossrefSource
from latex_claim_and_bib_checker._references import EntryStatus, verify_entries
from latex_claim_and_bib_checker._text import titles_agree

ENTRY = {
    "ID": "vaswani2017",
    "ENTRYTYPE": "inproceedings",
    "doi": "10.5555/3295222.3295349",
    "title": "Attention Is All You Need",
    "author": "Vaswani, Ashish and Shazeer, Noam and Parmar, Niki",
    "year": "2017",
    "booktitle": "Advances in Neural Information Processing Systems",
    "volume": "30",
}


class Scripted:
    """A minimal Source that replays fixed results."""

    def __init__(self, name, *results):
        self.name = name
        self.order = 0
        self._results = list(results)

    def available(self):
        return True

    def fetch(self, query):
        status, reason, record = self._results.pop(0) if len(self._results) > 1 else self._results[0]
        return Result(status, record, None, self.name, None, reason)


def _run(entry, *sources):
    return verify_entries([("refs.bib", entry)], sources, progress=False)[0]


def test_match_requires_a_compared_field():
    crossref = CrossrefSource(session=FakeHttp().on("GET", "crossref", make_response(200, crossref_work(ENTRY["doi"]))))
    result = _run(ENTRY, crossref)
    assert result.status is EntryStatus.MATCH
    assert {c.field_name for c in result.checks} >= {"title", "author", "year", "volume"}
    bare = {"ID": "k", "ENTRYTYPE": "misc", "doi": ENTRY["doi"]}
    assert (_run(bare, crossref).status, _run(bare, crossref).reason) == (EntryStatus.NOT_CHECKED,
                                                                         "no comparable fields")


def test_formatting_differences_are_minor_and_substantive_ones_major():
    record = crossref_work(ENTRY["doi"])["message"]

    def status(**changes):
        return _run(dict(ENTRY, **changes), Scripted("crossref", (Status.FOUND, None, record))).status

    assert status(year="2018", author="Vaswàni, Ashish and Shazeer, Noam and Pármar, Niki") \
        is EntryStatus.MISMATCH_MINOR
    assert status(year="2015") is EntryStatus.MISMATCH_MAJOR
    assert status(title="A Different Paper Entirely") is EntryStatus.MISMATCH_MAJOR


@pytest.mark.parametrize("bib,record,agree", [
    ("Deep learning", "Deep learning \u2014 a review", True),
    ("Deep learning", "Deep learning---a review", True),
    ("Deep learning", "Deep learning - a review", True),
    ("Random forests", "Random forests (2001)", True),
    ("Scikit-learn", "Scikit-learn: Machine Learning in Python", True),
    ("Long Short-Term Memory", "Long short\u2010term memory", True),
    ("Empirical Asset Pricing via Machine Learning", "Empirical Asset Pricing via Machine Learning*", True),
    ("Long Short-Term Memory", "Long short-term memory in older adults", False),
    ("Deep learning", "Deep learning-based trading", False),
    ("Deep learning", "Deep learning for finance", False),
])
def test_a_subtitle_needs_an_explicit_separator(bib, record, agree):
    assert (_title_match(bib, record), titles_agree(bib, record)) == (agree, agree)


def test_abbreviated_journal_names_are_minor():
    def major(bib, source):
        return is_major(FieldCheck("journal", bib, source, False, "crossref"))

    assert not major("J. Financ. Econ.", "Journal of Financial Economics")
    assert not major("Rev. Financ. Stud.", "The Review of Financial Studies")
    assert major("Journal of Finance", "Journal of Financial Economics")
    record = dict(crossref_work(ENTRY["doi"])["message"], **{"container-title": ["Journal of Financial Economics"]})
    entry = dict(ENTRY, journal="J. Financ. Econ.")
    assert _run(entry, Scripted("crossref", (Status.FOUND, None, record))).status is EntryStatus.MISMATCH_MINOR


def test_mixed_absent_and_error_is_source_error_not_not_found():
    crossref = Scripted("crossref", (Status.ABSENT, "not found", None))
    openalex = Scripted("openalex", (Status.ERROR, "HTTP 503 (server error)", None))
    datacite = Scripted("datacite", (Status.ABSENT, "not found", None))
    result = _run(ENTRY, crossref, openalex, datacite)
    assert result.status is EntryStatus.SOURCE_ERROR
    assert result.reason == "openalex: HTTP 503 (server error)"


def test_title_hit_with_other_doi_is_flagged():
    crossref = Scripted("crossref", (Status.ABSENT, "not found", None))

    class TitleSearch(Scripted):
        def fetch(self, query):
            if query.doi:
                return Result(Status.ABSENT, None, None, "openalex", None, "not found")
            work = {"title": ENTRY["title"], "publication_year": 2017, "doi": "https://doi.org/10.1/other"}
            return Result(Status.FOUND, work, None, "openalex", "https://doi.org/10.1/other", None,
                          matched_by="title", resolved_doi="10.1/other")

    result = _run(ENTRY, crossref, TitleSearch("openalex"))
    assert result.status is EntryStatus.MISMATCH_MAJOR
    assert [c.field_name for c in result.mismatches] == ["doi"]


def test_network_down_and_429_give_source_error_exit_3_and_one_title_search(http, run):
    http.on("GET", "api.crossref.org", requests.ConnectionError("Failed to establish a new connection"))
    http.on("GET", "api.openalex.org", make_response(429))
    http.on("GET", "api.datacite.org", make_response(429))
    result = run(EXAMPLES / "references.bib")
    assert result.code == 3 and "no metadata source answered" in result.err
    assert {e["status"] for e in result.data["references"]["entries"]} == {"SOURCE_ERROR"}
    assert sum(1 for c in http.calls if c.url.endswith("api.openalex.org/works")) == 1  # title search locked
