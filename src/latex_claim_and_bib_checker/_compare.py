"""Field-by-field comparison of a BibTeX entry with a source record.

Matching is deliberately tolerant of formatting noise: LaTeX markup, HTML
tags, diacritics, dash variants, surname particles, subtitles that one side
omits, and a publication year that differs by one (online-first versus
print). Remaining differences are graded as minor or major.
"""

from __future__ import annotations

import difflib
import re
import unicodedata
from dataclasses import dataclass
from typing import Any

# Surname particles that sources often drop or move ("van Eck" -> "Eck").
_NAME_PARTICLES = {"van", "vom", "von", "de", "del", "di", "den", "der", "la", "le"}


@dataclass
class FieldCheck:
    """Comparison of one field between the BibTeX entry and one source."""

    field_name: str
    bib_value: str
    source_value: str
    match: bool
    source: str


def _strip_diacritics(s: str) -> str:
    decomposed = unicodedata.normalize("NFD", s)
    return "".join(c for c in decomposed if unicodedata.category(c) != "Mn")


def _strip_html(s: str) -> str:
    return re.sub(r"<[^>]+>", "", s)


def normalize_text(s: str) -> str:
    """Lower-case comparison form without markup, accents, quotes or dashes."""
    s = _strip_html(s)
    s = _strip_diacritics(s)
    s = re.sub(r"[{}\\\'\"\-\u2013\u2014]", "", s)
    return re.sub(r"\s+", " ", s).strip().lower()


def clean_latex(s: str) -> str:
    """Drop simple LaTeX accent commands and grouping braces."""
    s = re.sub(r"\\['\"`^~uvHtcdb]\{([a-zA-Z])\}", r"\1", s)
    s = re.sub(r"\\['\"`^~uvHtcdb]([a-zA-Z])", r"\1", s)
    s = re.sub(r"\\v\{([a-zA-Z])\}", r"\1", s)
    s = re.sub(r"\\o\b", "o", s)
    s = re.sub(r"\{([^}]*)\}", r"\1", s)
    return s.strip()


def title_similarity(a: str, b: str) -> float:
    """Similarity ratio of two titles after normalisation (0.0 to 1.0)."""
    return difflib.SequenceMatcher(None, normalize_text(a), normalize_text(b)).ratio()


def _surnames_match(a: str, b: str) -> bool:
    """Compare two normalised surnames, allowing typos and compound names."""
    if a == b:
        return True
    if difflib.SequenceMatcher(None, a, b).ratio() >= 0.8:
        return True
    a_parts = {p for p in a.split() if len(p) >= 3}
    b_parts = {p for p in b.split() if len(p) >= 3}
    return bool(a_parts and b_parts and (a_parts & b_parts))


def _drop_particles(name: str) -> str:
    tokens = name.split()
    while len(tokens) > 1 and tokens[0].lower() in _NAME_PARTICLES:
        tokens.pop(0)
    return " ".join(tokens)


_WORD_RE = re.compile(r"\w+")
# Text between two title words that starts a subtitle. A plain space or a
# word-internal hyphen ("learning-based") does not.
_SUBTITLE_SEPARATOR_RE = re.compile(r"[:?!.(\[\u2013\u2014]|--|\s-|-\s")


def _title_words(title: str) -> tuple[str, list[re.Match[str]]]:
    """Comparison text of a title (LaTeX, HTML and accents removed, case-folded) and its words."""
    text = _strip_diacritics(_strip_html(clean_latex(title))).casefold()
    return text, list(_WORD_RE.finditer(text))


def _titles_equivalent(a: str, b: str) -> bool:
    """Same words, or one side adds a subtitle after an explicit separator.

    Two titles that both have a ':' or '?' and the same main part of at
    least 30 characters before it are equivalent as well.
    """
    shorter, longer = sorted((_title_words(a), _title_words(b)), key=lambda t: len(t[1]))
    words = [m[0] for m in shorter[1]]
    text, runs = longer
    n = len(words)
    if not n:
        return False
    if [m[0] for m in runs[:n]] == words:
        if len(runs) == n or _SUBTITLE_SEPARATOR_RE.search(text[runs[n - 1].end():runs[n].start()]):
            return True
    for sep in (":", "?"):
        if sep in shorter[0] and sep in text:
            mains = [_WORD_RE.findall(t.split(sep)[0]) for t in (shorter[0], text)]
            if mains[0] == mains[1] and len(" ".join(mains[0])) >= 30:
                return True
    return False


def _title_match(bib_title: str, source_title: str) -> bool:
    if _titles_equivalent(bib_title, source_title):
        return True
    a = normalize_text(clean_latex(bib_title))
    b = normalize_text(source_title)
    return difflib.SequenceMatcher(None, a, b).ratio() >= 0.85


def _source_surname(name: str) -> str:
    """Surname from a source name: 'Family, Given' or 'Given Family'."""
    return name.split(",")[0].strip() if "," in name else name.split()[-1]


def _source_surname_candidates(name: str) -> list[str]:
    """Surname candidates for a source name.

    Some OpenAlex display names glue family and given name into one token
    ('PedregosaFabian'). For such a token (no comma, no whitespace) the part
    before the first lower-to-upper case transition ('Pedregosa') is added as
    a second candidate; the whole token stays first so that names such as
    'LeCun' or 'McDonald' still match as written.
    """
    surname = _source_surname(name)
    candidates = [surname]
    token = name.strip()
    if "," not in token and len(token.split()) == 1:
        for i in range(1, len(token)):
            if token[i - 1].islower() and token[i].isupper():
                candidates.append(token[:i])
                break
    return candidates


def bib_surnames(authors: str) -> list[str]:
    """Surnames from a BibTeX author list; corporate names are skipped."""
    authors = clean_latex(authors)
    names: list[str] = []
    for part in re.split(r"\s+and\s+", authors):
        part = part.strip()
        if not part or part.startswith("{") or part.startswith("The "):
            continue
        if "," in part:
            names.append(part.split(",")[0].strip())
        else:
            tokens = part.split()
            if tokens:
                names.append(tokens[-1].strip())
    return names


def _authors_match(bib_authors: str, source_authors: list[str]) -> bool:
    """Compare the surnames of the first three authors on both sides."""
    bib_names = bib_surnames(bib_authors)[:3]
    source_names = [
        [normalize_text(_drop_particles(c)) for c in _source_surname_candidates(n)]
        for n in source_authors[:3]
        if n.strip()
    ]
    if not bib_names or not source_names:
        return True
    bib_normed = [normalize_text(_drop_particles(n)) for n in bib_names]
    hits = sum(
        1
        for b, candidates in zip(bib_normed, source_names, strict=False)
        if any(_surnames_match(b, a) for a in candidates)
    )
    return hits >= min(len(bib_names), len(source_names))


def _journal_match(bib_journal: str, source_journal: str) -> bool:
    a = normalize_text(clean_latex(bib_journal))
    b = normalize_text(source_journal)
    if a == b:
        return True
    return difflib.SequenceMatcher(None, a, b).ratio() >= 0.70


_JOURNAL_FILLERS = {"the", "of", "and", "&", "in", "for", "on"}


def _abbreviates(short: str, word: str) -> bool:
    """``short`` is ``word`` or an abbreviation of it (same first letter, letters in order)."""
    if not short or short[0] != word[:1]:
        return False
    rest = iter(word)
    return all(c in rest for c in short)


def _journal_abbreviation(bib_journal: str, source_journal: str) -> bool:
    """True if one journal name abbreviates the other word by word.

    Dots, commas and the words 'the', 'of', 'and', '&', 'in', 'for' and 'on'
    are ignored ('J. Financ. Econ.' and 'Journal of Financial Economics').
    """
    def words(name: str) -> list[str]:
        return [w for w in re.sub(r"[.,]", "", name).split() if w not in _JOURNAL_FILLERS]

    a = words(normalize_text(clean_latex(bib_journal)))
    b = words(normalize_text(source_journal))
    if not a or len(a) != len(b):
        return False
    return all(_abbreviates(x, y) for x, y in zip(a, b)) or all(_abbreviates(y, x) for x, y in zip(a, b))


def _unify_dashes(s: str) -> str:
    s = re.sub(r"[\u2013\u2014]", "-", s)
    s = re.sub(r"-{2,}", "-", s)
    return s.strip()


def _pages_match(bib_pages: str, source_pages: str) -> bool:
    """Equal ranges, or equal first pages when one side has no last page."""
    a = _unify_dashes(bib_pages)
    b = _unify_dashes(source_pages)
    if a == b:
        return True
    return a.split("-")[0].strip() == b.split("-")[0].strip()


def _issue_match(bib_issue: str, source_issue: str) -> bool:
    return _unify_dashes(bib_issue) == _unify_dashes(source_issue)


def is_major(check: FieldCheck) -> bool:
    """Grade a failed check: ``True`` for a substantive difference.

    Formatting-only differences (diacritics, particles, subtitles), a year
    that is off by one, an abbreviated journal name and issue numbers count
    as minor.
    """
    name = check.field_name
    if name == "title":
        return not _titles_equivalent(check.bib_value, check.source_value)

    if name == "author":
        bib_parts = [normalize_text(_drop_particles(n)) for n in check.bib_value.split(", ")]
        src_parts = [normalize_text(_drop_particles(n)) for n in check.source_value.split(", ")]
        if bib_parts == src_parts:
            return False
        hits = sum(
            1
            for b, a in zip(bib_parts, src_parts, strict=False)
            if b == a or difflib.SequenceMatcher(None, b, a).ratio() >= 0.8
        )
        return hits < min(len(bib_parts), len(src_parts))

    if name == "year":
        try:
            return abs(int(check.bib_value.strip()) - int(check.source_value.strip())) > 1
        except ValueError:
            return True

    if name == "journal":
        if _journal_abbreviation(check.bib_value, check.source_value):
            return False
        return normalize_text(clean_latex(check.bib_value)) != normalize_text(check.source_value)

    if name in ("volume", "pages", "doi"):
        return normalize_text(check.bib_value) != normalize_text(check.source_value)

    return False


def compare_entry(entry: dict[str, str], fields: dict[str, Any], source: str) -> list[FieldCheck]:
    """Compare every field present on both sides; absent fields are skipped."""
    checks: list[FieldCheck] = []

    bib_title = entry.get("title", "")
    src_title = fields.get("title", "")
    if bib_title and src_title:
        checks.append(FieldCheck("title", bib_title[:80], src_title[:80],
                                 _title_match(bib_title, src_title), source))

    bib_author = entry.get("author", entry.get("editor", ""))
    src_authors = fields.get("author_names", [])
    if bib_author and src_authors:
        bib_short = ", ".join(bib_surnames(bib_author)[:3])
        src_short = ", ".join(_source_surname(n) for n in src_authors[:3] if n.strip())
        checks.append(FieldCheck("author", bib_short, src_short,
                                 _authors_match(bib_author, src_authors), source))

    bib_year = entry.get("year", "")
    src_year = fields.get("year", "")
    if bib_year and src_year:
        checks.append(FieldCheck("year", bib_year, src_year,
                                 bib_year.strip() == src_year.strip(), source))

    bib_journal = entry.get("journal", entry.get("booktitle", ""))
    src_journal = fields.get("journal", "")
    if bib_journal and src_journal:
        checks.append(FieldCheck("journal", bib_journal[:60], src_journal[:60],
                                 _journal_match(bib_journal, src_journal), source))

    bib_volume = entry.get("volume", "")
    src_volume = fields.get("volume", "")
    if bib_volume and src_volume:
        checks.append(FieldCheck("volume", bib_volume, src_volume,
                                 bib_volume.strip() == src_volume.strip(), source))

    bib_issue = entry.get("number", "")
    src_issue = fields.get("issue", "")
    if bib_issue and src_issue:
        checks.append(FieldCheck("issue", bib_issue, src_issue,
                                 _issue_match(bib_issue, src_issue), source))

    bib_pages = entry.get("pages", "")
    src_pages = fields.get("pages", "")
    if bib_pages and src_pages:
        checks.append(FieldCheck("pages", bib_pages, src_pages,
                                 _pages_match(bib_pages, src_pages), source))

    return checks
