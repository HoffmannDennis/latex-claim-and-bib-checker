"""Check a LaTeX document: citations against the bibliography, BibTeX metadata
against Crossref, OpenAlex and DataCite, and whether the abstracts of the cited
works support the sentences that cite them.

The command line (``latex-claim-and-bib-checker``) is the main interface. The
names below are the stable interface for additional abstract sources. Such a
source is installed as a separate package that registers a factory under the
entry-point group :data:`ENTRY_POINT_GROUP` and implements the
:class:`Source` protocol (``name``, ``order``, ``available()`` and
``fetch(query)`` returning a :class:`Result`).
"""

from ._plugins import ENTRY_POINT_GROUP
from ._types import Query, Result, Source, Status, normalize_doi
from ._version import __version__

__all__ = ["ENTRY_POINT_GROUP", "Query", "Result", "Source", "Status", "normalize_doi"]
