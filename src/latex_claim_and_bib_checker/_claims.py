"""Extraction of (sentence, citation key) pairs from a LaTeX document.

Reading the files, resolving ``\\input``/``\\include``, locating the
bibliography files and parsing citations and BibTeX entries happens once
per run in :mod:`._document`. This module only splits the document body
into sentences.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from ._tex import iter_citations


@dataclass
class Claim:
    """One citing sentence paired with one of the keys it cites."""

    index: int  # 1-based position in document order
    sentence: str
    cite_key: str
    command: str
    context_before: str = ""


_COMMENT_RE = re.compile(r"(?<!\\)((?:\\\\)*)%.*$", re.MULTILINE)
_SKIP_ENVS = (
    "table", "figure", "equation", "align", "alignat", "gather", "multline", "eqnarray",
    "tabular", "tabularx", "longtable", "verbatim", "lstlisting", "displaymath", "math",
    "tikzpicture", "algorithm", "algorithmic",
)
_SKIP_ENV_RE = re.compile(
    r"\\begin\{(" + "|".join(_SKIP_ENVS) + r")(\*?)\}.*?\\end\{\1\2\}", re.DOTALL
)
_DISPLAY_MATH_RE = re.compile(r"\\\[.*?\\\]|\$\$.*?\$\$", re.DOTALL)
_HEADING_RE = re.compile(
    r"\\(?:part|chapter|section|subsection|subsubsection|paragraph|subparagraph)\*?"
    r"(?:\s*\[[^\]]*\])?\s*\{[^{}]*(?:\{[^{}]*\}[^{}]*)*\}"
)
_BREAK_RE = re.compile(r"\\(?:item|begin|end)\b(?:\s*\[[^\]]*\])?(?:\s*\{[^{}]*\})?|\\par\b|\\\\")
# A sentence ends at . ? or ! (plus closing brackets/quotes and any citation
# commands placed after the full stop), followed by whitespace and a capital
# letter, digit, opening bracket/quote or a LaTeX command.
_SPLIT_RE = re.compile(
    r"[.?!][)\]'\"\u2019\u201d]*"
    r"(?:\s*\\(?:cite|citep|parencite|autocite|footcite|supercite|smartcite|citenum)\*?"
    r"(?:\s*\[[^\]]*\])*\s*\{[^}]*\})*"
    r"(?=\s+[A-Z0-9(\\\"'`\u2018\u201c])"
)
_ABBREVIATION_END_RE = re.compile(
    r"(?:^|[^a-z])(?:e\.g|i\.e|et al|cf|figs?|eqs?|sec|vs|etc|no|vol|ch|approx|resp|incl|pp?|dr|prof|st)\.$",
    re.IGNORECASE,
)
_INITIAL_END_RE = re.compile(r"(?:^|[\s(~])[A-Z]\.$")
_SPACE_RE = re.compile(r"\s+")


def strip_comments(text: str) -> str:
    """Remove TeX comments (an unescaped ``%`` up to the end of the line)."""
    return _COMMENT_RE.sub(lambda m: m.group(1), text)


def _document_body(text: str) -> str:
    start = text.find("\\begin{document}")
    if start >= 0:
        text = text[start + len("\\begin{document}"):]
    end = text.find("\\end{document}")
    if end >= 0:
        text = text[:end]
    return text


def _paragraphs(body: str) -> list[str]:
    body = _SKIP_ENV_RE.sub("\n\n", body)
    body = _DISPLAY_MATH_RE.sub(" ", body)
    body = _HEADING_RE.sub("\n\n", body)
    body = _BREAK_RE.sub("\n\n", body)
    paragraphs = []
    for block in re.split(r"\n\s*\n", body):
        text = _SPACE_RE.sub(" ", block).strip()
        if text:
            paragraphs.append(text)
    return paragraphs


def split_sentences(paragraph: str) -> list[str]:
    """Split a paragraph into sentences, keeping common abbreviations intact.

    A citation command that directly follows a full stop stays with the
    sentence before it.
    """
    pieces: list[str] = []
    last = 0
    for match in _SPLIT_RE.finditer(paragraph):
        before = paragraph[last:match.start() + 1]  # up to and including the punctuation mark
        if _ABBREVIATION_END_RE.search(before) or _INITIAL_END_RE.search(before):
            continue
        pieces.append(paragraph[last:match.end()].strip())
        last = match.end()
    pieces.append(paragraph[last:].strip())
    return [p for p in pieces if p]


def extract_claims(tex_text: str) -> list[Claim]:
    """All (sentence, key) pairs of the document body, in document order."""
    claims: list[Claim] = []
    body = _document_body(strip_comments(tex_text))
    for paragraph in _paragraphs(body):
        previous = ""
        for sentence in split_sentences(paragraph):
            for citation in iter_citations(sentence):
                if citation.command == "nocite":
                    continue
                for key in citation.keys:
                    if key == "*":
                        continue
                    claims.append(Claim(len(claims) + 1, sentence, key, citation.command, previous))
            previous = sentence
    return claims
