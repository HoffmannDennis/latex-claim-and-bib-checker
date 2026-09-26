"""Command-line front end: one command that runs the reference check, the claim check or both."""

from __future__ import annotations

import argparse
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

from . import _http, _report
from ._abstract_sources import builtin_sources, metadata_sources
from ._db import AbstractDb, AbstractDbError
from ._document import Document, InputError, load_document
from ._judge import ClaimResult, judge
from ._llm import (
    CLOUD_KEY_ENV,
    CLOUD_PROVIDERS,
    LOCAL_URL_ENV,
    OpenAICompatibleClient,
    ProviderConfig,
    ProviderSetupError,
    choose_local_model,
    cloud_config,
    local_config,
    make_client,
)
from ._lookup import FOUND, Evidence, Lookup
from ._plugins import SourceInfo, SourceSetupError, build_registry, missing_env, select_sources
from ._references import EntryResult, EntryStatus, no_source_answered, verify_entries
from ._text import plain_latex
from ._version import __version__

PROG = "latex-claim-and-bib-checker"

EXIT_OK = 0
EXIT_FINDINGS = 1
EXIT_USAGE = 2
EXIT_NO_SOURCE_ANSWERED = 3
EXIT_PROVIDER_FAILED = 4

OUTPUT_SUFFIXES = (".md", ".xlsx")

EPILOG = f"""\
environment variables:
  OPENAI_API_KEY, GEMINI_API_KEY, ANTHROPIC_API_KEY
                       key of the --cloud provider (read only with --cloud)
  {LOCAL_URL_ENV}        local model server (default http://127.0.0.1:1234/v1, LM Studio).
                       Loopback and private network addresses only.
  OPENALEX_API_KEY     optional key for OpenAlex
  SPRINGER_API_KEY     enables the springer abstract source
  S2_API_KEY           optional key for Semantic Scholar
  CORE_API_KEY         optional key for CORE
  CONTACT_EMAIL        contact address sent with source requests

statuses per bibliography entry:
  MATCH                          at least one field compared, no difference
  MISMATCH_MINOR                 formatting-level differences only
  MISMATCH_MAJOR                 at least one substantive difference
  NOT_FOUND_IN_ENABLED_SOURCES   every applicable source answered, no matching record
  SOURCE_ERROR                   no record obtained, at least one source did not answer
  NOT_CHECKED                    no comparison possible (no applicable source or
                                 no comparable fields)

verdicts per citing sentence:
  SUPPORTED      a quoted passage of the abstract supports the sentence
  PLAUSIBLE      consistent with the abstract, but not stated there
  SUSPICIOUS     the abstract contradicts or does not support the sentence
  UNVERIFIABLE   the abstract is not enough to decide, or no abstract was found

exit codes (if several apply, the first in the order 4, 3, 1, 0):
  0  report produced, nothing that needs attention
  1  report produced with findings: a cited key without a bibliography entry,
     an entry that is MISMATCH_MAJOR or NOT_FOUND_IN_ENABLED_SOURCES, or a
     SUSPICIOUS claim
  2  usage, input or setup error, nothing was checked
  3  report produced, but in a check that ran no source answered any lookup
  4  report produced, but the model provider failed during the run
"""


class UsageError(Exception):
    """Invalid arguments, input or setup. Ends the run with exit code 2."""


@dataclass
class Settings:
    """Internal settings without a command-line option (used by tests and callers).

    ``sources`` restricts the sources by name and ``judge=False`` reports
    claims without asking a model.
    """

    sources: list[str] | None = None
    judge: bool = True


def _say(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=PROG,
        description=(
            "Check a LaTeX document: whether every citation has a bibliography entry, whether the "
            "BibTeX metadata agrees with Crossref, OpenAlex and DataCite, and whether the abstracts "
            "of the cited works support the sentences that cite them. A verification aid: "
            "verdicts rest on abstracts only."
        ),
        epilog=EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        allow_abbrev=False,
    )
    parser.add_argument("input", help="the LaTeX document (.tex) or a bibliography file (.bib)")
    parser.add_argument("--bib-check-only", action="store_true",
                        help="only check citations against the bibliography and the metadata, never use a model")
    parser.add_argument("--claim-check-only", action="store_true",
                        help="only check whether the cited abstracts support the citing sentences")
    parser.add_argument("--cloud", choices=CLOUD_PROVIDERS, metavar="PROVIDER",
                        help="judge claims with a cloud provider (openai, gemini or anthropic) instead of the "
                             "local model server. Needs --model and the provider's key.")
    parser.add_argument("--model", metavar="NAME",
                        help="model name. Required with --cloud. Locally only needed if the server offers "
                             "several models.")
    parser.add_argument("--abstract-db", type=Path, metavar="FILE",
                        help="read abstracts and metadata from a local SQLite file and add newly found ones "
                             "(created if needed)")
    parser.add_argument("-o", "--output", type=Path, metavar="FILE",
                        help="write the report to FILE (.md or .xlsx, an existing file is replaced). "
                             "Default: Markdown on standard output.")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    return parser


# ---------------------------------------------------------------------------
# Checks before anything is sent
# ---------------------------------------------------------------------------


def _check_arguments(args: argparse.Namespace, input_path: Path) -> None:
    if not input_path.is_file():
        raise UsageError(f"input file not found: {input_path.name}")
    out: Path | None = args.output
    if out is not None:
        if out.suffix.lower() not in OUTPUT_SUFFIXES:
            raise UsageError(f"unsupported report format '{out.suffix or out.name}', use .md or .xlsx")
        if out.is_dir():
            raise UsageError(f"{out.name} is a directory")
        if not out.parent.is_dir():
            raise UsageError(f"directory for {out.name} not found")
        if out.exists() and out.resolve() == input_path.resolve():
            raise UsageError("refusing to write the report over the input file")
    if args.bib_check_only:
        conflicting = [flag for flag, value in (("--claim-check-only", args.claim_check_only),
                                                ("--cloud", args.cloud), ("--model", args.model)) if value]
        if conflicting:
            raise UsageError(f"--bib-check-only cannot be combined with {', '.join(conflicting)}")
    if input_path.suffix.lower() == ".bib" and args.claim_check_only:
        raise UsageError("the claim check needs the LaTeX document, not a .bib file")


def _provider_config(args: argparse.Namespace) -> ProviderConfig:
    try:
        if args.cloud:
            return cloud_config(args.cloud, args.model)
        return local_config(os.environ.get(LOCAL_URL_ENV, "").strip() or None, args.model)
    except ProviderSetupError as exc:
        raise UsageError(str(exc)) from None


def _open_db(path: Path, readonly: bool) -> AbstractDb | None:
    """Open the database. A missing file is created unless ``readonly`` (then it counts as empty)."""
    if readonly and not path.expanduser().exists():
        return None
    try:
        return AbstractDb.open(path, readonly=readonly)
    except AbstractDbError as exc:
        raise UsageError(f"--abstract-db: {exc}") from None


def exit_code(*, provider_failed: bool, no_source: bool, findings: bool) -> int:
    """The exit code of a run that produced a report (precedence 4, 3, 1, 0)."""
    if provider_failed:
        return EXIT_PROVIDER_FAILED
    if no_source:
        return EXIT_NO_SOURCE_ANSWERED
    if findings:
        return EXIT_FINDINGS
    return EXIT_OK


# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------


@dataclass
class _Plan:
    """What this run does, settled before the first request."""

    document: Document
    references: bool
    claims: bool
    provider: ProviderConfig | None = None
    client: Any = None
    metadata: list[Any] | None = None  # metadata adapters for the reference check
    active: list[SourceInfo] | None = None  # abstract sources for the claim check
    registry: Any = None
    db: AbstractDb | None = None


def _run(args: argparse.Namespace, settings: Settings) -> int:
    input_path = Path(args.input)
    _check_arguments(args, input_path)
    is_bib = input_path.suffix.lower() == ".bib"
    references = not args.claim_check_only
    claims_wanted = not args.bib_check_only and not is_bib
    provider = _provider_config(args) if claims_wanted and settings.judge else None

    try:
        document = load_document(input_path)
    except InputError as exc:
        raise UsageError(str(exc)) from None
    for note in document.notes:
        _say(f"warning: {note}")

    skipped: list[dict[str, str]] = []
    claims = claims_wanted and bool(document.entries)
    if claims_wanted and not claims:
        if args.claim_check_only:
            raise UsageError("no bibliography entries found. The claim check needs a .bib file declared with "
                             "\\bibliography{...} or \\addbibresource{...}")
        skipped.append({"part": "claims", "reason": "no .bib entries found"})

    plan = _Plan(document, references, claims, provider)
    session = _http.new_session()
    contact = (os.environ.get("CONTACT_EMAIL") or "").strip() or None
    metadata = metadata_sources(session, contact)
    plan.metadata = [s for s in metadata if settings.sources is None or s.name in settings.sources]
    if claims:
        try:
            registry, warnings = build_registry(builtin_sources(session, contact, metadata=metadata))
            for warning in warnings:
                _say(warning)
            plan.registry = registry
            plan.active = select_sources(registry, settings.sources)
        except SourceSetupError as exc:
            raise UsageError(str(exc)) from None
        if provider is not None:
            plan.client = make_client(provider, _http.new_session())
            if provider.mode == "local":
                assert isinstance(plan.client, OpenAICompatibleClient)
                try:
                    provider.model = choose_local_model(plan.client, provider.model)
                except ProviderSetupError as exc:
                    raise UsageError(str(exc)) from None
    if args.abstract_db is not None:
        plan.db = _open_db(args.abstract_db, readonly=not claims)

    try:
        return _check(args, plan, skipped)
    finally:
        if plan.db is not None:
            plan.db.close()


def _sources_key_missing(registry: Any) -> list[dict[str, Any]]:
    missing: list[dict[str, Any]] = []
    for info in registry.sorted() if registry is not None else ():
        if not info.available:
            names = missing_env(info, os.environ)
            if names:
                missing.append({"source": info.name, "env": names})
    return missing


def _check(args: argparse.Namespace, plan: _Plan, skipped: list[dict[str, str]]) -> int:
    document = plan.document
    meta = _report.ReportMeta(input_name=document.input_name, bib_names=list(document.bib_names),
                              notes=list(document.notes), skipped=skipped)

    entries: list[EntryResult] | None = None
    if plan.references and document.bib_entries:
        meta.parts.append("references")
        meta.metadata_sources = [s.name for s in plan.metadata or []]
        _say(f"Checking {len(document.bib_entries)} entries against {', '.join(meta.metadata_sources)}")
        entries = verify_entries(document.bib_entries, plan.metadata, db=plan.db)
    elif plan.references and document.consistency is not None:
        meta.parts.append("references")

    results: list[ClaimResult] | None = None
    lookup: Lookup | None = None
    keys: list[str] = []
    if plan.claims:
        meta.parts.append("claims")
        meta.abstract_sources = [s.name for s in plan.active or []]
        meta.sources_key_missing = _sources_key_missing(plan.registry)
        records: dict[str, list[Any]] = {}
        for entry in entries or ():
            records.setdefault(entry.key, entry.used)
        lookup = Lookup(plan.active or [], plan.db, warn=_say, records=records)
        claims = document.claims
        _say(f"{len(claims)} citing sentences found in {document.input_name}")
        evidence: dict[str, Evidence] = {}
        keys = list(dict.fromkeys(c.cite_key for c in claims))
        if keys:
            _say(f"Looking up {len(keys)} cited works in: {', '.join(meta.abstract_sources)}")
        for number, key in enumerate(keys, 1):
            ev = lookup.lookup(key, document.entries.get(key))
            evidence[key] = ev
            where = f" ({ev.source})" if ev.lookup_status == FOUND else ""
            _say(f"  [{number}/{len(keys)}] {key}: {ev.lookup_status}{where}")
        results = [
            ClaimResult(claim=c, title=plain_latex((document.entries.get(c.cite_key) or {}).get("title")),
                        evidence=evidence[c.cite_key])
            for c in claims
        ]
        meta.mode = "no-judge" if plan.provider is None else plan.provider.mode

    failure: str | None = None
    if results is not None and plan.provider is not None and plan.client is not None:
        provider, client = plan.provider, plan.client
        meta.provider, meta.model = provider.provider, provider.model
        if provider.mode == "cloud":
            _say(f"Sending claims and abstracts to {provider.provider} ({provider.model}).")
        failure = judge(results, client, progress=_say)
        meta.model_requests = client.usage.calls
        meta.input_tokens = client.usage.input_tokens
        meta.output_tokens = client.usage.output_tokens
        meta.cached_tokens = client.usage.cached_tokens
        if failure:
            meta.notes.append(f"The model provider failed during the run: {failure}")
            _say(f"error: {failure}")

    # The citation consistency belongs to the reference check (not part of --claim-check-only).
    consistency = document.consistency if plan.references else None
    data = _report.build_report(meta, consistency, entries, results)
    _report.write_report(data, args.output)
    if args.output is not None:
        _say(f"Report written to {args.output}")

    no_source = False
    if entries and no_source_answered(entries):
        _say("error: no metadata source answered any lookup (network down or rate limited?)")
        no_source = True
    if lookup is not None and keys and lookup.stats.db_hits == 0 and lookup.stats.answered == 0 \
            and lookup.stats.errors > 0:
        _say("error: no abstract source answered any lookup (network down or rate limited?)")
        no_source = True
    findings = bool(consistency and consistency.cited_but_undefined) or any(
        e.status in (EntryStatus.MISMATCH_MAJOR, EntryStatus.NOT_FOUND_IN_ENABLED_SOURCES) for e in entries or ()
    ) or any(r.verdict == "SUSPICIOUS" for r in results or ())
    return exit_code(provider_failed=bool(failure), no_source=no_source, findings=findings)


def _os_error_text(exc: OSError) -> str:
    """The error text with file names only, never absolute paths."""
    if exc.strerror:
        names = [Path(str(f)).name for f in (exc.filename, exc.filename2) if f is not None]
        return f"{exc.strerror}: {', '.join(names)}" if names else exc.strerror
    return _http.redact(str(exc))


def main(argv: Sequence[str] | None = None, *, sources: list[str] | None = None, judge: bool = True) -> int:
    """Run the command line and return the exit code.

    The keyword arguments are internal settings without a command-line
    option (see :class:`Settings`).
    """
    parser = build_parser()
    args = parser.parse_args(argv)
    settings = Settings(sources=sources, judge=judge)
    try:
        return _run(args, settings)
    except UsageError as exc:
        _say(f"error: {exc}")
        return EXIT_USAGE
    except OSError as exc:
        _say(f"error: {type(exc).__name__}: {_os_error_text(exc)}")
        return EXIT_USAGE


__all__ = ["main", "build_parser", "exit_code", "CLOUD_KEY_ENV"]
