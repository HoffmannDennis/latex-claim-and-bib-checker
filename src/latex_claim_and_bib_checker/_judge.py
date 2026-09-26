"""Judging claims with one model provider, and the quote check.

Claims are grouped by cited key (in order of first citation) and judged one
after another, so that consecutive requests share the longest possible
identical prefix. Claims whose cited work has no usable abstract are never
sent; they are ``UNVERIFIABLE`` with ``evidence_type`` ``none``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from ._claims import Claim
from ._llm import ChatClient, ProviderFailure, _Transient
from ._lookup import Evidence
from ._prompt import SYSTEM_PROMPT, claim_block, source_block, user_message
from ._text import quote_in_abstract

@dataclass
class ClaimResult:
    claim: Claim
    title: str
    evidence: Evidence
    verdict: str | None = None  # None when no model was asked
    evidence_quote: str = ""
    rationale: str = ""

    @property
    def evidence_type(self) -> str:
        return "abstract" if self.evidence.abstract else "none"


def grouped_order(claims: list[Claim]) -> list[Claim]:
    """Claims grouped by cited key, groups in order of first citation."""
    groups: dict[str, list[Claim]] = {}
    for claim in claims:
        groups.setdefault(claim.cite_key, []).append(claim)
    return [claim for group in groups.values() for claim in group]


def apply_quote_check(verdict: dict[str, str], abstract: str) -> tuple[str, str, str]:
    """Check the model's quote against the abstract.

    Returns ``(verdict, evidence_quote, rationale)``. ``SUPPORTED`` without a
    quote that occurs in the abstract becomes ``UNVERIFIABLE``. For other
    verdicts an invalid quote is removed and the verdict is kept.
    """
    label = verdict["verdict"]
    quote = verdict["evidence_quote"].strip()
    rationale = verdict["rationale"].strip()
    found = bool(quote) and quote_in_abstract(quote, abstract)
    if label == "SUPPORTED":
        if found:
            return label, quote, rationale
        why = "no evidence quote was given" if not quote else "the evidence quote was not found in the abstract"
        note = f"Downgraded from SUPPORTED because {why}."
        return "UNVERIFIABLE", "", f"{note} Model rationale: {rationale}" if rationale else note
    if quote and not found:
        note = "The quoted passage was not found in the abstract and was removed."
        return label, "", f"{rationale} ({note})" if rationale else note
    return label, quote if found else "", rationale


def judge(results: list[ClaimResult], client: ChatClient,
          progress: Callable[[str], None] | None = None) -> str | None:
    """Fill in verdicts in place. Returns an error message if the provider failed.

    The provider counts as failed when it stops working, and also when a
    single request fails (for example with HTTP 429, a server error or a
    timeout). In that case the other claims are still judged.
    """
    by_claim = {id(r.claim): r for r in results}
    ordered = [by_claim[id(c)] for c in grouped_order([r.claim for r in results])]
    to_send = [r for r in ordered if r.evidence.abstract]
    for r in ordered:
        if not r.evidence.abstract:
            r.verdict = "UNVERIFIABLE"
            r.rationale = "No usable abstract was found for the cited work, so no model was asked."

    failure: str | None = None
    failed = 0
    for number, result in enumerate(to_send, 1):
        if failure is not None:
            result.verdict = "UNVERIFIABLE"
            result.rationale = f"Not judged: {failure}"
            continue
        if progress:
            progress(f"Judging claim {number}/{len(to_send)} ({result.claim.cite_key})")
        abstract = result.evidence.abstract or ""
        message = user_message(
            source_block(result.claim.cite_key, result.title, abstract),
            claim_block(result.claim.sentence, result.claim.context_before),
        )
        try:
            reply = client.judge(SYSTEM_PROMPT, message)
        except ProviderFailure as exc:
            failure = str(exc)
            result.verdict = "UNVERIFIABLE"
            result.rationale = f"Not judged: {failure}"
            continue
        except _Transient as exc:
            failed += 1
            result.verdict = "UNVERIFIABLE"
            result.rationale = f"Not judged: {exc}"
            continue
        if reply.verdict is None:
            result.verdict = "UNVERIFIABLE"
            result.rationale = "model output invalid"
            continue
        result.verdict, result.evidence_quote, result.rationale = apply_quote_check(reply.verdict, abstract)
    if failure is None and failed:
        failure = (f"model requests failed for {failed} claims, which are marked UNVERIFIABLE (not judged)"
                   if failed > 1 else "the model request failed for 1 claim, which is marked UNVERIFIABLE (not judged)")
    return failure
