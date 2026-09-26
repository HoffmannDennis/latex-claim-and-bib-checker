"""Source objects used as entry-point targets by the plugin tests (never installed)."""

from __future__ import annotations

from latex_claim_and_bib_checker import Query, Result, Status

ABSTRACT = (
    "An example plugin abstract about simulated widget markets, long enough to pass the length "
    "rule, describing how the widget index predicts returns."
)


class ExampleSource:
    name = "example-source"
    order = 0

    def available(self) -> bool:
        return True

    def fetch(self, query: Query) -> Result:
        if not query.doi:
            return Result(Status.SKIPPED, None, None, self.name, None, "needs a DOI")
        return Result(Status.FOUND, {"doi": query.doi}, ABSTRACT, "example-endpoint",
                      f"https://doi.org/{query.doi}", None, "doi", query.doi)


class RaisingFetch:
    name = "raising-fetch"
    order = 0

    def available(self) -> bool:
        return True

    def fetch(self, query: Query) -> Result:
        raise RuntimeError("backend exploded")


class CrossrefClash:
    name = "crossref"
    order = 1

    def available(self) -> bool:
        return True

    def fetch(self, query: Query) -> Result:  # pragma: no cover
        raise AssertionError


def make_example() -> ExampleSource:
    return ExampleSource()


def make_raising_fetch() -> RaisingFetch:
    return RaisingFetch()


def make_clash() -> CrossrefClash:
    return CrossrefClash()


def failing_factory():
    raise ValueError("factory cannot build the source")
