"""Source plugins registered through the entry-point group."""

from __future__ import annotations

from importlib.metadata import EntryPoint

import pytest

from latex_claim_and_bib_checker import ENTRY_POINT_GROUP, _plugins
from conftest import ABSTRACT_ALPHA, claim_rows, crossref_payload, make_project, make_response

TEX_ONE = "\\begin{document}\nWidget indices predict returns \\citep{alpha}.\n\\bibliography{refs}\n\\end{document}\n"
BIB_DOI = "@article{alpha, title={Widget Markets}, year={2020}, doi={10.1234/alpha}}\n"


@pytest.fixture
def plugins(monkeypatch):
    """Install fake entry points: ``plugins(name="module:attr", ...)``."""

    def install(**targets: str) -> None:
        eps = [EntryPoint(name=name.replace("_", "-"), value=value, group=ENTRY_POINT_GROUP)
               for name, value in targets.items()]

        def fake_entry_points(*, group: str):
            assert group == ENTRY_POINT_GROUP
            return eps

        monkeypatch.setattr(_plugins, "entry_points", fake_entry_points)

    return install


def test_entry_point_group_name():
    assert ENTRY_POINT_GROUP == "latex_claim_and_bib_checker.sources"


def test_plugin_is_queried_in_its_order(tmp_path, http, plugins, run):
    plugins(example_source="plugin_fixtures:make_example")
    path = make_project(tmp_path, TEX_ONE, {"refs.bib": BIB_DOI})
    result = run(path, "--claim-check-only", judge=False)
    assert result.code == 0, result.err
    row = claim_rows(result.data)[1]
    assert row["evidence_source"] == "example-endpoint"  # Result.source, not Source.name
    assert http.calls == []  # order 0 comes before every built-in source


def test_failing_plugins_warn_and_the_run_continues(tmp_path, http, plugins, run):
    plugins(broken="plugin_fixtures:failing_factory", raising="plugin_fixtures:make_raising_fetch")
    http.on("GET", "api.crossref.org", make_response(200, crossref_payload("10.1234/alpha", ABSTRACT_ALPHA)))
    path = make_project(tmp_path, TEX_ONE, {"refs.bib": BIB_DOI})
    result = run(path, "--claim-check-only", sources=["raising-fetch", "crossref"], judge=False)
    assert result.code == 0
    assert "warning: could not load source plugin 'broken'" in result.err
    assert "raising-fetch: source raised RuntimeError: backend exploded" in result.err
    assert claim_rows(result.data)[1]["evidence_source"] == "crossref"


def test_name_collision_aborts(tmp_path, http, plugins, run):
    plugins(clash="plugin_fixtures:make_clash")
    path = make_project(tmp_path, TEX_ONE, {"refs.bib": BIB_DOI})
    result = run(path, "--claim-check-only", judge=False)
    assert result.code == 2 and "already used by a built-in source" in result.err
    assert http.calls == []
