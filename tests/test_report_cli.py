"""Reports, outputs, run modes and request economy of a combined run through the command line."""

from __future__ import annotations

import getpass
import socket as socket_module
from pathlib import Path

import pytest

from latex_claim_and_bib_checker._cli import exit_code
from conftest import (
    ABSTRACT_ALPHA,
    ABSTRACT_BETA,
    ABSTRACT_GAMMA,
    BIB_BASIC,
    TEX_BASIC,
    chat_completion,
    crossref_payload,
    make_project,
    make_response,
    models_list,
    verdict,
)


def route_sources(http) -> None:
    http.on("GET", "api.crossref.org/works/10.1234/alpha",
            make_response(200, crossref_payload("10.1234/alpha", ABSTRACT_ALPHA, title="Widget Markets")))
    http.on("GET", "api.crossref.org/works/10.1234/beta",
            make_response(200, crossref_payload("10.1234/beta", ABSTRACT_BETA, title="Gadget Standards")))


def route_model(http, label: str = "PLAUSIBLE") -> None:
    http.on("GET", "127.0.0.1:1234/v1/models", models_list("local-model"))
    http.on("POST", "127.0.0.1:1234/v1/chat/completions", chat_completion(verdict(label)))


@pytest.fixture
def project(tmp_path):
    return make_project(tmp_path / "doc")


# -- reports and outputs --------------------------------------------------------


def test_markdown_on_stdout_by_default(project, http, run):
    route_sources(http)
    result = run(project, "--claim-check-only", sources=["crossref"], judge=False)
    assert result.code == 0
    assert result.out.startswith("# Citation and bibliography report")


def test_reports_contain_no_absolute_paths_or_user(project, http, run, tmp_path):
    openpyxl = pytest.importorskip("openpyxl")
    route_sources(http)
    route_model(http)
    for suffix in (".md", ".xlsx"):
        out = tmp_path / f"report{suffix}"
        assert run(project, "-o", out, sources=["crossref"]).code == 0
        if suffix == ".md":
            text = out.read_text(encoding="utf-8")
        else:
            book = openpyxl.load_workbook(out)
            text = "\n".join(str(c.value) for sheet in book.worksheets for row in sheet.iter_rows() for c in row
                             if c.value is not None)
        assert str(tmp_path) not in text and str(Path.home()) not in text
        assert getpass.getuser() not in text
        assert socket_module.gethostname() not in text


def test_xlsx_opens_with_three_sheets_and_lists_the_undefined_key(tmp_path, http, run):
    openpyxl = pytest.importorskip("openpyxl")
    route_sources(http)
    tex = ("\\documentclass{article}\n\\begin{document}\nWidget indices predict returns \\citep{alpha}.\n"
           "Missing work \\citep{ghost}.\n\\bibliography{refs}\n\\end{document}\n")
    out = tmp_path / "report.xlsx"
    result = run(make_project(tmp_path / "doc", tex), "--bib-check-only", "-o", out)
    assert result.code == 1  # a cited key without an entry
    book = openpyxl.load_workbook(out)
    assert book.sheetnames == ["Summary", "References", "Claims"]
    summary = {row[0].value: row[1].value for row in book["Summary"].iter_rows() if row and row[0].value}
    assert summary["cited_but_undefined"] == "ghost"
    assert summary["defined_but_uncited"] == "beta"


def test_existing_output_is_replaced_and_other_suffixes_are_refused(project, http, run, tmp_path):
    route_sources(http)
    out = tmp_path / "report.md"
    out.write_text("old report", encoding="utf-8")
    assert run(project, "--claim-check-only", "-o", out, sources=["crossref"], judge=False).code == 0
    assert out.read_text(encoding="utf-8").startswith("# Citation and bibliography report")
    http.calls.clear()
    result = run(project, "-o", tmp_path / "report.json")
    assert result.code == 2 and "use .md or .xlsx" in result.err
    assert http.calls == []


def test_untrusted_text_stays_text_in_both_formats(tmp_path, http, run):
    openpyxl = pytest.importorskip("openpyxl")
    payload = "=1+1 ![x](https://example.invalid/p.png) <img src=x>"
    bib = BIB_BASIC.replace("Widget Markets", payload)
    http.on("GET", "api.crossref.org/works/10.1234/alpha",
            make_response(200, crossref_payload("10.1234/alpha", ABSTRACT_ALPHA, title=payload)))
    http.on("GET", "127.0.0.1:1234/v1/models", models_list("local-model"))
    http.on("POST", "127.0.0.1:1234/v1/chat/completions", chat_completion(verdict("PLAUSIBLE", "", payload)))
    doc = make_project(tmp_path / "doc", bibs={"refs.bib": bib})
    md, xlsx = tmp_path / "report.md", tmp_path / "report.xlsx"
    for out in (md, xlsx):
        run(doc, "--claim-check-only", "-o", out, sources=["crossref"])
    text = md.read_text(encoding="utf-8")
    assert "![x]" not in text and text.count("<img") == text.count("\\<img") == 4  # title and rationale, two claims
    assert "\\!\\[x\\](https://example.invalid/p.png) \\<img src=x\\>" in text
    claims = openpyxl.load_workbook(xlsx)["Claims"]
    cells = [c for row in claims.iter_rows(min_row=2) for c in row if isinstance(c.value, str)]
    assert [c.value for c in cells if c.value.startswith("=")] == [payload] * 4  # title and rationale, two claims
    assert all(c.data_type == "s" for c in cells)


def test_urls_with_parentheses_are_links_and_other_schemes_stay_text(tmp_path, http, run):
    doi = "10.1016/0304-405x(93)90023-5"
    bib = BIB_BASIC.replace("10.1234/alpha", doi.upper())
    http.on("GET", f"api.crossref.org/works/{doi}",
            make_response(200, crossref_payload(doi, ABSTRACT_ALPHA, title="Widget Markets")))
    http.on("GET", "api.crossref.org/works/10.1234/beta", make_response(200, crossref_payload(
        "10.1234/beta", ABSTRACT_BETA, title="Gadget Standards", DOI=None, URL="javascript:alert(1)")))
    result = run(make_project(tmp_path / "doc", bibs={"refs.bib": bib}), sources=["crossref"], judge=False)
    target = "(https://doi.org/10.1016/0304-405x%2893%2990023-5)"
    assert f"| [crossref (doi)]{target} |" in result.out
    assert f"- Link: [https://doi.org/10.1016/0304-405x(93)90023-5]{target}" in result.out
    assert "| crossref (doi) (javascript:alert(1)) |" in result.out
    assert "- Link: javascript:alert(1)" in result.out and "](javascript" not in result.out


def test_output_into_a_missing_directory_stops_before_any_request(project, http, run, tmp_path):
    result = run(project, "-o", tmp_path / "missing" / "report.md")
    assert result.code == 2 and "directory for report.md not found" in result.err
    assert str(tmp_path) not in result.err
    assert http.calls == []


def test_generated_reports_have_no_semicolons(project, http, run, tmp_path):
    # Fixed visible texts use no semicolon, several reasons are joined with " · ".
    http.on("GET", "api.crossref.org/works/10.1234/alpha", make_response(503))
    http.on("GET", "api.crossref.org/works/10.1234/beta", make_response(404))
    http.on("GET", "api.openalex.org", make_response(404))
    http.on("GET", "api.datacite.org", make_response(404))
    route_model(http)
    out = tmp_path / "report.md"
    run(project, "-o", out, sources=["crossref", "openalex", "datacite"])
    text = out.read_text(encoding="utf-8")
    assert ";" not in text
    assert " · " in text


# -- run modes --------------------------------------------------------------------


def test_bib_check_only_runs_no_model_and_no_abstract_source(project, http, run):
    route_sources(http)
    result = run(project, "--bib-check-only")
    assert result.code == 0
    assert result.data["parts"] == ["references"] and result.data["claims_run"] is False
    assert result.data["abstract_sources"] == []
    assert not any("127.0.0.1" in c.url for c in http.calls)
    assert http.posts() == []


def test_claim_check_only_runs_no_metadata_comparison(project, http, run):
    route_sources(http)
    route_model(http)
    result = run(project, "--claim-check-only", sources=["crossref"])
    assert result.code == 0
    assert result.data["parts"] == ["claims"] and result.data["references"] is None
    assert result.data["consistency"] is None
    assert len(http.posts()) == 3  # one model request per citing sentence with an abstract


def test_bib_input_runs_only_the_metadata_check(tmp_path, http, run):
    route_sources(http)
    bib = tmp_path / "refs.bib"
    bib.write_text(BIB_BASIC, encoding="utf-8")
    result = run(bib)
    assert result.code == 0
    assert result.data["parts"] == ["references"] and result.data["claims_run"] is False
    assert not any("127.0.0.1" in c.url for c in http.calls)
    assert run(bib, "--claim-check-only").code == 2


def test_thebibliography_in_a_default_run_skips_the_claim_check_with_a_note(tmp_path, http, run):
    # The bib check runs as before, the claim check is skipped, the exit code is unchanged.
    tex = ("\\documentclass{article}\n\\begin{document}\nA claim \\cite{a}.\n"
           "\\begin{thebibliography}{9}\n\\bibitem{a} A. Author. A work. 2020.\n\\end{thebibliography}\n"
           "\\end{document}\n")
    doc = make_project(tmp_path / "doc", tex, {})
    result = run(doc)
    assert result.code == 0, result.err
    assert http.calls == []  # no model server check either, there is nothing to judge
    assert result.data["parts"] == ["references"]
    assert "- Skipped: claims (no .bib entries found)" in result.out
    assert run(doc, "--claim-check-only").code == 2


@pytest.mark.parametrize("extra", [["--claim-check-only"], ["--cloud", "openai"], ["--model", "m"]])
def test_bib_check_only_conflicts_stop_before_any_request(project, http, run, extra):
    assert run(project, "--bib-check-only", *extra).code == 2
    assert http.calls == []


def test_exit_precedence():
    assert exit_code(provider_failed=True, no_source=True, findings=True) == 4
    assert exit_code(provider_failed=False, no_source=True, findings=True) == 3
    assert exit_code(provider_failed=False, no_source=False, findings=True) == 1
    assert exit_code(provider_failed=False, no_source=False, findings=False) == 0


# -- request economy and the report header ----------------------------------------


def test_one_request_per_doi_serves_both_parts_and_only_errors_are_asked_again(tmp_path, http, run):
    # alpha is found, beta is not found (remembered), gamma first fails (asked again).
    bib = BIB_BASIC + "@article{gamma, title={Sprockets}, year={2019}, doi={10.1234/gamma}}\n"
    tex = TEX_BASIC.replace("\\bibliography", "Sprockets have colours \\citep{gamma}.\n\\bibliography")
    doc = make_project(tmp_path / "doc", tex, {"refs.bib": bib})
    http.on("GET", "api.crossref.org/works/10.1234/alpha",
            make_response(200, crossref_payload("10.1234/alpha", ABSTRACT_ALPHA, title="Widget Markets")))
    http.on("GET", "api.crossref.org/works/10.1234/beta", make_response(404))
    answers = iter([make_response(503), make_response(200, crossref_payload("10.1234/gamma", ABSTRACT_GAMMA))])
    http.on("GET", "api.crossref.org/works/10.1234/gamma", lambda call: next(answers))
    route_model(http)
    result = run(doc, sources=["crossref"])
    assert [len(http.to(f"api.crossref.org/works/10.1234/{k}")) for k in ("alpha", "beta", "gamma")] == [1, 1, 2]
    assert result.entries["gamma"]["status"] == "SOURCE_ERROR"
    assert result.claims[4]["lookup_status"] == "found"


def test_header_lists_used_sources_and_sources_key_missing(project, http, run):
    # Always every available source, the report names the ones that lack a key.
    route_model(http)
    result = run(project)
    data = result.data
    assert data["metadata_sources"] == ["crossref", "openalex", "datacite"]
    assert data["abstract_sources"] == ["arxiv", "crossref", "openalex", "datacite", "s2", "core", "europepmc"]
    assert "- Abstract sources: arxiv, crossref, openalex, datacite, s2, core, europepmc" in result.out
    assert "- Sources not used, key missing: springer (`SPRINGER_API_KEY`)" in result.out
