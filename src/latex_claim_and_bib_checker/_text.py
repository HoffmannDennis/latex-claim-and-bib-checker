"""Text normalisation shared by the prompt, the report, the quote check and the database.

``clean_text`` is the single cleaning rule for abstract text: markup is
removed, entities are decoded, the text is NFKC-normalised and whitespace
is collapsed. Case and punctuation are kept. An abstract is usable when its
cleaned form has between :data:`MIN_ABSTRACT_CHARS` and
:data:`MAX_ABSTRACT_CHARS` characters; text is never shortened.
"""

from __future__ import annotations

import html
import re
import unicodedata

from ._compare import _title_match

MIN_ABSTRACT_CHARS = 50
MAX_ABSTRACT_CHARS = 7000

# Tags whose boundaries separate words (paragraphs, headings, list items ...).
_BLOCK_TAGS = frozenset({
    "p", "title", "sec", "section", "br", "div", "li", "ul", "ol", "list", "list-item",
    "h1", "h2", "h3", "h4", "h5", "h6", "abstract", "table", "tr", "td", "th", "label",
    "caption", "blockquote", "dd", "dt", "hr",
})
_TAG_RE = re.compile(r"<\s*/?\s*(?:[A-Za-z][\w.-]*:)?([A-Za-z][\w.-]*)\b[^<>]*>")
_SPACE_RE = re.compile(r"\s+")


def _strip_tags(text: str) -> str:
    def replace(match: re.Match[str]) -> str:
        return " " if match.group(1).lower() in _BLOCK_TAGS else ""

    return _TAG_RE.sub(replace, text)


def clean_text(text: str | None) -> str:
    """Return ``text`` without JATS/HTML markup, NFKC-normalised, whitespace collapsed."""
    if not text:
        return ""
    value = _strip_tags(str(text))
    value = html.unescape(value)
    value = _strip_tags(value)  # markup that arrived entity-encoded
    value = unicodedata.normalize("NFKC", value)
    value = value.replace("\u00ad", "")  # soft hyphen
    return _SPACE_RE.sub(" ", value).strip()


def usable_abstract(text: str | None) -> str | None:
    """The cleaned text if it is usable as an abstract, otherwise ``None``."""
    cleaned = clean_text(text)
    if MIN_ABSTRACT_CHARS <= len(cleaned) <= MAX_ABSTRACT_CHARS:
        return cleaned
    return None


_QUOTE_MAP = str.maketrans({
    "\u2018": "'", "\u2019": "'", "\u201a": "'", "\u201b": "'", "\u2032": "'",
    "`": "'", "\u00b4": "'",
    "\u201c": '"', "\u201d": '"', "\u201e": '"', "\u201f": '"', "\u2033": '"',
    "\u00ab": '"', "\u00bb": '"',
    "\u2010": "-", "\u2011": "-", "\u2012": "-", "\u2013": "-", "\u2014": "-",
    "\u2015": "-", "\u2212": "-", "\ufe58": "-", "\ufe63": "-",
})


def match_key(text: str | None) -> str:
    """Comparison form used only by the quote check.

    ``clean_text``, then typographic quotes and dashes unified, then
    case-folded.
    """
    value = clean_text(text).translate(_QUOTE_MAP).replace("''", '"')
    return _SPACE_RE.sub(" ", value.casefold()).strip()


_QUOTE_EDGES = "\"' "


def quote_in_abstract(quote: str | None, abstract: str | None) -> bool:
    """True if ``quote`` occurs verbatim in ``abstract`` (compared via :func:`match_key`).

    Surrounding quotation marks and a final full stop of the quote are
    ignored. A quote needs at least three words.
    """
    needle = match_key(quote).strip(_QUOTE_EDGES).rstrip(".").strip(_QUOTE_EDGES)
    if len(needle.split()) < 3:
        return False
    return needle in match_key(abstract)


# ---------------------------------------------------------------------------
# LaTeX to plain text (for titles in prompts and reports)
# ---------------------------------------------------------------------------

_ACCENTS = {
    '"': "\u0308", "'": "\u0301", "`": "\u0300", "^": "\u0302", "~": "\u0303",
    "=": "\u0304", ".": "\u0307", "c": "\u0327", "v": "\u030c", "u": "\u0306",
    "H": "\u030b", "k": "\u0328", "r": "\u030a",
}
_ACCENT_RE = re.compile(r"\\([\"'`^~=.]|[cvuHkr](?![A-Za-z]))\s*\{?\s*([A-Za-z])\s*\}?")
_SPECIALS = {
    r"\&": "&", r"\%": "%", r"\_": "_", r"\$": "$", r"\#": "#",
    r"\ss": "ß", r"\o": "ø", r"\O": "Ø", r"\ae": "æ", r"\AE": "Æ",
    r"\l": "ł", r"\L": "Ł", r"\aa": "å", r"\AA": "Å", r"\i": "ı",
}
_SPECIAL_RE = re.compile(
    "|".join(re.escape(k) + (r"(?![A-Za-z])" if k[-1].isalpha() else "")
             for k in sorted(_SPECIALS, key=len, reverse=True))
)
_COMMAND_RE = re.compile(r"\\[A-Za-z]+\*?\s*")


def plain_latex(text: str | None) -> str:
    """Rough plain-text rendering of a BibTeX field (braces and commands removed)."""
    if not text:
        return ""
    value = _ACCENT_RE.sub(lambda m: unicodedata.normalize("NFC", m.group(2) + _ACCENTS[m.group(1)]), text)
    value = _SPECIAL_RE.sub(lambda m: _SPECIALS[m.group(0)], value)
    value = _COMMAND_RE.sub("", value)
    value = value.replace("{", "").replace("}", "").replace("~", " ")
    value = value.replace("---", "\u2014").replace("--", "\u2013")
    return _SPACE_RE.sub(" ", value).strip()


# ---------------------------------------------------------------------------
# Title agreement (is a source record about the cited work?)
# ---------------------------------------------------------------------------


def titles_agree(bib_title: str | None, record_title: str | None) -> bool:
    """True if a source record's title names the work of the bibliography entry.

    The titles agree when the metadata check's title rule
    (:func:`._compare._title_match`) accepts them. A missing title on either
    side is not compared (the titles agree), so callers decide where a
    record without a title is acceptable.
    """
    if not (bib_title or "").strip() or not (record_title or "").strip():
        return True
    return _title_match(bib_title, record_title)
