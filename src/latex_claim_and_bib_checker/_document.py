"""Reading the input once: the LaTeX document (or a ``.bib`` file) and its bibliography.

Both checks work on the :class:`Document` built here. The reference check
uses every bibliography entry, duplicates included. The claim check uses
the first entry of each key. Every note is recorded once.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from ._claims import Claim, extract_claims
from ._references import ConsistencyResult, check_consistency
from ._tex import cited_keys, embedded_bib_keys, find_bib_files, has_embedded_bibliography, parse_bib, read_tex


class InputError(Exception):
    """The input cannot be processed. The run stops with exit code 2."""


@dataclass
class Document:
    input_name: str
    is_bib: bool
    bib_names: list[str] = field(default_factory=list)
    bib_entries: list[tuple[str, dict[str, str]]] = field(default_factory=list)  # (file label, entry)
    entries: dict[str, dict[str, str]] = field(default_factory=dict)  # first entry per key
    claims: list[Claim] = field(default_factory=list)
    consistency: ConsistencyResult | None = None
    notes: list[str] = field(default_factory=list)


def display_name(path: Path, base: Path | None) -> str:
    """A short, path-free label for reports: relative to ``base`` if possible."""
    if base is not None:
        try:
            return path.resolve().relative_to(base.resolve()).as_posix()
        except ValueError:
            pass
    return path.name


def _read_bib(path: Path) -> list[dict[str, str]]:
    try:
        return parse_bib(path)
    except UnicodeDecodeError:
        raise InputError(f"{path.name} is not valid UTF-8") from None


def _add_note(document: Document, message: str) -> None:
    if message not in document.notes:
        document.notes.append(message)


def load_document(path: Path) -> Document:
    """Read ``path`` (``.tex`` or ``.bib``) and the bibliography files it declares."""
    if not path.is_file():
        raise InputError(f"input file not found: {path.name}")
    if path.suffix.lower() == ".bib":
        document = Document(input_name=path.name, is_bib=True, bib_names=[path.name])
        for entry in _read_bib(path):
            document.bib_entries.append((path.name, entry))
        _first_entries(document)
        return document

    try:
        tex = read_tex(path)
    except UnicodeDecodeError:
        raise InputError(f"{path.name} is not valid UTF-8") from None
    base = path.resolve().parent
    document = Document(input_name=path.name, is_bib=False)
    cited, nocite_all = cited_keys(tex)
    bib_format = "external"
    defined: set[str] = set()
    bib_paths: list[Path] = []

    declared = find_bib_files(tex, base)
    for bib in declared:
        if bib.is_file():
            bib_paths.append(bib)
        else:
            _add_note(document, f"declared bibliography file not found: {display_name(bib, base)}")
    if not declared:
        if has_embedded_bibliography(tex):
            bib_format = "embedded"
            defined = embedded_bib_keys(tex)
            _add_note(document, "embedded bibliography (thebibliography): "
                                "metadata verification needs a .bib file")
        else:
            bib_format = "none"
            _add_note(document, "no bibliography declared (\\bibliography or \\addbibresource)")

    for bib in bib_paths:
        label = display_name(bib, base)
        entries = _read_bib(bib)
        document.bib_names.append(label)
        document.bib_entries.extend((label, entry) for entry in entries)
        defined.update(entry.get("ID", "") for entry in entries)

    document.consistency = check_consistency(cited, defined, nocite_all)
    document.consistency.bib_format = bib_format
    document.claims = extract_claims(tex)
    _first_entries(document)
    return document


def _first_entries(document: Document) -> None:
    for _, entry in document.bib_entries:
        key = entry.get("ID", "")
        if key and key not in document.entries:
            document.entries[key] = entry
        elif key:
            _add_note(document, f"duplicate bibliography key '{key}', the claim check uses the first entry")
