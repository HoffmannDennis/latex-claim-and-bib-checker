"""Runs the command line in a separate process with a fake HTTP layer.

Used by the usage-trace test: the process gets its own HOME, TMPDIR, XDG
directories and working directory, all watched by the test. Real sockets
are blocked. Usage: ``python trace_runner.py <cli arguments>``.
"""

from __future__ import annotations

import socket
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import conftest  # noqa: E402
from latex_claim_and_bib_checker import _cli, _http  # noqa: E402


def _blocked(*args, **kwargs):
    raise AssertionError("no network in the trace runner")


def main() -> int:
    socket.socket.connect = _blocked  # type: ignore[method-assign]
    socket.create_connection = _blocked  # type: ignore[assignment]
    fake = conftest.FakeHttp()
    fake.on("GET", "api.crossref.org/works/10.1234/alpha",
            conftest.make_response(200, conftest.crossref_payload("10.1234/alpha", conftest.ABSTRACT_ALPHA,
                                                                  title="Widget Markets")))
    fake.on("GET", "api.crossref.org/works/10.1234/beta",
            conftest.make_response(200, conftest.crossref_payload("10.1234/beta", conftest.ABSTRACT_BETA,
                                                                  title="Gadget Standards")))
    fake.on("GET", "127.0.0.1:1234/v1/models", conftest.models_list("local-model"))
    fake.on("POST", "127.0.0.1:1234/v1/chat/completions",
            conftest.chat_completion(conftest.verdict("PLAUSIBLE")))
    _http.new_session = lambda: fake  # type: ignore[assignment]
    return _cli.main(sys.argv[1:], sources=["crossref"])


if __name__ == "__main__":
    raise SystemExit(main())
