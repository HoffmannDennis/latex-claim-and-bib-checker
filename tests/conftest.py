"""Shared fixtures: fake HTTP layers, canned API payloads and a guard against real network use."""

from __future__ import annotations

import io
import json
import socket
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import pytest
import requests
from requests.structures import CaseInsensitiveDict

from latex_claim_and_bib_checker import _http, _plugins

ENV_VARS = (
    "OPENAI_API_KEY", "GEMINI_API_KEY", "ANTHROPIC_API_KEY", "SPRINGER_API_KEY", "S2_API_KEY",
    "CORE_API_KEY", "OPENALEX_API_KEY", "CONTACT_EMAIL", "LOCAL_LLM_URL",
)

EXAMPLES = Path(__file__).resolve().parent.parent / "examples"

# Synthetic abstracts (not taken from any publication).
ABSTRACT_ALPHA = (
    "We study synthetic widget markets and show that the widget index predicts next-quarter "
    "returns across twelve simulated exchanges. The effect is robust to transaction costs."
)
ABSTRACT_BETA = (
    "This note describes a simulated survey of gadget producers. Producers who adopted the new "
    "gadget standard reported lower inventory costs than producers who did not."
)
ABSTRACT_GAMMA = (
    "A synthetic laboratory experiment with sprockets finds no relation between sprocket colour "
    "and assembly speed in any of the three treatment groups."
)


# ---------------------------------------------------------------------------
# Network guard and environment
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _no_real_network(monkeypatch):
    def _blocked(*args, **kwargs):
        raise AssertionError("tests must not open network connections")

    monkeypatch.setattr(socket.socket, "connect", _blocked)
    monkeypatch.setattr(socket, "create_connection", _blocked)
    monkeypatch.setattr(socket, "getaddrinfo", _blocked)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for name in ENV_VARS:
        monkeypatch.delenv(name, raising=False)


@pytest.fixture(autouse=True)
def _no_installed_plugins(monkeypatch):
    """Only the built-in sources, even if a source plugin is installed in this environment."""
    monkeypatch.setattr(_plugins, "entry_points", lambda *, group: [])


@pytest.fixture(autouse=True)
def sleeps(monkeypatch):
    """Record instead of performing throttling pauses (the fixture form of a zero delay)."""
    recorded: list[float] = []
    monkeypatch.setattr(time, "sleep", lambda seconds: recorded.append(seconds))
    return recorded


@pytest.fixture(autouse=True)
def _non_interactive(monkeypatch):
    monkeypatch.setattr("sys.stdin", io.StringIO(""))


# ---------------------------------------------------------------------------
# Fake HTTP
# ---------------------------------------------------------------------------


def make_response(status: int = 200, payload: Any = None, *, text: str | None = None,
                  headers: dict[str, str] | None = None, content_type: str = "application/json",
                  url: str = "https://api.example.org/") -> requests.Response:
    """Build a real ``requests.Response`` without any network traffic."""
    resp = requests.Response()
    resp.status_code = status
    resp.url = url
    resp.headers = CaseInsensitiveDict({"Content-Type": content_type, **(headers or {})})
    if text is not None:
        resp._content = text.encode("utf-8")
    elif payload is not None:
        resp._content = json.dumps(payload).encode("utf-8")
    else:
        resp._content = b""
    resp.encoding = "utf-8"
    return resp


@dataclass
class Call:
    method: str
    url: str
    params: dict[str, Any]
    headers: dict[str, Any]
    body: Any = None
    raw: bytes | None = None


Responder = Any  # Response | Exception | Callable[[Call], Response]


@dataclass
class FakeHttp:
    """Stand-in for ``requests.Session``: routes by method and URL fragment, records every call."""

    routes: list[tuple[str, str, Responder]] = field(default_factory=list)
    calls: list[Call] = field(default_factory=list)

    def on(self, method: str, fragment: str, responder: Responder) -> "FakeHttp":
        self.routes.insert(0, (method, fragment, responder))  # later rules win
        return self

    def _dispatch(self, call: Call) -> requests.Response:
        self.calls.append(call)
        for method, fragment, responder in self.routes:
            if method == call.method and fragment in call.url:
                if isinstance(responder, BaseException):
                    raise responder
                if callable(responder) and not isinstance(responder, requests.Response):
                    return responder(call)
                return responder
        return make_response(404)

    def get(self, url, params=None, headers=None, timeout=None, **kwargs):
        return self._dispatch(Call("GET", url, dict(params or {}), dict(headers or {})))

    def post(self, url, data=None, json=None, headers=None, timeout=None, **kwargs):  # noqa: A002
        raw = data if isinstance(data, bytes) else (data.encode("utf-8") if isinstance(data, str) else None)
        body = json
        if body is None and raw is not None:
            try:
                body = __import__("json").loads(raw.decode("utf-8"))
            except ValueError:
                body = None
        return self._dispatch(Call("POST", url, {}, dict(headers or {}), body, raw))

    def to(self, fragment: str, method: str | None = None) -> list[Call]:
        return [c for c in self.calls if fragment in c.url and (method is None or c.method == method)]

    def posts(self) -> list[Call]:
        return [c for c in self.calls if c.method == "POST"]


@pytest.fixture
def http(monkeypatch) -> FakeHttp:
    fake = FakeHttp()
    monkeypatch.setattr(_http, "new_session", lambda: fake)
    return fake


# ---------------------------------------------------------------------------
# Canned API payloads
# ---------------------------------------------------------------------------


# Record titles are left out unless a test passes one: a record title that
# disagrees with the bibliography title makes the lookup reject the abstract.


def crossref_payload(doi: str, abstract: str | None = None, *, title: str | None = None,
                     **extra: Any) -> dict[str, Any]:
    message: dict[str, Any] = {"DOI": doi, "title": [title] if title else []}
    if abstract is not None:
        message["abstract"] = f"<jats:p>{abstract}</jats:p>"
    message.update(extra)
    return {"status": "ok", "message": message}


def crossref_work(doi: str, **overrides: Any) -> dict[str, Any]:
    message = {
        "DOI": doi,
        "title": ["Attention Is All You Need"],
        "author": [
            {"family": "Vaswani", "given": "Ashish"},
            {"family": "Shazeer", "given": "Noam"},
            {"family": "Parmar", "given": "Niki"},
        ],
        "issued": {"date-parts": [[2017]]},
        "container-title": ["Advances in Neural Information Processing Systems"],
        "volume": "30",
        "page": "5998-6008",
    }
    message.update(overrides)
    return {"status": "ok", "message": message}


def inverted_index(text: str) -> dict[str, list[int]]:
    index: dict[str, list[int]] = {}
    for position, word in enumerate(text.split()):
        index.setdefault(word, []).append(position)
    return index


def openalex_payload(doi: str | None, abstract: str | None = None, title: str | None = None) -> dict[str, Any]:
    return {
        "id": "https://openalex.org/W0000000001",
        "doi": f"https://doi.org/{doi}" if doi else None,
        "title": title,
        "abstract_inverted_index": inverted_index(abstract) if abstract else None,
    }


def datacite_payload(doi: str, abstract: str | None = None, *, title: str | None = None) -> dict[str, Any]:
    descriptions = [{"description": abstract, "descriptionType": "Abstract"}] if abstract else []
    attributes: dict[str, Any] = {"doi": doi, "descriptions": descriptions}
    if title:
        attributes["titles"] = [{"title": title}]
    return {"data": {"id": doi, "attributes": attributes}}


def arxiv_feed(arxiv_id: str, summary: str, version: int = 1, *, title: str = "") -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<feed xmlns="http://www.w3.org/2005/Atom">'
        "<entry>"
        f"<id>http://arxiv.org/abs/{arxiv_id}v{version}</id>"
        f"<title>{title}</title>"
        f"<summary>  {summary}\n</summary>"
        "<published>2017-06-12T17:57:34Z</published>"
        "</entry></feed>"
    )


def springer_payload(doi: str, abstract: str | None, *, title: str | None = None) -> dict[str, Any]:
    return {"records": [{"doi": doi, "title": title, "abstract": abstract}]}


def s2_payload(doi: str | None, abstract: str | None, *, title: str | None = None) -> dict[str, Any]:
    ids: dict[str, Any] = {"DOI": doi} if doi else {}
    return {"paperId": "0" * 40, "title": title, "abstract": abstract, "externalIds": ids,
            "url": "https://www.semanticscholar.org/paper/0000"}


def core_payload(doi: str | None, abstract: str | None, *, title: str | None = None) -> dict[str, Any]:
    return {"totalHits": 1, "results": [{"id": 1, "doi": doi, "title": title, "abstract": abstract}]}


def europepmc_payload(doi: str, abstract: str | None, *, title: str | None = None) -> dict[str, Any]:
    return {"hitCount": 1, "resultList": {"result": [{"id": "1", "doi": doi, "title": title,
                                                      "abstractText": abstract}]}}


def chat_completion(content: Any) -> requests.Response:
    text = content if isinstance(content, str) else json.dumps(content)
    return make_response(200, {"choices": [{"index": 0, "message": {"role": "assistant", "content": text}}]})


def verdict(label: str, quote: str = "", rationale: str = "Synthetic rationale.") -> dict[str, str]:
    return {"verdict": label, "evidence_quote": quote, "rationale": rationale}


def models_list(*names: str) -> requests.Response:
    return make_response(200, {"object": "list", "data": [{"id": n, "object": "model"} for n in names]})


# ---------------------------------------------------------------------------
# Documents and CLI runs
# ---------------------------------------------------------------------------

BIB_BASIC = """
@article{alpha,
  author = {Doe, Jane},
  title  = {Widget Markets},
  year   = {2020},
  doi    = {10.1234/alpha}
}

@article{beta,
  author = {Roe, Richard},
  title  = {Gadget Standards},
  year   = {2021},
  doi    = {10.1234/beta}
}
"""

TEX_BASIC = r"""\documentclass{article}
\begin{document}
Widget indices predict returns in simulated markets \citep{alpha}.
Gadget standards lower inventory costs \citep{beta}.
The widget effect survives transaction costs \citep{alpha}.
\bibliography{refs}
\end{document}
"""


def make_project(root: Path, tex: str = TEX_BASIC, bibs: dict[str, str] | None = None,
                 name: str = "paper.tex") -> Path:
    root.mkdir(parents=True, exist_ok=True)
    for bib_name, text in (bibs if bibs is not None else {"refs.bib": BIB_BASIC}).items():
        (root / bib_name).parent.mkdir(parents=True, exist_ok=True)
        (root / bib_name).write_text(text, encoding="utf-8")
    path = root / name
    path.write_text(tex, encoding="utf-8")
    return path


@dataclass
class RunResult:
    code: int
    out: str
    err: str
    data: dict[str, Any] | None = None  # the report data that Markdown and Excel are rendered from

    @property
    def claims(self) -> dict[int, dict[str, Any]]:
        return claim_rows(self.data)

    @property
    def entries(self) -> dict[str, dict[str, Any]]:
        references = (self.data or {}).get("references") or {}
        return {e["key"]: e for e in references.get("entries") or []}


@pytest.fixture
def run(capsys, monkeypatch) -> Callable[..., RunResult]:
    """Run the command line in-process.

    Keyword arguments are the internal settings of ``main`` (source
    selection and evidence-only runs without a model).
    """
    from latex_claim_and_bib_checker import _cli, _report

    captured: dict[str, Any] = {}
    original = _report.write_report

    def spy(data: dict[str, Any], output: Any) -> None:
        captured["data"] = data
        original(data, output)

    monkeypatch.setattr(_report, "write_report", spy)

    def _run(*argv: Any, **settings: Any) -> RunResult:
        captured.clear()
        code = _cli.main([str(a) for a in argv], **settings)
        out = capsys.readouterr()
        return RunResult(code, out.out, out.err, captured.get("data"))

    return _run



def claim_rows(report: dict[str, Any] | None) -> dict[int, dict[str, Any]]:
    return {row["index"]: row for row in ((report or {}).get("claims") or [])}
