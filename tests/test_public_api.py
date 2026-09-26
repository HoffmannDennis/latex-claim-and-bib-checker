"""The frozen public surface of the package: the interface for additional abstract sources."""

from __future__ import annotations

import latex_claim_and_bib_checker as pkg

EXPECTED = {"Query", "Result", "Source", "Status", "normalize_doi", "ENTRY_POINT_GROUP"}


def test_all_is_exactly_the_frozen_api():
    assert set(pkg.__all__) == EXPECTED
    assert len(pkg.__all__) == len(EXPECTED)
    for name in EXPECTED:
        assert hasattr(pkg, name)
    assert pkg.ENTRY_POINT_GROUP == "latex_claim_and_bib_checker.sources"
