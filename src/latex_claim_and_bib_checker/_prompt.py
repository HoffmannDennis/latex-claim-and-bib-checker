"""Prompt layout and the verdict schema.

Every request is built from three parts, always in this order:

1. the fixed system part (instructions, verdict definitions, JSON schema);
2. the source block (citation key, title, cleaned abstract);
3. the claim (citing sentence plus the sentence before it).

Parts 1 and 2 are byte-identical for all claims that cite the same work, so
a server that reuses a cached prompt prefix can skip re-processing them.
"""

from __future__ import annotations

import json
from typing import Any

VERDICTS = ("SUPPORTED", "PLAUSIBLE", "SUSPICIOUS", "UNVERIFIABLE")

VERDICT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "verdict": {"type": "string", "enum": list(VERDICTS)},
        "evidence_quote": {"type": "string"},
        "rationale": {"type": "string"},
    },
    "required": ["verdict", "evidence_quote", "rationale"],
    "additionalProperties": False,
}

SCHEMA_NAME = "citation_verdict"

SYSTEM_PROMPT = (
    "You check whether a cited scientific work supports the sentence that cites it. "
    "You receive the abstract of the cited work and the citing sentence. Judge only on "
    "the basis of the abstract; do not use outside knowledge about the work.\n"
    "\n"
    "Verdicts:\n"
    "- SUPPORTED: the abstract clearly states what the sentence attributes to the cited work.\n"
    "- PLAUSIBLE: the abstract is on topic and consistent with the sentence, but does not "
    "state the specific point.\n"
    "- SUSPICIOUS: the abstract contradicts the sentence, or suggests that the work does not "
    "support what the sentence attributes to it.\n"
    "- UNVERIFIABLE: the abstract does not contain enough information to decide.\n"
    "\n"
    "Rules:\n"
    "- evidence_quote must be one contiguous passage copied exactly from the abstract, "
    "without ellipses, omissions or changes. It is required for SUPPORTED. Use an empty "
    "string if there is no suitable passage.\n"
    "- rationale explains the verdict in at most 80 words.\n"
    "- Reply with a single JSON object that matches this JSON schema and nothing else:\n"
    + json.dumps(VERDICT_SCHEMA, sort_keys=True)
)

REPAIR_PROMPT = (
    "Your previous reply was not a valid JSON object matching the schema. Reply again with "
    "only the JSON object: keys verdict, evidence_quote, rationale."
)


def source_block(cite_key: str, title: str, abstract: str) -> str:
    """Part 2: the cited work. Identical for every claim citing ``cite_key``."""
    return (
        "CITED WORK\n"
        f"Citation key: {cite_key}\n"
        f"Title: {title or '(no title)'}\n"
        "Abstract:\n"
        f"{abstract}\n"
        "\n"
    )


def claim_block(sentence: str, context_before: str = "") -> str:
    """Part 3: the claim to judge."""
    text = "CLAIM TO CHECK\n"
    if context_before:
        text += f"Preceding sentence (context only): {context_before}\n"
    text += f"Citing sentence: {sentence}\n"
    return text


def user_message(block: str, claim: str) -> str:
    return block + claim


def validate_verdict(data: Any) -> dict[str, str] | None:
    """Return the verdict object if it matches :data:`VERDICT_SCHEMA`, else ``None``."""
    if not isinstance(data, dict):
        return None
    if set(data) != {"verdict", "evidence_quote", "rationale"}:
        return None
    verdict, quote, rationale = data["verdict"], data["evidence_quote"], data["rationale"]
    if not isinstance(verdict, str) or verdict not in VERDICTS:
        return None
    if not isinstance(quote, str) or not isinstance(rationale, str):
        return None
    return {"verdict": verdict, "evidence_quote": quote, "rationale": rationale}


def parse_verdict_text(text: Any) -> dict[str, str] | None:
    """Parse a model reply (a JSON string, optionally inside a code fence)."""
    if not isinstance(text, str):
        return None
    body = text.strip()
    if body.startswith("```"):
        body = body.split("\n", 1)[1] if "\n" in body else ""
        if body.rstrip().endswith("```"):
            body = body.rstrip()[:-3]
    try:
        return validate_verdict(json.loads(body))
    except ValueError:
        return None
