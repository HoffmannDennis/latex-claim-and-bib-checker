"""TeX reading, bibliography discovery, citation scanning and claim sentences."""

from __future__ import annotations

from pathlib import Path

import pytest

from latex_claim_and_bib_checker._claims import extract_claims
from latex_claim_and_bib_checker._tex import cited_keys, find_bib_files, iter_citations, read_tex


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def test_read_tex_inlines_input_and_include_with_cycle_guard(tmp_path):
    _write(tmp_path / "sections" / "intro.tex", "Intro text \\cite{alpha}.\n\\input{sections/deep}\n")
    _write(tmp_path / "sections" / "deep.tex", "Deep text \\citep{beta}.\n\\input{main}\n")  # cycle back
    _write(tmp_path / "appendix.tex", "Appendix \\citet{gamma}.\n")
    main = _write(tmp_path / "main.tex", "\\input{sections/intro}\n\\include{appendix}\n% \\input{missing}\n")
    text = read_tex(main)
    assert "Intro text" in text and "Deep text" in text and "Appendix" in text
    assert "% \\input{missing}" in text  # a commented-out directive is left untouched


def test_find_bib_files_handles_multiple_files_and_biblatex(tmp_path):
    tex = (
        "\\bibliography{refs, extra/more.bib}\n"
        "\\addbibresource[datatype=bibtex]{biblatex.bib}\n"
        "% \\bibliography{commented}\n"
        "\\addbibresource{refs.bib}\n"
    )
    assert find_bib_files(tex, tmp_path) == [
        tmp_path / "refs.bib",
        tmp_path / "extra" / "more.bib",
        tmp_path / "biblatex.bib",
    ]


def test_iter_citations_covers_common_commands_and_skips_comments():
    text = (
        "See \\citep[p.~3]{a, b} and \\textcite{c}.\n"
        "Also \\Citet*{d}. % \\cite{hidden}\n"
        "Price is 5\\% \\autocite[see][12]{e}\n"
        "\\nocite{*}\n"
    )
    assert [(c.command, c.keys) for c in iter_citations(text)] == [
        ("citep", ("a", "b")),
        ("textcite", ("c",)),
        ("citet", ("d",)),
        ("autocite", ("e",)),
        ("nocite", ("*",)),
    ]


def test_sentences_and_keys_in_document_order():
    tex = r"""\begin{document}
\section{Intro}
Widgets matter, e.g. in markets \citep{alpha, beta}. Dr. Doe et al. disagree \citet{beta}.
% A commented citation \cite{hidden}.
\begin{table}\caption{Not a claim \cite{intable}}\end{table}
Gadgets matter.\cite{gamma} Another sentence without citation.
\nocite{*}
\end{document}"""
    claims = extract_claims(tex)
    assert [(c.cite_key, c.index) for c in claims] == [("alpha", 1), ("beta", 2), ("beta", 3), ("gamma", 4)]
    assert claims[0].sentence == r"Widgets matter, e.g. in markets \citep{alpha, beta}."
    assert claims[2].sentence == r"Dr. Doe et al. disagree \citet{beta}."
    assert claims[3].sentence == r"Gadgets matter.\cite{gamma}"


@pytest.mark.parametrize("command,keys", [
    ("\\parencites[see][12]{alpha}[34]{beta}", ["alpha", "beta"]),
    ("\\textcites{gamma}{delta}", ["gamma", "delta"]),
    ("\\footfullcite{alpha}", ["alpha"]),
    ("\\newcommand{\\mycite}[1]{\\citep{#1}}", []),  # a macro parameter is no key
])
def test_multi_cite_commands_give_every_key_to_both_checks(command, keys):
    tex = f"\\begin{{document}}\nWidgets matter {command}.\n\\end{{document}}"
    assert cited_keys(tex) == (set(keys), False)
    assert [c.cite_key for c in extract_claims(tex)] == keys
