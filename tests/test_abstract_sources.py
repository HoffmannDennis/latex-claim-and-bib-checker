"""The sources: each response shape, fallthrough, locks, the DOI rule, title search and secrets."""

from __future__ import annotations

import pytest
import requests

from latex_claim_and_bib_checker import Query, Status
from latex_claim_and_bib_checker._abstract_sources import CoreSource, SemanticScholarSource
from latex_claim_and_bib_checker._metadata_sources import CrossrefSource
from conftest import (
    ABSTRACT_ALPHA,
    FakeHttp,
    arxiv_feed,
    claim_rows,
    core_payload,
    crossref_payload,
    crossref_work,
    datacite_payload,
    europepmc_payload,
    make_project,
    make_response,
    openalex_payload,
    s2_payload,
    springer_payload,
)

DOI = "10.1234/alpha"

TEX_ONE = "\\begin{document}\nWidget indices predict returns \\citep{alpha}.\n\\bibliography{refs}\n\\end{document}\n"
BIB_DOI = "@article{alpha, title={Widget Markets}, year={2020}, doi={10.1234/Alpha}}\n"
BIB_ARXIV = ("@misc{alpha, title={Widget Markets}, year={2017}, eprint={1706.03762v5}, "
             "archivePrefix={arXiv}}\n")

# (source, bib, url fragment, response for a record title, expected resolved_doi, extra env)
SOURCE_CASES = [
    ("arxiv", BIB_ARXIV, "export.arxiv.org/api/query", lambda t: make_response(
        200, text=arxiv_feed("1706.03762", ABSTRACT_ALPHA, 5, title=t), content_type="application/atom+xml"),
     "10.48550/arxiv.1706.03762", {}),
    ("datacite", BIB_DOI, f"api.datacite.org/dois/{DOI}",
     lambda t: make_response(200, datacite_payload(DOI, ABSTRACT_ALPHA, title=t)), DOI, {}),
    ("crossref", BIB_DOI, f"api.crossref.org/works/{DOI}",
     lambda t: make_response(200, crossref_payload(DOI, ABSTRACT_ALPHA, title=t)), DOI, {}),
    ("openalex", BIB_DOI, f"api.openalex.org/works/doi:{DOI}",
     lambda t: make_response(200, openalex_payload(DOI, ABSTRACT_ALPHA, title=t)), DOI, {}),
    ("springer", BIB_DOI, "api.springernature.com/meta/v2/json",
     lambda t: make_response(200, springer_payload(DOI, ABSTRACT_ALPHA, title=t)), DOI,
     {"SPRINGER_API_KEY": "springer-secret-123456"}),
    ("s2", BIB_DOI, f"api.semanticscholar.org/graph/v1/paper/DOI:{DOI}",
     lambda t: make_response(200, s2_payload(DOI, ABSTRACT_ALPHA, title=t)), DOI, {}),
    ("core", BIB_DOI, "api.core.ac.uk/v3/search/works",
     lambda t: make_response(200, core_payload(DOI, ABSTRACT_ALPHA, title=t)), DOI, {}),
    ("europepmc", BIB_DOI, "europepmc/webservices/rest/search",
     lambda t: make_response(200, europepmc_payload(DOI, ABSTRACT_ALPHA, title=t)), DOI, {}),
]


@pytest.mark.parametrize("name,bib,fragment,respond,resolved,env", SOURCE_CASES, ids=[c[0] for c in SOURCE_CASES])
def test_each_source_shape_gives_evidence_unless_its_record_names_another_work(
        tmp_path, http, run, monkeypatch, name, bib, fragment, respond, resolved, env):
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    path = make_project(tmp_path, TEX_ONE, {"refs.bib": bib})
    out = tmp_path / "report.md"
    http.on("GET", fragment, respond("WIDGET markets."))
    result = run(path, "--claim-check-only", "-o", out, sources=[name], judge=False)
    assert result.code == 0, result.err
    row = claim_rows(result.data)[1]
    assert (row["lookup_status"], row["evidence_source"], row["evidence_origin"], row["resolved_doi"]) == (
        "found", name, "API", resolved)
    assert all(fragment in c.url for c in http.calls)
    for value in env.values():
        assert value not in result.err and value not in out.read_text()

    http.on("GET", fragment, respond("Gadget standards in a simulated survey"))
    other = claim_rows(run(path, "--claim-check-only", sources=[name], judge=False).data)[1]
    assert other["lookup_status"] == "absent"
    assert f"{name}: record title does not match the entry" in other["lookup_reason"]


def test_error_and_429_move_on_to_next_source(tmp_path, http, run):
    http.on("GET", "api.datacite.org", requests.ConnectionError("connection refused"))
    http.on("GET", "api.crossref.org", make_response(429))
    http.on("GET", "api.openalex.org", make_response(503))
    http.on("GET", "api.semanticscholar.org", make_response(200, s2_payload(DOI, ABSTRACT_ALPHA)))
    result = run(make_project(tmp_path, TEX_ONE, {"refs.bib": BIB_DOI}), "--claim-check-only", judge=False)
    assert result.code == 0, result.err
    row = claim_rows(result.data)[1]
    assert row["evidence_source"] == "s2" and row["lookup_status"] == "found"
    assert http.to("api.core.ac.uk") == []  # stopped at the first usable abstract


def test_all_sources_failing_gives_source_error_exit_3_and_no_title_search(tmp_path, http, run):
    for fragment in ("datacite", "crossref", "openalex", "semanticscholar", "core.ac.uk", "europepmc"):
        http.on("GET", fragment, make_response(429))
    result = run(make_project(tmp_path, TEX_ONE, {"refs.bib": BIB_DOI}), "--claim-check-only", judge=False)
    assert result.code == 3
    row = claim_rows(result.data)[1]
    assert row["lookup_status"] == "source_error" and "429" in row["lookup_reason"]
    assert not [c for c in http.calls if "filter" in c.params]  # no title search after DOI errors


def test_access_denied_and_used_up_allowance_lock_the_endpoint_for_the_run(tmp_path, http, run):
    bib = "".join(f"@article{{k{i}, title={{Work {i}}}, year={{2020}}, doi={{10.1234/k{i}}}}}\n" for i in range(3))
    tex = "\\begin{document}\n" + " ".join(f"Claim {i} \\citep{{k{i}}}." for i in range(3)) + \
          "\n\\bibliography{refs}\n\\end{document}\n"
    http.on("GET", "api.crossref.org", make_response(403))
    http.on("GET", "api.core.ac.uk", make_response(429, headers={"X-RateLimit-Remaining": "0"}))
    http.on("GET", "europepmc", lambda call: make_response(
        200, europepmc_payload(call.params["query"].split('"')[1], ABSTRACT_ALPHA)))
    result = run(make_project(tmp_path, tex, {"refs.bib": bib}), "--claim-check-only",
                 sources=["crossref", "core", "europepmc"], judge=False)
    assert result.code == 0, result.err
    assert len(http.to("api.crossref.org")) == 1 and len(http.to("api.core.ac.uk")) == 1
    assert all(r["evidence_source"] == "europepmc" for r in claim_rows(result.data).values())


def test_record_with_another_doi_is_absent():
    session = FakeHttp().on("GET", "api.crossref.org", make_response(200, crossref_work("10.9999/other")))
    result = CrossrefSource(session=session).fetch(Query(doi=DOI))
    assert (result.status, result.reason, result.record) == (Status.ABSENT, "doi mismatch", None)
    session = FakeHttp().on("GET", "api.core.ac.uk", make_response(200, core_payload("10.9999/other", ABSTRACT_ALPHA)))
    assert CoreSource(session=session).fetch(Query(doi=DOI)).reason == "doi mismatch"


def test_title_search_only_without_doi_or_after_absent(tmp_path, http, run):
    bib = ("@article{alpha, title={Widget Markets}, year={2020}}\n"
           "@article{beta, title={Gadget Standards}, year={2021}, doi={10.1234/beta}}\n"
           "@article{gamma, title={Sprocket Colour}, year={2022}, doi={10.1234/gamma}}\n")
    tex = ("\\begin{document}\nA \\citep{alpha}. B \\citep{beta}. C \\citep{gamma}.\n"
           "\\bibliography{refs}\n\\end{document}\n")
    # gamma: the DOI record exists but carries no abstract, which rules out a title search
    http.on("GET", "api.crossref.org/works/10.1234/gamma",
            make_response(200, crossref_payload("10.1234/gamma", None, title="Sprocket Colour")))
    http.on("GET", "api.openalex.org/works", lambda call: make_response(
        200, {"results": [openalex_payload("10.1234/found-by-title", ABSTRACT_ALPHA, title="Widget Markets")]})
        if "filter" in call.params else make_response(404))
    result = run(make_project(tmp_path, tex, {"refs.bib": bib}), "--claim-check-only",
                 sources=["crossref", "openalex"], judge=False)
    rows = claim_rows(result.data)
    assert rows[1]["evidence_match"] == "title" and rows[1]["resolved_doi"] == "10.1234/found-by-title"
    # every DOI lookup for beta was absent, so a title search ran (without a match)
    assert rows[2]["lookup_status"] == "absent" and "no title match" in rows[2]["lookup_reason"]
    assert rows[3]["lookup_status"] == "absent" and "crossref: no usable abstract" in rows[3]["lookup_reason"]
    searches = [c for c in http.calls if "filter" in c.params]
    assert [("Widget" in c.params["filter"]) for c in searches] == [True, False]
    assert not any("Sprocket" in c.params["filter"] for c in searches)


def test_generic_user_agent_and_contact_email_only_from_env(tmp_path, http, run, monkeypatch):
    http.on("GET", "api.crossref.org", make_response(200, crossref_payload(DOI, ABSTRACT_ALPHA)))
    path = make_project(tmp_path, TEX_ONE, {"refs.bib": BIB_DOI})
    run(path, sources=["crossref"], judge=False)
    assert http.calls and all(c.headers["User-Agent"].startswith("latex-claim-and-bib-checker/") for c in http.calls)
    assert not any("mailto" in c.headers["User-Agent"] or "mailto" in c.params for c in http.calls)
    http.calls.clear()
    monkeypatch.setenv("CONTACT_EMAIL", "someone@example.org")
    run(path, sources=["crossref"], judge=False)
    assert http.calls and all("someone@example.org" in c.headers["User-Agent"] for c in http.calls)


def test_optional_keys_travel_in_headers():
    session = FakeHttp()
    session.on("GET", "semanticscholar", make_response(200, s2_payload(DOI, ABSTRACT_ALPHA)))
    session.on("GET", "core.ac.uk", make_response(200, core_payload(DOI, ABSTRACT_ALPHA)))
    SemanticScholarSource(session=session, api_key="s2-key-value").fetch(Query(doi=DOI))
    CoreSource(session=session, api_key="core-key-value").fetch(Query(doi=DOI))
    s2_call, core_call = session.calls
    assert s2_call.headers["x-api-key"] == "s2-key-value" and "s2-key-value" not in str(s2_call.params)
    assert core_call.headers["Authorization"] == "Bearer core-key-value" and "core-key-value" not in core_call.url


def test_api_key_never_reaches_report_or_stderr(tmp_path, http, run, monkeypatch):
    secret = "oa-test-value-7c1e9a4b2f"
    monkeypatch.setenv("OPENALEX_API_KEY", secret)

    def openalex(call):
        assert call.params.get("api_key") == secret
        raise requests.ConnectionError(
            f"HTTPSConnectionPool: Max retries exceeded with url: {call.url}?api_key={secret}&per_page=5")

    http.on("GET", "api.crossref.org", make_response(503))
    http.on("GET", "api.openalex.org", openalex)
    path = make_project(tmp_path / "doc")
    for suffix in (".md", ".xlsx"):
        out = tmp_path / f"report{suffix}"
        result = run(path, "--bib-check-only", "-o", out)
        assert secret.encode() not in out.read_bytes()
        assert secret not in result.out + result.err
    assert "openalex" in (tmp_path / "report.md").read_text(encoding="utf-8")
    assert http.to("api.openalex.org")
