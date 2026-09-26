"""Reading LaTeX sources and BibTeX files.

Everything here works on local files, reads them as UTF-8 and never writes
anything. Comment handling follows TeX: an unescaped ``%`` starts a comment
that runs to the end of the line.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Iterator, NamedTuple

from bibtexparser import loads as _bib_loads
from bibtexparser.bparser import BibTexParser

# Citation commands recognised by :func:`iter_citations` (standard LaTeX,
# natbib and biblatex). Capitalised and starred variants are accepted too.
_CITE_COMMANDS = (
    "cite",
    "citep",
    "citet",
    "citealt",
    "citealp",
    "citeauthor",
    "citeyear",
    "citeyearpar",
    "citenum",
    "citetitle",
    "parencite",
    "textcite",
    "autocite",
    "smartcite",
    "supercite",
    "fullcite",
    "footcite",
    "footfullcite",
    "nocite",
)
# biblatex multi-cite commands: one or more groups of up to two optional
# arguments and a key list, as in \parencites[see][12]{a}[34]{b}.
_MULTI_CITE_COMMANDS = (
    "cites", "parencites", "textcites", "autocites", "footcites", "smartcites", "supercites",
)


def _alternatives(names: tuple[str, ...]) -> str:
    return "|".join(sorted((re.escape(c) for c in names), key=len, reverse=True))


_CITE_RE = re.compile(
    r"\\(?:"
    r"(" + _alternatives(_MULTI_CITE_COMMANDS) + r")\*?"
    r"((?:(?:\s*\[[^\]]*\]){0,2}\s*\{[^}]*\})+)"
    r"|(" + _alternatives(_CITE_COMMANDS) + r")\*?"
    r"(?:\s*\[[^\]]*\])*"
    r"\s*\{([^}]*)\}"
    r")",
    re.IGNORECASE,
)
_OPTIONAL_ARG_RE = re.compile(r"\[[^\]]*\]")
_KEY_GROUP_RE = re.compile(r"\{([^}]*)\}")
_INPUT_RE = re.compile(r"\\(?:input|include)\s*\{([^}]+)\}")
_BIBLIOGRAPHY_RE = re.compile(r"\\(?:no)?bibliography\s*\{([^}]+)\}")
_ADDBIBRESOURCE_RE = re.compile(r"\\addbibresource\s*(?:\[[^\]]*\])?\s*\{([^}]+)\}")
_BIBITEM_RE = re.compile(r"\\bibitem\s*(?:\[[^\]]*\])?\s*\{([^}]+)\}")
_COMMENT_START_RE = re.compile(r"(?<!\\)(?:\\\\)*%")

_MAX_INPUT_DEPTH = 10
_ENCODING = "utf-8-sig"  # UTF-8; a leading byte-order mark is tolerated


class Citation(NamedTuple):
    """One citation command found by :func:`iter_citations`.

    ``command`` is the lower-cased command name without backslash or star,
    ``keys`` the comma-separated keys in order (``("*",)`` for
    ``\\nocite{*}``, all key lists of a multi-cite command in turn), and
    ``start``/``end`` the span of the whole command in the scanned text.
    Keys containing ``#`` (macro parameters) are left out.
    """

    command: str
    keys: tuple[str, ...]
    start: int
    end: int


def _mask_comments(text: str) -> str:
    """Blank out TeX comments while keeping every offset and newline intact."""
    out: list[str] = []
    for line in text.splitlines(keepends=True):
        match = _COMMENT_START_RE.search(line)
        if match is None:
            out.append(line)
            continue
        cut = match.end() - 1
        body = line[:cut]
        rest = line[cut:]
        newline = rest[len(rest.rstrip("\r\n")):]
        out.append(body + " " * (len(rest) - len(newline)) + newline)
    return "".join(out)


def iter_citations(text: str) -> Iterator[Citation]:
    """Yield every citation command in ``text``, skipping TeX comments."""
    masked = _mask_comments(text)
    for match in _CITE_RE.finditer(masked):
        if match.group(1):
            command = match.group(1)
            lists = _KEY_GROUP_RE.findall(_OPTIONAL_ARG_RE.sub("", match.group(2)))
        else:
            command, lists = match.group(3), [match.group(4)]
        keys = tuple(k.strip() for keys in lists for k in keys.split(",") if k.strip() and "#" not in k)
        yield Citation(command.lower(), keys, match.start(), match.end())


def _resolve_child(name: str, root: Path, current_dir: Path) -> Path | None:
    names = [name] if name.endswith(".tex") else [name + ".tex", name]
    for base in (root, current_dir):
        for candidate_name in names:
            candidate = (base / candidate_name).resolve()
            if candidate.is_file():
                return candidate
    return None


def _inline_inputs(file: Path, root: Path, stack: tuple[Path, ...]) -> str:
    text = file.read_text(encoding=_ENCODING)
    if len(stack) > _MAX_INPUT_DEPTH:
        return text
    masked = _mask_comments(text)
    pieces: list[str] = []
    last = 0
    for match in _INPUT_RE.finditer(masked):
        child = _resolve_child(match.group(1).strip(), root, file.parent)
        if child is None or child in stack:
            continue
        pieces.append(text[last:match.start()])
        pieces.append(_inline_inputs(child, root, stack + (child,)))
        last = match.end()
    pieces.append(text[last:])
    return "".join(pieces)


def read_tex(path: str | Path) -> str:
    """Read a LaTeX file as UTF-8 and inline its ``\\input``/``\\include`` files.

    Included files are looked up relative to the directory of the main file
    first (as LaTeX does) and then relative to the including file; a missing
    ``.tex`` suffix is added. Files that cannot be found, commented-out
    directives and include cycles are left as they are. Nesting is limited
    to ten levels.
    """
    main = Path(path).resolve()
    return _inline_inputs(main, main.parent, (main,))


def find_bib_files(tex_text: str, base_dir: str | Path) -> list[Path]:
    """List the bibliography files declared in ``tex_text``.

    Supports ``\\bibliography{a,b}`` (``.bib`` is appended where missing) and
    biblatex ``\\addbibresource[...]{file.bib}``. Paths are relative to
    ``base_dir``, returned in order of appearance without duplicates and
    not checked for existence.
    """
    base = Path(base_dir)
    masked = _mask_comments(tex_text)
    found: list[tuple[int, str]] = []
    for match in _BIBLIOGRAPHY_RE.finditer(masked):
        for name in match.group(1).split(","):
            name = name.strip()
            if name:
                found.append((match.start(), name if name.endswith(".bib") else name + ".bib"))
    for match in _ADDBIBRESOURCE_RE.finditer(masked):
        name = match.group(1).strip()
        if name:
            found.append((match.start(), name))
    found.sort(key=lambda item: item[0])
    paths: list[Path] = []
    for _, name in found:
        candidate = base / name
        if candidate not in paths:
            paths.append(candidate)
    return paths


def _parse_bib_text(text: str) -> list[dict[str, str]]:
    parser = BibTexParser(common_strings=True, ignore_nonstandard_types=False)
    database = _bib_loads(text, parser=parser)
    return [dict(entry) for entry in database.entries]


def parse_bib(path: str | Path) -> list[dict[str, str]]:
    """Parse a BibTeX/biblatex file (read as UTF-8) into a list of entries.

    Each entry is a plain dict with lower-case field names plus ``ID``
    (the citation key) and ``ENTRYTYPE``. Non-standard entry types such as
    ``@online`` are kept.
    """
    return _parse_bib_text(Path(path).read_text(encoding=_ENCODING))


def cited_keys(text: str) -> tuple[set[str], bool]:
    """Return the set of cited keys and whether ``\\nocite{*}`` is present."""
    keys: set[str] = set()
    nocite_all = False
    for citation in iter_citations(text):
        for key in citation.keys:
            if key == "*":
                if citation.command == "nocite":
                    nocite_all = True
                continue
            keys.add(key)
    return keys, nocite_all


def has_embedded_bibliography(text: str) -> bool:
    return "\\begin{thebibliography}" in _mask_comments(text)


def embedded_bib_keys(text: str) -> set[str]:
    """Keys defined with ``\\bibitem`` inside an embedded bibliography."""
    return {m.group(1).strip() for m in _BIBITEM_RE.finditer(_mask_comments(text))}
