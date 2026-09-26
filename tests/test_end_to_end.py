"""End to end on ``examples/``: both checks, the local model and the abstract database, all mocked.

The example has three deliberate cases: the year of ``breiman2001random`` is
off by one, ``unused2020example`` is never cited, and one sentence cites
``he2016resnet`` for a claim the paper does not make. Every source and model
answer here is synthetic.
"""

from __future__ import annotations

import shutil
import sqlite3

import pytest

from conftest import (
    EXAMPLES,
    chat_completion,
    crossref_work,
    datacite_payload,
    make_response,
    models_list,
    verdict,
)

# Synthetic stand-ins, one per cited work.
ABSTRACTS = {
    "lecun2015deep": "Synthetic stand-in abstract: models built from stacked layers learn layered "
                     "representations of their input data and are trained end to end.",
    "hochreiter1997lstm": "Synthetic stand-in abstract: a recurrent architecture with gated memory cells "
                          "learns to bridge long time lags in sequence data.",
    "he2016resnet": "Synthetic stand-in abstract: a residual learning framework eases the training of very "
                    "deep networks for image recognition.",
    "vaswani2017attention": "Synthetic stand-in abstract: a network architecture based solely on attention "
                            "mechanisms, dispensing with recurrence and convolutions entirely.",
    "kingma2015adam": "Synthetic stand-in abstract: an algorithm for first-order gradient-based optimization "
                      "of stochastic objective functions based on adaptive moment estimates.",
    "breiman2001random": "Synthetic stand-in abstract: random forests are combinations of tree predictors, "
                         "and their generalization error converges as the number of trees grows.",
    "gu2020empirical": "Synthetic stand-in abstract: machine learning methods are compared for measuring "
                       "asset risk premiums, with trees and neural networks performing best.",
    "harris2020numpy": "Synthetic stand-in abstract: array programming provides a powerful syntax for "
                       "accessing and manipulating data in scientific computing.",
}

# (key, doi, title, authors, year, container) as the metadata sources report them.
CROSSREF = {
    "10.1038/nature14539": ("lecun2015deep", "Deep learning", [("LeCun", "Yann"), ("Bengio", "Yoshua"),
                                                                ("Hinton", "Geoffrey")], 2015, "Nature"),
    "10.1162/neco.1997.9.8.1735": ("hochreiter1997lstm", "Long Short-Term Memory",
                                   [("Hochreiter", "Sepp"), ("Schmidhuber", "Jürgen")], 1997, "Neural Computation"),
    "10.1109/cvpr.2016.90": ("he2016resnet", "Deep Residual Learning for Image Recognition",
                             [("He", "Kaiming"), ("Zhang", "Xiangyu"), ("Ren", "Shaoqing"), ("Sun", "Jian")], 2016,
                             None),
    "10.1023/a:1010933404324": ("breiman2001random", "Random Forests", [("Breiman", "Leo")], 2001,
                                "Machine Learning"),
    "10.1093/rfs/hhaa009": ("gu2020empirical", "Empirical Asset Pricing via Machine Learning",
                            [("Gu", "Shihao"), ("Kelly", "Bryan"), ("Xiu", "Dacheng")], 2020,
                            "The Review of Financial Studies"),
    "10.1038/s41586-020-2649-2": ("harris2020numpy", "Array programming with NumPy",
                                  [("Harris", "Charles R."), ("Millman", "K. Jarrod")], 2020, "Nature"),
}


def crossref(call):
    doi = call.url.split("/works/", 1)[1].lower()
    if doi not in CROSSREF:
        return make_response(404)
    key, title, authors, year, container = CROSSREF[doi]
    return make_response(200, crossref_work(
        doi, title=[title], author=[{"family": f, "given": g} for f, g in authors],
        issued={"date-parts": [[year]]}, **({"container-title": [container]} if container else {"container-title": []}),
        volume=None, page=None, abstract=f"<jats:p>{ABSTRACTS[key]}</jats:p>"))


def datacite(call):
    doi = call.url.split("/dois/", 1)[1].lower()
    if doi == "10.48550/arxiv.1706.03762":
        return make_response(200, datacite_payload(doi, ABSTRACTS["vaswani2017attention"],
                                                   title="Attention Is All You Need"))
    if doi == "10.48550/arxiv.1412.6980":
        return make_response(200, datacite_payload(doi, ABSTRACTS["kingma2015adam"],
                                                   title="Adam: A Method for Stochastic Optimization"))
    return make_response(404)


def openalex(call):
    if "filter" in call.params and "Scikit-learn" in call.params["filter"]:
        return make_response(200, {"results": []})
    return make_response(404)


def model(call):
    user = next(m for m in call.body["messages"] if m["role"] == "user")["content"]
    key = user.split("Citation key: ", 1)[1].split("\n", 1)[0]
    sentence = user.split("CLAIM TO CHECK", 1)[1]
    if key == "he2016resnet" and "interest" in sentence:
        return chat_completion(verdict("SUSPICIOUS", "", "The abstract is about image recognition."))
    if key == "lecun2015deep":
        return chat_completion(verdict("SUPPORTED", "learn layered representations of their input data"))
    return chat_completion(verdict("PLAUSIBLE", "", "Consistent with the abstract."))


@pytest.fixture
def example(tmp_path, http):
    doc = tmp_path / "doc"
    shutil.copytree(EXAMPLES, doc)
    http.on("GET", "api.crossref.org/works/", crossref)
    http.on("GET", "api.datacite.org/dois/", datacite)
    http.on("GET", "api.openalex.org/works", openalex)
    http.on("GET", "127.0.0.1:1234/v1/models", models_list("example-local-model"))
    http.on("POST", "127.0.0.1:1234/v1/chat/completions", model)
    return doc


def test_default_run_on_the_example(example, http, run, tmp_path):
    out = tmp_path / "report.md"
    db = tmp_path / "store.sqlite"
    result = run(example / "paper.tex", "-o", out, "--abstract-db", db, sources=["crossref", "openalex", "datacite"])
    assert result.code == 1, result.err  # a SUSPICIOUS claim (the year difference alone is minor)
    data = result.data
    assert data["parts"] == ["references", "claims"] and data["mode"] == "local"
    entries = result.entries
    assert entries["breiman2001random"]["status"] == "MISMATCH_MINOR"
    assert entries["lecun2015deep"]["status"] == "MATCH" and entries["lecun2015deep"]["origin"] == "API"
    assert data["consistency"]["defined_but_uncited"] == ["unused2020example"]
    claims = {(c["cite_key"], c["index"]): c for c in data["claims"]}
    resnet = [c for (k, _), c in claims.items() if k == "he2016resnet"]
    assert [c["verdict"] for c in resnet] == ["PLAUSIBLE", "SUSPICIOUS"]  # one citation, one misattribution
    text = out.read_text(encoding="utf-8")
    assert "## References" in text and "## Claims" in text
    assert str(tmp_path) not in text and "store.sqlite" not in text
    assert ";" not in text  # fixed report texts use no semicolon

    stored = sqlite3.connect(db).execute("SELECT doi, provider FROM abstracts ORDER BY doi").fetchall()
    assert ("10.1038/nature14539", "crossref") in stored

    # Second run: no request for a stored work whose stored metadata match, in either check.
    # breiman2001random has a stored year difference, so its bib check goes live.
    http.calls.clear()
    second = run(example / "paper.tex", "--abstract-db", db, sources=["crossref", "openalex", "datacite"])
    stored_dois = {doi for doi, _ in stored}
    matching = {e["doi"].lower() for e in second.entries.values()
                if e["doi"] and e["doi"].lower() in stored_dois and e["origin"] == "DB"}
    assert matching and "10.1038/nature14539" in matching
    for call in http.calls:
        assert not any(doi in call.url.lower() for doi in matching), call.url
    assert second.entries["breiman2001random"]["origin"] == "API"
    assert second.entries["breiman2001random"]["status"] == "MISMATCH_MINOR"
    for entry in second.entries.values():
        if entry["origin"] == "DB":
            assert entry["status"] == "MATCH" and entry["metadata_date"]
    by_key = {c["cite_key"]: c for c in second.data["claims"]}
    assert by_key["lecun2015deep"]["evidence_origin"] == "DB"
    assert by_key["breiman2001random"]["evidence_origin"] == "DB"  # a year difference keeps the stored abstract
