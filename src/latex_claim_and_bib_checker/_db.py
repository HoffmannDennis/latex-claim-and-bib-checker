"""Optional local SQLite file with abstracts (off unless a path is given).

A new file gets exactly one table::

    abstracts(doi TEXT PRIMARY KEY, abstract TEXT, provider TEXT NOT NULL,
              outcome TEXT NOT NULL, fetched_at TEXT NOT NULL, raw_json TEXT)

in WAL mode. An existing file is only checked for a table ``abstracts`` with
the five columns ``doi``, ``abstract``, ``provider``, ``outcome`` and
``fetched_at``. Its schema, journal mode and existing rows are never
changed. Rows are only ever added (``INSERT OR IGNORE``), one statement per
row in autocommit mode, so no write transaction stays open while sources
are queried.

When the table has a ``raw_json`` column, rows written by this tool carry
compact bibliographic metadata in the format described by
:func:`decode_metadata`. Anything else in ``raw_json`` is ignored.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ._types import normalize_doi

REQUIRED_COLUMNS = ("doi", "abstract", "provider", "outcome", "fetched_at")
BUSY_TIMEOUT_MS = 5000

METADATA_FORMAT = "latex-claim-and-bib-checker/metadata-1"
METADATA_SOURCES = ("crossref", "openalex", "datacite")
_STRING_FIELDS = ("title", "year", "journal", "volume", "issue", "pages")
_KNOWN_FIELDS = ("title", "author_names", "year", "journal", "volume", "issue", "pages")

_CREATE = (
    "CREATE TABLE abstracts ("
    "doi TEXT PRIMARY KEY, abstract TEXT, provider TEXT NOT NULL, "
    "outcome TEXT NOT NULL, fetched_at TEXT NOT NULL, raw_json TEXT)"
)
_INSERT = (
    "INSERT OR IGNORE INTO abstracts (doi, abstract, provider, outcome, fetched_at) "
    "VALUES (?, ?, ?, 'FOUND', ?)"
)
_INSERT_WITH_METADATA = (
    "INSERT OR IGNORE INTO abstracts (doi, abstract, provider, outcome, fetched_at, raw_json) "
    "VALUES (?, ?, ?, 'FOUND', ?, ?)"
)
_SELECT = "SELECT abstract, provider FROM abstracts WHERE doi = ? AND outcome = 'FOUND'"
_SELECT_WITH_METADATA = (
    "SELECT abstract, provider, raw_json FROM abstracts WHERE doi = ? AND outcome = 'FOUND'"
)
_SELECT_METADATA = "SELECT raw_json FROM abstracts WHERE doi = ?"


class AbstractDbError(Exception):
    """The file cannot be used. The run stops before anything is written."""


# ---------------------------------------------------------------------------
# Stored metadata (the ``raw_json`` column)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class StoredMetadata:
    """Checked bibliographic metadata of one row."""

    source: str  # crossref, openalex or datacite
    matched_by: str  # always "doi"
    doi: str  # normalised, equal to the row's DOI
    fetched_at: str  # ISO 8601 with time zone
    fields: dict[str, Any]  # only the known comparison fields


def decode_metadata(raw: Any, row_doi: Any) -> StoredMetadata | None:
    """The checked metadata in ``raw``, or ``None`` if ``raw`` is not usable.

    Usable means: a JSON object with ``format`` equal to
    :data:`METADATA_FORMAT`, ``source`` one of :data:`METADATA_SOURCES`,
    ``matched_by`` equal to ``"doi"``, a string ``doi`` whose normalised
    value starts with ``10.`` and equals the normalised ``row_doi``, an
    ISO 8601 ``fetched_at`` with a time zone and an object ``fields``. The
    comparison fields ``title``, ``year``, ``journal``, ``volume``,
    ``issue`` and ``pages`` must be strings and ``author_names`` a list of
    strings where present. Only these fields are returned. Anything else,
    including malformed JSON, gives ``None`` and never raises.
    """
    if not isinstance(raw, str) or not raw.strip():
        return None
    try:
        data = json.loads(raw)
    except (ValueError, RecursionError):
        return None
    if not isinstance(data, dict) or data.get("format") != METADATA_FORMAT:
        return None
    source, matched_by = data.get("source"), data.get("matched_by")
    if source not in METADATA_SOURCES or matched_by != "doi":
        return None
    doi = data.get("doi")
    if not isinstance(doi, str) or not isinstance(row_doi, str):
        return None
    doi_norm, row_norm = normalize_doi(doi), normalize_doi(row_doi)
    if not doi_norm or not doi_norm.startswith("10.") or doi_norm != row_norm:
        return None
    fetched_at = data.get("fetched_at")
    if not isinstance(fetched_at, str):
        return None
    try:
        stamp = datetime.fromisoformat(fetched_at)
    except ValueError:
        return None
    if stamp.tzinfo is None or stamp.utcoffset() is None:
        return None
    fields = data.get("fields")
    if not isinstance(fields, dict):
        return None
    for name in _STRING_FIELDS:
        if name in fields and not isinstance(fields[name], str):
            return None
    if "author_names" in fields:
        names = fields["author_names"]
        if not isinstance(names, list) or not all(isinstance(n, str) for n in names):
            return None
    known = {name: fields[name] for name in _KNOWN_FIELDS if name in fields}
    return StoredMetadata(source, "doi", doi_norm, fetched_at, known)


def encode_metadata(source: str, doi: str, fields: dict[str, Any], fetched_at: str | None = None) -> str | None:
    """Compact ``raw_json`` for a record found by DOI, or ``None`` if it would not decode."""
    payload = {
        "format": METADATA_FORMAT,
        "source": source,
        "matched_by": "doi",
        "doi": doi,
        "fetched_at": fetched_at or datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "fields": fields,
    }
    try:
        text = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    except (TypeError, ValueError):
        return None
    return text if decode_metadata(text, doi) is not None else None


# ---------------------------------------------------------------------------
# The file
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class StoredRow:
    abstract: str
    provider: str
    raw_json: Any = None


def _connect(path: Path, readonly: bool) -> sqlite3.Connection:
    if readonly:
        # Read-only access must leave no trace. A WAL-mode file would get -wal/-shm
        # files even with mode=ro. Without such files there is no pending write,
        # so the file can be opened as immutable, which creates no files at all.
        side_files = any(Path(f"{path}{suffix}").exists() for suffix in ("-wal", "-shm", "-journal"))
        uri = f"{path.resolve().as_uri()}?mode=ro" + ("" if side_files else "&immutable=1")
        conn = sqlite3.connect(uri, uri=True, isolation_level=None, timeout=BUSY_TIMEOUT_MS / 1000)
    else:
        conn = sqlite3.connect(str(path), isolation_level=None, timeout=BUSY_TIMEOUT_MS / 1000)
    conn.execute(f"PRAGMA busy_timeout = {int(BUSY_TIMEOUT_MS)}")
    return conn


def _columns(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute("PRAGMA table_info(abstracts)").fetchall()
    return {str(row[1]).lower() for row in rows}


class AbstractDb:
    """Read and append access to the abstract file."""

    def __init__(self, conn: sqlite3.Connection, readonly: bool, has_metadata: bool = False) -> None:
        self._conn = conn
        self.readonly = readonly
        self.has_metadata = has_metadata  # the table has a raw_json column

    @classmethod
    def open(cls, path: str | Path, *, readonly: bool = False) -> "AbstractDb":
        db_path = Path(path).expanduser()
        if db_path.exists():
            if not db_path.is_file():
                raise AbstractDbError("not a compatible abstract database")
            try:
                conn = _connect(db_path, readonly)
                present = _columns(conn)
            except sqlite3.DatabaseError:
                raise AbstractDbError("not a compatible abstract database") from None
            if not set(REQUIRED_COLUMNS) <= present:
                conn.close()
                raise AbstractDbError("not a compatible abstract database")
            return cls(conn, readonly, "raw_json" in present)

        if readonly:
            raise AbstractDbError("abstract database not found (read-only access needs an existing file)")
        if not db_path.parent.is_dir():
            raise AbstractDbError("cannot create the abstract database: its directory does not exist")
        try:
            conn = _connect(db_path, False)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute(_CREATE)
        except sqlite3.DatabaseError as exc:
            raise AbstractDbError(f"cannot create the abstract database: {exc}") from None
        return cls(conn, False, True)

    def _fetch(self, sql: str, doi: str) -> list[tuple]:
        try:
            cursor = self._conn.execute(sql, (doi,))
            try:
                return cursor.fetchall()
            finally:
                cursor.close()
        except sqlite3.DatabaseError:
            return []

    def row(self, doi: str) -> StoredRow | None:
        """The ``FOUND`` row for ``doi`` (abstract, provider and, if present, ``raw_json``)."""
        sql = _SELECT_WITH_METADATA if self.has_metadata else _SELECT
        for values in self._fetch(sql, doi):
            if isinstance(values[0], str):
                return StoredRow(values[0], str(values[1]), values[2] if self.has_metadata else None)
        return None

    def metadata(self, doi: str) -> StoredMetadata | None:
        """Checked metadata stored for ``doi``, or ``None`` (see :func:`decode_metadata`)."""
        if not self.has_metadata:
            return None
        for (raw,) in self._fetch(_SELECT_METADATA, doi):
            return decode_metadata(raw, doi)
        return None

    def add(self, doi: str, abstract: str, provider: str, raw_json: str | None = None) -> bool:
        """Append one row. Never replaces an existing one. False if it could not be written."""
        if self.readonly:
            return False
        fetched_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        try:
            if self.has_metadata:
                self._conn.execute(_INSERT_WITH_METADATA, (doi, abstract, provider, fetched_at, raw_json))
            else:
                self._conn.execute(_INSERT, (doi, abstract, provider, fetched_at))
        except sqlite3.DatabaseError:
            return False
        return True

    def close(self) -> None:
        self._conn.close()
