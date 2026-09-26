"""The optional abstract database, the stored-metadata decoder and the wrong-work guard around it."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from latex_claim_and_bib_checker._db import decode_metadata
from conftest import (
    ABSTRACT_ALPHA,
    ABSTRACT_BETA,
    ABSTRACT_GAMMA,
    chat_completion,
    crossref_payload,
    datacite_payload,
    make_project,
    make_response,
    models_list,
    openalex_payload,
    s2_payload,
    verdict,
)

DOI = "10.1234/alpha"
TITLE = "Widget Markets"
FOREIGN_TITLE = "Sprocket Colour and Assembly Speed in Laboratory Settings"
TEX = ("\\begin{document}\nWidget indices predict returns \\citep{alpha}. "
       "Gadget standards lower costs \\citep{beta}.\n\\bibliography{refs}\n\\end{document}\n")
BIB = ("@article{alpha, title={Widget Markets}, year={2020}, doi={https://doi.org/10.1234/ALPHA}}\n"
       "@article{beta, title={Gadget Standards}, year={2021}, doi={10.1234/beta}}\n")
CREATE_WITH_METADATA = ("CREATE TABLE abstracts (doi TEXT PRIMARY KEY, abstract TEXT, provider TEXT NOT NULL, "
                        "outcome TEXT NOT NULL, fetched_at TEXT NOT NULL, raw_json TEXT)")
CREATE_MINIMAL = ("CREATE TABLE abstracts (doi TEXT PRIMARY KEY, abstract TEXT, provider TEXT NOT NULL, "
                  "outcome TEXT NOT NULL, fetched_at TEXT NOT NULL)")


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def rows(path: Path) -> list[tuple]:
    conn = sqlite3.connect(path)
    try:
        return conn.execute("SELECT * FROM abstracts ORDER BY doi").fetchall()
    finally:
        conn.close()


def master(path: Path) -> list[tuple]:
    conn = sqlite3.connect(path)
    try:
        return conn.execute("SELECT type, name, tbl_name, sql FROM sqlite_master ORDER BY name").fetchall()
    finally:
        conn.close()


def metadata_json(**overrides: Any) -> str:
    payload: dict[str, Any] = {"format": "latex-claim-and-bib-checker/metadata-1", "source": "crossref",
                               "matched_by": "doi", "doi": DOI, "fetched_at": "2026-09-01T10:00:00+00:00",
                               "fields": {"title": TITLE, "year": "2020"}}
    payload.update(overrides)
    return json.dumps(payload)


def route_both(http) -> None:
    http.on("GET", "api.crossref.org/works/10.1234/alpha",
            make_response(200, crossref_payload("10.1234/alpha", ABSTRACT_ALPHA, title=TITLE)))
    http.on("GET", "api.crossref.org/works/10.1234/beta",
            make_response(200, crossref_payload("10.1234/beta", ABSTRACT_BETA, title="Gadget Standards")))


def route_model(http) -> None:
    http.on("GET", "127.0.0.1:1234/v1/models", models_list("local-model"))
    http.on("POST", "127.0.0.1:1234/v1/chat/completions", chat_completion(verdict("PLAUSIBLE")))


@pytest.fixture
def project(tmp_path):
    return make_project(tmp_path / "doc", TEX, {"refs.bib": BIB})


# -- on/off ------------------------------------------------------------------------


def test_database_is_off_by_default(project, http, run, monkeypatch, tmp_path):
    def forbidden(*args, **kwargs):
        raise AssertionError("sqlite3.connect must not be called")

    monkeypatch.setattr(sqlite3, "connect", forbidden)
    route_both(http)
    before = sorted(tmp_path.rglob("*"))
    assert run(project, "--claim-check-only", sources=["crossref"], judge=False).code == 0
    assert sorted(tmp_path.rglob("*")) == before


# -- round trip --------------------------------------------------------------------


def test_round_trip_second_run_makes_no_request_and_shows_origin_db(project, http, run, tmp_path):
    db = tmp_path / "store.sqlite"
    route_both(http)
    route_model(http)
    first = run(project, "--abstract-db", db, sources=["crossref"])
    assert first.code == 0, first.err
    assert [(r[0], r[2], r[3]) for r in rows(db)] == [("10.1234/alpha", "crossref", "FOUND"),
                                                      ("10.1234/beta", "crossref", "FOUND")]
    http.calls.clear()
    second = run(project, "--abstract-db", db, sources=["crossref"])
    assert not [c for c in http.calls if "crossref" in c.url]  # neither part asks again
    assert {e["origin"] for e in second.entries.values()} == {"DB"}
    assert {r["evidence_origin"] for r in second.claims.values()} == {"DB"}
    assert "origin DB" in second.out and "store.sqlite" not in second.out


# -- existing files are never altered ---------------------------------------------


def test_existing_rows_and_schema_are_never_altered(project, http, run, tmp_path):
    db = tmp_path / "existing.sqlite"
    conn = sqlite3.connect(db, isolation_level=None)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript("CREATE TABLE abstracts (doi TEXT PRIMARY KEY, abstract TEXT, provider TEXT NOT NULL, "
                       "outcome TEXT NOT NULL, fetched_at TEXT NOT NULL, note TEXT, raw_json TEXT);\n"
                       "CREATE INDEX idx_abstracts_fetched_at ON abstracts(fetched_at);\n"
                       "CREATE TABLE notes (id INTEGER PRIMARY KEY, text TEXT NOT NULL);")
    conn.execute("INSERT INTO abstracts VALUES ('10.1234/alpha', ?, 'springer', 'FOUND', "
                 "'2026-01-01T00:00:00+00:00', 'checked', '{\"raw\": true}')", (ABSTRACT_ALPHA,))
    conn.execute("INSERT INTO abstracts VALUES ('10.1234/beta', NULL, 'core', 'ABSENT', "
                 "'2026-01-02T00:00:00+00:00', NULL, NULL)")
    conn.close()
    schema_before, rows_before = master(db), rows(db)
    route_both(http)
    result = run(project, "--claim-check-only", "--abstract-db", db, sources=["crossref"], judge=False)
    assert result.code == 0, result.err
    assert master(db) == schema_before and rows(db) == rows_before  # the ABSENT row is kept as it is
    assert result.claims[1]["evidence_origin"] == "DB" and result.claims[1]["evidence_source"] == "springer"
    assert result.claims[2]["evidence_origin"] == "API"  # a row that is not FOUND is not used


def test_file_without_raw_json_gets_abstracts_without_metadata(project, http, run, tmp_path):
    db = tmp_path / "minimal.sqlite"
    conn = sqlite3.connect(db)
    conn.execute(CREATE_MINIMAL)
    conn.close()
    route_both(http)
    assert run(project, "--claim-check-only", "--abstract-db", db, sources=["crossref"], judge=False).code == 0
    assert [r[:4] for r in rows(db)] == [("10.1234/alpha", ABSTRACT_ALPHA, "crossref", "FOUND"),
                                         ("10.1234/beta", ABSTRACT_BETA, "crossref", "FOUND")]
    assert master(db)[0][3] == CREATE_MINIMAL


@pytest.mark.parametrize("setup", ["other_columns", "not_sqlite"])
def test_incompatible_file_aborts_without_writing(project, http, run, tmp_path, setup):
    db = tmp_path / "other.sqlite"
    if setup == "not_sqlite":
        db.write_bytes(b"this is not a database file at all" * 10)
    else:
        conn = sqlite3.connect(db)
        conn.execute("CREATE TABLE abstracts (doi TEXT PRIMARY KEY, text TEXT)")
        conn.close()
    digest = sha256(db)
    result = run(project, "--claim-check-only", "--abstract-db", db, sources=["crossref"], judge=False)
    assert result.code == 2 and "not a compatible abstract database" in result.err
    assert sha256(db) == digest and http.calls == []
    assert not Path(f"{db}-wal").exists()


# -- --bib-check-only is read-only --------------------------------------------------


def test_bib_check_only_never_creates_or_changes_a_database(project, http, run, tmp_path):
    route_both(http)
    missing = tmp_path / "missing.sqlite"
    assert run(project, "--bib-check-only", "--abstract-db", missing).code == 0
    assert not missing.exists() and not Path(f"{missing}-wal").exists()
    # A file the tool created itself is in WAL mode: reading it must leave it byte-identical, without side files.
    own = tmp_path / "own.sqlite"
    route_model(http)
    assert run(project, "--abstract-db", own, sources=["crossref"]).code == 0
    digest = sha256(own)
    http.calls.clear()
    result = run(project, "--bib-check-only", "--abstract-db", own)
    assert result.code == 0 and http.calls == []
    assert {e["origin"] for e in result.entries.values()} == {"DB"}
    assert sha256(own) == digest
    assert not Path(f"{own}-wal").exists() and not Path(f"{own}-shm").exists()


# -- the write invariant --------------------------------------------------------------


def test_no_row_without_resolved_doi_or_for_another_doi(project, http, run, tmp_path):
    # alpha: S2 gives an abstract without a DOI. beta: only the title search finds a record, under another DOI.
    http.on("GET", "api.semanticscholar.org", lambda call: make_response(200, s2_payload(None, ABSTRACT_ALPHA))
            if "alpha" in call.url else make_response(404))
    http.on("GET", "api.openalex.org/works", lambda call: make_response(
        200, {"results": [openalex_payload("10.5555/other", ABSTRACT_BETA, title="Gadget Standards")]})
        if "filter" in call.params else make_response(404))
    db = tmp_path / "store.sqlite"
    result = run(project, "--claim-check-only", "--abstract-db", db, sources=["s2", "openalex"], judge=False)
    assert result.claims[1]["lookup_status"] == "found" and result.claims[1]["resolved_doi"] is None
    assert result.claims[2]["lookup_status"] == "absent"
    assert "openalex: title search hit is another work" in result.claims[2]["lookup_reason"]
    assert rows(db) == []


# -- the stored-metadata decoder ---------------------------------------------------

BAD_RAW = {
    "missing": None, "corrupt": "{not json", "foreign": json.dumps({"raw": True}),
    "unknown_version": metadata_json(format="latex-claim-and-bib-checker/metadata-2"),
    "bad_source": metadata_json(source="springer"), "bad_matched_by": metadata_json(matched_by="title"),
    "doi_other_row": metadata_json(doi="10.1234/other"), "naive_timestamp": metadata_json(fetched_at="2026-09-01"),
    "fields_not_object": metadata_json(fields=["title"]), "title_not_string": metadata_json(fields={"title": [1]}),
    "authors_not_strings": metadata_json(fields={"author_names": ["Doe, Jane", 7]}),
}


def test_decoder_rejects_unusable_raw_json_and_accepts_an_equivalent_doi():
    assert [case for case, raw in BAD_RAW.items() if decode_metadata(raw, DOI) is not None] == []
    decoded = decode_metadata(metadata_json(doi="https://doi.org/10.1234/ALPHA"), DOI)
    assert decoded is not None and decoded.fields == {"title": TITLE, "year": "2020"}


def test_unusable_stored_metadata_leaves_the_row_unchanged_and_checks_live(project, http, run, tmp_path):
    db = tmp_path / "a.sqlite"
    conn = sqlite3.connect(db)
    conn.execute(CREATE_WITH_METADATA)
    conn.execute("INSERT INTO abstracts VALUES (?, ?, 'crossref', 'FOUND', '2026-09-01', ?)",
                 (DOI, ABSTRACT_ALPHA, BAD_RAW["corrupt"]))
    conn.commit()
    conn.close()
    before = rows(db)
    route_both(http)
    result = run(project, "--bib-check-only", "--abstract-db", db)
    assert result.code == 0, result.err
    assert result.entries["alpha"]["origin"] == "API" and len(http.to("works/10.1234/alpha")) == 1
    assert rows(db) == before


# -- wrong-work guard ---------------------------------------------------------------


def test_db_abstract_with_foreign_stored_title_is_not_used(project, http, run, tmp_path):
    db = tmp_path / "a.sqlite"
    conn = sqlite3.connect(db)
    conn.execute(CREATE_WITH_METADATA)
    conn.execute("INSERT INTO abstracts VALUES (?, ?, 'crossref', 'FOUND', '2026-09-01', ?)",
                 (DOI, ABSTRACT_GAMMA, metadata_json(fields={"title": FOREIGN_TITLE})))
    conn.commit()
    conn.close()
    before = rows(db)
    route_both(http)
    result = run(project, "--claim-check-only", "--abstract-db", db, sources=["crossref"], judge=False)
    assert result.claims[1]["evidence_origin"] == "API"
    assert "stored record title does not match the entry" in result.claims[1]["lookup_reason"]
    assert rows(db)[0] == before[0]  # the alpha row is unchanged


@pytest.mark.parametrize("with_db", [False, True])
@pytest.mark.parametrize("title,foreign", [
    (TITLE, FOREIGN_TITLE),
    ("Long Short-Term Memory", "Short-term memory deficits in older adults"),  # overlapping words only
])
def test_wrong_work_abstract_is_skipped_and_reaches_no_report_row_or_model(http, run, tmp_path, with_db,
                                                                           title, foreign):
    # Crossref, asked first, holds a record of another work under the same DOI. DataCite holds the right one.
    project = make_project(tmp_path / "doc", TEX, {"refs.bib": BIB.replace(TITLE, title)})
    route_both(http)
    http.on("GET", "api.crossref.org/works/10.1234/alpha",
            make_response(200, crossref_payload(DOI, ABSTRACT_GAMMA, title=foreign)))
    http.on("GET", "api.datacite.org", make_response(200, datacite_payload(DOI, ABSTRACT_ALPHA, title=title)))
    route_model(http)
    out = tmp_path / "report.md"
    db = tmp_path / "abstracts.sqlite"
    result = run(project, "-o", out, *(["--abstract-db", db] if with_db else []), sources=["crossref", "datacite"])
    # The metadata check compares the same Crossref record and reports the other title as a finding.
    assert result.code == 1, result.err
    assert result.entries["alpha"]["status"] == "MISMATCH_MAJOR"
    assert result.claims[1]["evidence_source"] == "datacite"
    bodies = " ".join(json.dumps(c.body) for c in http.posts())
    assert ABSTRACT_ALPHA[:40] in bodies and "sprocket" not in bodies.lower()
    report = out.read_text(encoding="utf-8")
    assert "sprocket" not in report.replace(foreign, "").lower()  # only the title difference is shown
    assert "sprocket" not in report.split("## Claims", 1)[1].lower()
    if with_db:
        assert all(ABSTRACT_GAMMA != r[1] for r in rows(db)) and rows(db)
        assert foreign.lower() not in json.dumps(rows(db)).lower()  # nor its title as stored metadata


def test_title_search_hit_by_other_authors_is_not_used(tmp_path, http, run):
    # No DOI: the title search finds a record with the same title by another author.
    bib = "@article{alpha, author={Doe, Jane}, title={Widget Markets}, year={2020}}\n"
    hit = openalex_payload("10.5555/other", ABSTRACT_GAMMA, title=TITLE)
    hit["authorships"] = [{"author": {"display_name": "John Smith"}}]
    http.on("GET", "api.openalex.org/works", lambda call: make_response(200, {"results": [hit]})
            if "filter" in call.params else make_response(404))
    route_model(http)
    result = run(make_project(tmp_path / "doc", TEX, {"refs.bib": bib}), "--claim-check-only", sources=["openalex"])
    row = result.claims[1]
    assert (row["verdict"], row["lookup_status"], row["evidence_type"]) == ("UNVERIFIABLE", "absent", "none")
    assert "openalex: title search hit is another work" in row["lookup_reason"]
    assert http.posts() == []
