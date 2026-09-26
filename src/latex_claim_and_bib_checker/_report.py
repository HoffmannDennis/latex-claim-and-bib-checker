"""The combined report: one data structure, rendered as Markdown or Excel.

:func:`build_report` collects everything a report shows in plain, JSON
compatible data. :func:`render_markdown` and :func:`write_xlsx` only read
that structure, so both formats always show the same results.

Reports name the input by file name only and never contain absolute paths,
host or user names, or the location of an abstract database.
"""

from __future__ import annotations

import re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ._judge import ClaimResult
from ._references import ConsistencyResult, EntryResult, EntryStatus, as_dict
from ._version import __version__

TOOL = "latex-claim-and-bib-checker"
POSITIONING = (
    "This report is a verification aid. Verdicts are based on "
    "abstracts only and must be checked by a person against the full text."
)

STATUS_MEANING = {
    EntryStatus.MATCH: "at least one field compared, no difference",
    EntryStatus.MISMATCH_MINOR: "formatting-level differences only (e.g. diacritics, year off by one)",
    EntryStatus.MISMATCH_MAJOR: "at least one substantive difference",
    EntryStatus.NOT_FOUND_IN_ENABLED_SOURCES: "every applicable enabled source answered without a matching record",
    EntryStatus.SOURCE_ERROR: "no record obtained and at least one source did not answer",
    EntryStatus.NOT_CHECKED: "no comparison possible (see note)",
}
VERDICT_ORDER = ("SUSPICIOUS", "UNVERIFIABLE", "PLAUSIBLE", "SUPPORTED")
VERDICT_HINTS = {
    "SUSPICIOUS": "the abstract appears to contradict or not to support the sentence, review first",
    "UNVERIFIABLE": "the abstract alone is not enough to decide, check the full text",
    "PLAUSIBLE": "consistent with the abstract but not stated there, spot-check",
    "SUPPORTED": "the quoted passage of the abstract supports the sentence",
}
LOOKUP_ORDER = ("found", "absent", "source_error")

CLAIM_COLUMNS = (
    "index", "sentence", "cite_key", "title", "verdict", "evidence_type", "evidence_source",
    "evidence_origin", "evidence_match", "lookup_status", "lookup_reason", "evidence_quote",
    "rationale", "resolved_doi", "public_url",
)
REFERENCE_COLUMNS = (
    "key", "bib_file", "entry_type", "doi", "status", "reason", "compared_with", "origin",
    "metadata_date", "resolved_doi", "public_url", "differences",
)


@dataclass
class ReportMeta:
    input_name: str
    bib_names: list[str] = field(default_factory=list)
    parts: list[str] = field(default_factory=list)  # checks that ran: "references", "claims"
    mode: str | None = None  # "local", "cloud", "no-judge" or None (no claim check)
    provider: str | None = None
    model: str | None = None
    metadata_sources: list[str] = field(default_factory=list)
    abstract_sources: list[str] = field(default_factory=list)
    sources_key_missing: list[dict[str, Any]] = field(default_factory=list)  # {"source", "env"}
    model_requests: int | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    cached_tokens: int | None = None
    notes: list[str] = field(default_factory=list)
    skipped: list[dict[str, str]] = field(default_factory=list)  # {"part", "reason"}
    generated: str = field(default_factory=lambda: time.strftime("%Y-%m-%d %H:%M"))


# ---------------------------------------------------------------------------
# Data
# ---------------------------------------------------------------------------


def claim_row(result: ClaimResult) -> dict[str, Any]:
    ev = result.evidence
    return {
        "index": result.claim.index,
        "sentence": result.claim.sentence,
        "cite_key": result.claim.cite_key,
        "title": result.title,
        "verdict": result.verdict,
        "evidence_type": result.evidence_type,
        "evidence_source": ev.source,
        "evidence_origin": ev.origin,
        "evidence_match": ev.match,
        "lookup_status": ev.lookup_status,
        "lookup_reason": ev.reason,
        "evidence_quote": result.evidence_quote,
        "rationale": result.rationale,
        "resolved_doi": ev.resolved_doi,
        "public_url": ev.public_url,
    }


def _consistency(result: ConsistencyResult | None) -> dict[str, Any] | None:
    if result is None:
        return None
    return {
        "bib_format": result.bib_format,
        "consistent": result.is_consistent,
        "nocite_all": result.nocite_all,
        "cited_keys": sorted(result.cited_keys),
        "defined_keys": sorted(result.defined_keys),
        "cited_but_undefined": sorted(result.cited_but_undefined),
        "defined_but_uncited": sorted(result.defined_but_uncited),
    }


def _claim_summary(rows: list[dict[str, Any]]) -> dict[str, dict[str, int]]:
    verdicts: dict[str, int] = {}
    lookups: dict[str, int] = {}
    for row in rows:
        if row["verdict"]:
            verdicts[row["verdict"]] = verdicts.get(row["verdict"], 0) + 1
        lookups[row["lookup_status"]] = lookups.get(row["lookup_status"], 0) + 1
    return {"verdicts": verdicts, "lookup_status": lookups}


def build_report(meta: ReportMeta, consistency: ConsistencyResult | None,
                 entries: list[EntryResult] | None, claims: list[ClaimResult] | None) -> dict[str, Any]:
    """Everything a report shows, as plain data."""
    references = None
    if entries is not None:
        counts = {status.value: 0 for status in EntryStatus}
        for entry in entries:
            counts[entry.status.value] += 1
        references = {
            "sources": list(meta.metadata_sources),
            "summary": counts,
            "entries": [as_dict(e) for e in entries],
        }
    rows = [claim_row(r) for r in sorted(claims or [], key=lambda r: r.claim.index)]
    header: dict[str, Any] = {
        "tool": TOOL,
        "version": __version__,
        "generated": meta.generated,
        "input": meta.input_name,
        "bibliography": list(meta.bib_names),
        "parts": list(meta.parts),
        "mode": meta.mode,
        "provider": meta.provider,
        "model": meta.model,
        "metadata_sources": list(meta.metadata_sources),
        "abstract_sources": list(meta.abstract_sources),
        "sources_key_missing": [dict(item) for item in meta.sources_key_missing],
    }
    for name in ("model_requests", "input_tokens", "output_tokens", "cached_tokens"):
        value = getattr(meta, name)
        if value is not None:
            header[name] = value
    return {
        **header,
        "note": POSITIONING,
        "notes": list(meta.notes),
        "skipped": [dict(item) for item in meta.skipped],
        "consistency": _consistency(consistency),
        "references": references,
        "claims_run": claims is not None,
        "claims_summary": _claim_summary(rows),
        "claims": rows,
    }


# ---------------------------------------------------------------------------
# Markdown
# ---------------------------------------------------------------------------

_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")


def _plain(value: Any) -> str:
    if value is None:
        return ""
    return _CONTROL_RE.sub("", str(value))


def _key_missing(items: list[dict[str, Any]], quote: str = "") -> str:
    return ", ".join(f"{item['source']} ({', '.join(quote + name + quote for name in item['env'])})" for item in items)


_MD_SPECIAL_RE = re.compile(r"([<>\[\]!|])")
_HTTP_URL_RE = re.compile(r"https?://\S+")
_URL_ESCAPES = str.maketrans({c: f"%{ord(c):02X}" for c in "()<>[]|\"\\"})


def _md(text: Any) -> str:
    """Text for Markdown: no markup, HTML, link or image can come from it."""
    value = _plain(text).replace("\\", "\\\\")
    return _MD_SPECIAL_RE.sub(r"\\\1", value).replace("\n", " ")


def _link(label: str, url: Any) -> str:
    """A Markdown link for an http(s) URL, otherwise the label and URL as escaped text.

    Characters that could end or break the link target are percent-encoded.
    """
    if not url:
        return _md(label)
    if _HTTP_URL_RE.fullmatch(str(url)):
        return f"[{_md(label)}]({str(url).translate(_URL_ESCAPES)})"
    return _md(f"{label} ({url})")


def _bare_link(url: Any) -> str:
    """The URL as is when it is http(s) and needs no encoding, otherwise as :func:`_link` or escaped text."""
    text = str(url)
    if not _HTTP_URL_RE.fullmatch(text):
        return _md(text)
    return text if text.translate(_URL_ESCAPES) == text else _link(text, text)


def _cell(text: Any) -> str:
    return _md(text).strip() or "—"


def _code(text: Any) -> str:
    return "`" + _plain(text).replace("`", "'") + "`"


def _header_md(data: dict[str, Any]) -> list[str]:
    lines = ["# Citation and bibliography report", ""]
    lines.append(f"- Tool: {TOOL} {data['version']}")
    lines.append(f"- Date: {data['generated']}")
    lines.append(f"- Input: {_md(data['input'])}")
    if data["bibliography"]:
        lines.append(f"- Bibliography: {_md(', '.join(data['bibliography']))}")
    lines.append(f"- Checks run: {', '.join(data['parts']) if data['parts'] else 'none'}")
    if data["mode"]:
        lines.append(f"- Mode: {data['mode']}")
    if data["provider"]:
        lines.append(f"- Provider: {_md(data['provider'])}")
    if data["model"]:
        lines.append(f"- Model: {_md(data['model'])}")
    if data["metadata_sources"]:
        lines.append(f"- Metadata sources: {', '.join(data['metadata_sources'])}")
    if data["abstract_sources"]:
        lines.append(f"- Abstract sources: {', '.join(data['abstract_sources'])}")
    if data["sources_key_missing"]:
        lines.append(f"- Sources not used, key missing: {_key_missing(data['sources_key_missing'], '`')}")
    if "model_requests" in data:
        lines.append(f"- Model requests: {data['model_requests']}")
    tokens = [f"{name.replace('_', ' ')} {data[name]}" for name in ("input_tokens", "output_tokens",
                                                                    "cached_tokens") if name in data]
    if tokens:
        lines.append(f"- Tokens reported: {', '.join(tokens)}")
    lines += ["", f"> {POSITIONING}", ""]
    for item in data["skipped"]:
        lines.append(f"- Skipped: {item['part']} ({_md(item['reason'])})")
    for note in data["notes"]:
        lines.append(f"- Note: {_md(note)}")
    if data["skipped"] or data["notes"]:
        lines.append("")
    return lines


def _summary_md(data: dict[str, Any]) -> list[str]:
    lines = ["## Summary", ""]
    consistency = data["consistency"]
    references = data["references"]
    if consistency is not None or references is not None:
        lines += ["| References | Count |", "|---|---|"]
        if consistency is not None:
            lines.append(f"| Cited but undefined | {len(consistency['cited_but_undefined'])} |")
            lines.append(f"| Defined but uncited | {len(consistency['defined_but_uncited'])} |")
        if references is not None:
            for status, count in references["summary"].items():
                lines.append(f"| {status} | {count} |")
        lines.append("")
    if data["claims_run"]:
        summary = data["claims_summary"]
        lines += ["| Claims | Count |", "|---|---|", f"| Claims | {len(data['claims'])} |"]
        for verdict in VERDICT_ORDER:
            if verdict in summary["verdicts"]:
                lines.append(f"| {verdict} | {summary['verdicts'][verdict]} |")
        for status in LOOKUP_ORDER:
            if status in summary["lookup_status"]:
                lines.append(f"| lookup {status} | {summary['lookup_status'][status]} |")
        lines.append("")
    return lines


def _consistency_md(data: dict[str, Any]) -> list[str]:
    result = data["consistency"]
    lines = ["### Citation consistency", ""]
    bibs = ", ".join(data["bibliography"]) if data["bibliography"] else result["bib_format"]
    lines.append(f"**Bibliography:** {_md(bibs)}")
    lines.append("")
    lines += [
        "| Metric | Count |",
        "|---|---|",
        f"| Cited keys | {len(result['cited_keys'])} |",
        f"| Defined keys | {len(result['defined_keys'])} |",
        f"| Cited but undefined (error) | {len(result['cited_but_undefined'])} |",
        f"| Defined but uncited (warning) | {len(result['defined_but_uncited'])} |",
        "",
        f"**Status: {'CONSISTENT' if result['consistent'] else 'INCONSISTENT'}**",
        "",
    ]
    if result["nocite_all"]:
        lines += ["`\\nocite{*}` is present, so every defined entry counts as cited.", ""]
    if result["cited_but_undefined"]:
        lines += ["#### Cited but undefined", ""]
        lines += [f"- {_code(k)}" for k in result["cited_but_undefined"]]
        lines.append("")
    if result["defined_but_uncited"]:
        lines += ["#### Defined but uncited", ""]
        lines += [f"- {_code(k)}" for k in result["defined_but_uncited"]]
        lines.append("")
    return lines


def _origin(entry: dict[str, Any]) -> str:
    if entry["origin"] == "DB":
        return f"DB ({entry['metadata_date']})" if entry["metadata_date"] else "DB"
    return entry["origin"] or "—"


def _compared_with_md(entry: dict[str, Any]) -> str:
    if entry["origin"] == "DB":
        return _cell(entry["compared_with"])
    parts = []
    for lookup in entry["lookups"]:
        if lookup["status"] != "FOUND" or lookup["source"] not in {c["source"] for c in entry["checks"]}:
            continue
        label = f"{lookup['source']} ({lookup['matched_by']})" if lookup["matched_by"] else lookup["source"]
        parts.append(_link(label, lookup["public_url"]))
    return ", ".join(parts) if parts else "—"


def _entry_note(entry: dict[str, Any]) -> str:
    mismatches = [c for c in entry["checks"] if not c["match"]]
    if mismatches:
        return "differs: " + ", ".join(f"{c['field']} [{c['source']}]" for c in mismatches)
    if entry["status"] == EntryStatus.MATCH.value:
        return "compared: " + ", ".join(dict.fromkeys(c["field"] for c in entry["checks"]))
    return entry["reason"] or ""


def _references_md(data: dict[str, Any]) -> list[str]:
    references = data["references"]
    lines = ["### Metadata verification", ""]
    lines.append(f"**Sources:** {', '.join(references['sources']) or 'none'} "
                 "(stops at the first source with comparable fields)")
    lines.append("")
    lines += ["| Status | Count | Meaning |", "|---|---|---|"]
    for status in EntryStatus:
        lines.append(f"| {status.value} | {references['summary'][status.value]} | {STATUS_MEANING[status]} |")
    lines.append("")
    entries = references["entries"]
    lines += ["#### Entries", ""]
    if not entries:
        return lines + ["No bibliography entries.", ""]
    lines += ["| # | Key | DOI | Status | Origin | Compared with | Note |", "|---|---|---|---|---|---|---|"]
    for i, entry in enumerate(entries, 1):
        lines.append(
            f"| {i} | {_cell(entry['key'])} | {_cell(entry['doi'])} | **{entry['status']}** | "
            f"{_cell(_origin(entry))} | {_compared_with_md(entry)} | {_cell(_entry_note(entry))} |"
        )
    lines.append("")
    differing = [e for e in entries if any(not c["match"] for c in e["checks"])]
    if differing:
        lines += ["#### Differences", ""]
        for entry in differing:
            lines += [f"**{_md(entry['key'])}** ({entry['status']})", ""]
            for c in entry["checks"]:
                if not c["match"]:
                    lines.append(f"- {c['field']} [{c['source']}, {c['severity']}]: "
                                 f"bib {_code(c['bib_value'])} vs source {_code(c['source_value'])}")
            lines.append("")
    unresolved = [e for e in entries if e["status"] in (EntryStatus.SOURCE_ERROR.value,
                                                         EntryStatus.NOT_FOUND_IN_ENABLED_SOURCES.value)]
    if unresolved:
        lines += ["#### Lookup details", ""]
        for entry in unresolved:
            outcomes = " · ".join(
                f"{r['source']}: {r['status']}" + (f" ({_md(r['reason'])})" if r["reason"] else "")
                for r in entry["lookups"] if r["status"] != "SKIPPED"
            )
            lines.append(f"- **{_md(entry['key'])}**: {outcomes}")
        lines.append("")
    return lines


def _claims_md(data: dict[str, Any]) -> list[str]:
    rows = data["claims"]
    lines = ["## Claims", ""]
    if not rows:
        return lines + ["No citing sentences.", ""]
    judged = any(row["verdict"] for row in rows)
    if judged:
        sections = [(v, [r for r in rows if r["verdict"] == v]) for v in VERDICT_ORDER]
    else:
        sections = [("Claims without verdict", rows)]
    for heading, items in sections:
        if not items:
            continue
        lines += [f"### {heading}", ""]
        hint = VERDICT_HINTS.get(heading)
        if hint:
            lines += [f"_{hint[0].upper() + hint[1:]}._", ""]
        for row in items:
            lines += [f"#### {row['index']}. {_md(row['cite_key'])}", "", f"> {_md(row['sentence'])}", ""]
            if row["title"]:
                lines.append(f"- Title: {_md(row['title'])}")
            if row["verdict"]:
                lines.append(f"- Verdict: **{row['verdict']}**")
            lookup = row["lookup_status"] + (f" ({_md(row['lookup_reason'])})" if row["lookup_reason"] else "")
            lines.append(f"- Lookup: {lookup}")
            lines.append(f"- Evidence: {row['evidence_type']}")
            if row["evidence_source"]:
                lines.append(f"- Evidence source: {_md(row['evidence_source'])} "
                             f"(origin {row['evidence_origin']}, matched by {row['evidence_match']})")
            if row["public_url"]:
                lines.append(f"- Link: {_bare_link(row['public_url'])}")
            if row["evidence_quote"]:
                lines.append(f"- Evidence quote: \"{_md(row['evidence_quote'])}\"")
            if row["rationale"]:
                lines.append(f"- Rationale: {_md(row['rationale'])}")
            lines.append("")
    return lines


def render_markdown(data: dict[str, Any]) -> str:
    lines = _header_md(data) + _summary_md(data)
    if data["consistency"] is not None or data["references"] is not None:
        lines += ["## References", ""]
        if data["consistency"] is not None:
            lines += _consistency_md(data)
        if data["references"] is not None:
            lines += _references_md(data)
    if data["claims_run"]:
        lines += _claims_md(data)
    return "\n".join(lines).rstrip("\n") + "\n"


# ---------------------------------------------------------------------------
# Excel
# ---------------------------------------------------------------------------


def _xlsx_value(value: Any) -> Any:
    if isinstance(value, bool) or value is None:
        return "" if value is None else str(value)
    if isinstance(value, int):
        return value
    if isinstance(value, list):
        return ", ".join(_plain(v) for v in value)
    return _plain(value)


def write_xlsx(data: dict[str, Any], path: Path) -> None:
    """Write the workbook: sheets Summary, References and Claims."""
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill

    book = Workbook()
    summary = book.worksheets[0]
    summary.title = "Summary"
    for key in ("tool", "version", "generated", "input", "bibliography", "parts", "mode", "provider", "model",
                "metadata_sources", "abstract_sources", "sources_key_missing", "model_requests",
                "input_tokens", "output_tokens", "cached_tokens"):
        if key == "sources_key_missing":
            summary.append([key, _key_missing(data[key])])
        elif key in data:
            summary.append([key, _xlsx_value(data[key])])
    summary.append(["note", POSITIONING])
    for note in data["notes"]:
        summary.append(["notes", _plain(note)])
    for item in data["skipped"]:
        summary.append(["skipped", f"{item['part']} ({_plain(item['reason'])})"])
    summary.append([])
    if data["consistency"] is not None:
        summary.append(["cited_but_undefined", _xlsx_value(data["consistency"]["cited_but_undefined"])])
        summary.append(["defined_but_uncited", _xlsx_value(data["consistency"]["defined_but_uncited"])])
    if data["references"] is not None:
        for status, count in data["references"]["summary"].items():
            summary.append([f"references {status}", count])
    if data["claims_run"]:
        summary.append(["claims", len(data["claims"])])
        for verdict, count in data["claims_summary"]["verdicts"].items():
            summary.append([f"verdict {verdict}", count])
        for status, count in data["claims_summary"]["lookup_status"].items():
            summary.append([f"lookup {status}", count])
    summary.column_dimensions["A"].width = 24
    summary.column_dimensions["B"].width = 80

    def table(title: str, columns: tuple[str, ...], rows: list[dict[str, Any]], widths: dict[str, int]):
        sheet = book.create_sheet(title)
        for col, name in enumerate(columns, 1):
            cell = sheet.cell(row=1, column=col, value=name)
            cell.font = Font(bold=True)
            sheet.column_dimensions[cell.column_letter].width = widths.get(name, 16)
        for number, row in enumerate(rows, 2):
            for col, name in enumerate(columns, 1):
                cell = sheet.cell(row=number, column=col, value=_xlsx_value(row.get(name)))
                cell.alignment = Alignment(wrap_text=name in widths, vertical="top")
        sheet.freeze_panes = "A2"
        return sheet

    table("References", REFERENCE_COLUMNS, (data["references"] or {}).get("entries") or [],
          {"reason": 40, "compared_with": 30, "differences": 60})
    claims = table("Claims", CLAIM_COLUMNS, data["claims"],
                   {"sentence": 70, "title": 45, "evidence_quote": 60, "rationale": 60, "lookup_reason": 40})
    fills = {"SUPPORTED": "C6EFCE", "PLAUSIBLE": "FFEB9C", "UNVERIFIABLE": "E7E6E6", "SUSPICIOUS": "FFC7CE"}
    verdict_col = CLAIM_COLUMNS.index("verdict") + 1
    for number, row in enumerate(data["claims"], 2):
        fill = fills.get(row["verdict"] or "")
        if fill:
            claims.cell(row=number, column=verdict_col).fill = PatternFill("solid", fgColor=fill)
    for sheet in book.worksheets:  # text stays text, never a formula
        for cells in sheet.iter_rows():
            for cell in cells:
                if isinstance(cell.value, str):
                    cell.data_type = "s"
    book.save(path)


def write_report(data: dict[str, Any], output: Path | None) -> None:
    """Markdown on standard output, or ``output`` (``.md`` or ``.xlsx``), replacing an existing file."""
    if output is None:
        sys.stdout.write(render_markdown(data))
        sys.stdout.flush()
        return
    if output.suffix.lower() == ".xlsx":
        write_xlsx(data, output)
    else:
        output.write_text(render_markdown(data), encoding="utf-8")
