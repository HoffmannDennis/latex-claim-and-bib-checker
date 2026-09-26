"""Model providers: local mode, cloud mode, secrets and the judging rules."""

from __future__ import annotations

import socket

import pytest
import requests

from latex_claim_and_bib_checker import _cli, _llm
from latex_claim_and_bib_checker._llm import check_local_url
from latex_claim_and_bib_checker._prompt import SYSTEM_PROMPT, VERDICT_SCHEMA
from conftest import (
    ABSTRACT_ALPHA,
    ABSTRACT_BETA,
    chat_completion,
    claim_rows,
    crossref_payload,
    make_project,
    make_response,
    models_list,
    verdict,
)

LOCAL = "http://127.0.0.1:1234/v1"
QUOTE_ALPHA = "the widget index predicts next-quarter returns"
CLAIMS = "--claim-check-only"


def route_sources(http, alpha: str | None = ABSTRACT_ALPHA, beta: str | None = ABSTRACT_BETA) -> None:
    http.on("GET", "api.crossref.org/works/10.1234/alpha", make_response(200, crossref_payload("10.1234/alpha", alpha)))
    http.on("GET", "api.crossref.org/works/10.1234/beta", make_response(200, crossref_payload("10.1234/beta", beta)))


def cited_key(call) -> str:
    """Citation key of the source block in a recorded chat request."""
    first_user = next(m for m in call.body["messages"] if m["role"] == "user")
    return first_user["content"].split("Citation key: ", 1)[1].split("\n", 1)[0]


@pytest.fixture
def project(tmp_path):
    return make_project(tmp_path / "doc")


def supported_for(call):
    if cited_key(call) == "alpha":
        return chat_completion(verdict("SUPPORTED", QUOTE_ALPHA))
    return chat_completion(verdict("PLAUSIBLE", "", "On topic."))


# -- local mode and prompt layout ------------------------------------------------


def test_local_run_groups_requests_with_a_byte_identical_prefix(project, http, run):
    route_sources(http)
    http.on("GET", f"{LOCAL}/models", models_list("local-model"))
    http.on("POST", f"{LOCAL}/chat/completions", supported_for)
    result = run(project, CLAIMS, sources=["crossref"])
    assert result.code == 0, result.err
    posts = http.posts()
    assert [cited_key(c) for c in posts] == ["alpha", "alpha", "beta"]  # grouped by cited work
    assert posts[0].body["messages"][0]["content"] == SYSTEM_PROMPT
    raw_a, raw_b = posts[0].raw, posts[1].raw
    cut = raw_a.index(b"CLAIM TO CHECK")
    assert raw_a[:cut] == raw_b[:cut] and raw_a != raw_b
    assert "Authorization" not in posts[0].headers
    rows = claim_rows(result.data)
    assert rows[1]["verdict"] == "SUPPORTED" and rows[1]["evidence_quote"] == QUOTE_ALPHA


def test_local_mode_never_reads_cloud_keys(project, http, run, monkeypatch):
    route_sources(http)
    http.on("GET", f"{LOCAL}/models", models_list("m"))
    http.on("POST", f"{LOCAL}/chat/completions", supported_for)

    class Watch(dict):
        def get(self, key, default=None):
            assert key not in ("OPENAI_API_KEY", "GEMINI_API_KEY", "ANTHROPIC_API_KEY"), key
            return super().get(key, default)

        def __getitem__(self, key):
            assert key not in ("OPENAI_API_KEY", "GEMINI_API_KEY", "ANTHROPIC_API_KEY"), key
            return super().__getitem__(key)

    watched = Watch({"OPENAI_API_KEY": "dummy-openai-value-never-read"})
    monkeypatch.setattr(_cli.os, "environ", watched)
    monkeypatch.setattr(_llm.os, "environ", watched)
    assert run(project, sources=["crossref"]).code == 0


def test_unreachable_local_server_with_cloud_key_aborts_without_cloud_call(project, http, run, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "dummy-openai-value-000000")
    route_sources(http)
    http.on("GET", f"{LOCAL}/models", requests.ConnectionError("connection refused"))
    result = run(project, sources=["crossref"])
    assert result.code == 2
    assert "--bib-check-only" in result.err and "--cloud" in result.err
    assert http.posts() == []
    assert [c.url for c in http.calls] == [f"{LOCAL}/models"]  # before any source or cloud request


def test_several_local_models_need_model(project, http, run):
    route_sources(http)
    http.on("GET", f"{LOCAL}/models", models_list("model-a", "model-b"))
    result = run(project, CLAIMS, sources=["crossref"])
    assert result.code == 2 and "several models are available" in result.err
    assert http.posts() == []


def test_public_local_llm_url_is_refused_and_private_ones_are_accepted(project, http, run, monkeypatch):
    table = {"api.openai.com": "203.0.113.10", "llm-box.lan": "192.168.1.20"}

    def getaddrinfo(host, *args, **kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (table[host], 0))]

    monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)
    for url in ("https://api.openai.com/v1", "http://8.8.8.8:1234/v1"):
        monkeypatch.setenv("LOCAL_LLM_URL", url)
        result = run(project)
        assert result.code == 2 and "--cloud" in result.err
    assert http.calls == []
    for url in ("http://127.0.0.1:1234/v1", "http://[::1]:8080/v1", "http://10.0.0.5:1234/v1",
                "http://llm-box.lan:1234/v1"):
        assert check_local_url(url) == url


# -- judging -----------------------------------------------------------------------------


def test_quote_not_in_the_abstract_is_downgraded(project, http, run):
    route_sources(http)
    http.on("GET", f"{LOCAL}/models", models_list("m"))
    http.on("POST", f"{LOCAL}/chat/completions", lambda call: chat_completion(
        verdict("SUPPORTED", "widgets always outperform gadgets in every market")))
    rows = claim_rows(run(project, CLAIMS, sources=["crossref"]).data)
    assert rows[1]["verdict"] == "UNVERIFIABLE" and rows[1]["evidence_quote"] == ""
    assert "Downgraded from SUPPORTED" in rows[1]["rationale"]


def test_no_model_call_without_abstract(project, http, run):
    route_sources(http, beta=None)
    http.on("GET", f"{LOCAL}/models", models_list("m"))
    http.on("POST", f"{LOCAL}/chat/completions", supported_for)
    result = run(project, CLAIMS, sources=["crossref"])
    assert [cited_key(c) for c in http.posts()] == ["alpha", "alpha"]
    assert claim_rows(result.data)[2]["verdict"] == "UNVERIFIABLE"


def test_failed_model_requests_give_exit_4_and_a_header_note(project, http, run):
    route_sources(http)
    http.on("GET", f"{LOCAL}/models", models_list("m"))
    http.on("POST", f"{LOCAL}/chat/completions", make_response(429, {"error": {"message": "quota"}}))
    result = run(project, CLAIMS, sources=["crossref"])
    assert result.code == 4
    assert len(http.posts()) == 3  # every claim is still tried
    assert all(r["verdict"] == "UNVERIFIABLE" for r in claim_rows(result.data).values())
    assert "- Note: The model provider failed during the run: model requests failed for 3 claims, " \
           "which are marked UNVERIFIABLE (not judged)" in result.out


def test_local_redirect_is_not_followed_and_proxies_are_ignored(project, http, run):
    route_sources(http)
    http.on("GET", f"{LOCAL}/models", models_list("m"))
    http.on("POST", f"{LOCAL}/chat/completions",
            make_response(307, headers={"Location": "https://public.example.invalid/collect"}))
    result = run(project, CLAIMS, sources=["crossref"])
    assert result.code == 4 and "redirect, which is not followed" in result.err
    assert [c.url for c in http.posts()] == [f"{LOCAL}/chat/completions"]
    assert http.trust_env is False


def test_invalid_json_gets_one_repair_then_unverifiable(project, http, run):
    route_sources(http)
    http.on("GET", f"{LOCAL}/models", models_list("m"))
    http.on("POST", f"{LOCAL}/chat/completions", chat_completion("this is not json"))
    result = run(project, CLAIMS, sources=["crossref"])
    assert len(http.posts()) == 6  # three claims, one repair each
    assert all(r["verdict"] == "UNVERIFIABLE" and r["rationale"] == "model output invalid"
               for r in claim_rows(result.data).values())


# -- cloud mode --------------------------------------------------------------------


def test_cloud_sends_without_asking_and_keeps_the_key_out_of_the_report(project, http, run, monkeypatch, tmp_path):
    key = "dummy-gemini-value-000000"
    monkeypatch.setenv("GEMINI_API_KEY", key)
    route_sources(http)
    http.on("POST", "generativelanguage.googleapis.com/v1beta/openai/chat/completions", supported_for)
    out = tmp_path / "r.md"
    result = run(project, CLAIMS, "--cloud", "gemini", "--model", "gemini-test", "-o", out, sources=["crossref"])
    assert result.code == 0, result.err
    assert "Send?" not in result.err and "Widget indices predict returns" not in result.err
    posts = http.posts()
    assert len(posts) == 3 and posts[0].headers["Authorization"] == f"Bearer {key}"
    assert key not in posts[0].url and key not in out.read_text() + result.err


def test_cloud_without_model_or_key_stops_and_reads_only_the_chosen_key(project, http, run, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "dummy-openai-value-111111")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "dummy-anthropic-value-222222")
    assert run(project, "--cloud", "openai").code == 2
    result = run(project, "--cloud", "gemini", "--model", "x")
    assert result.code == 2 and "GEMINI_API_KEY is not set" in result.err
    assert "value-111111" not in result.err and "value-222222" not in result.err
    assert http.calls == []


def test_anthropic_native_adapter_uses_forced_tool(project, http, run, monkeypatch, tmp_path):
    key = "dummy-anthropic-value-333333"
    monkeypatch.setenv("ANTHROPIC_API_KEY", key)
    route_sources(http)
    http.on("POST", "api.anthropic.com/v1/messages", make_response(200, {
        "content": [{"type": "tool_use", "id": "tu_1", "name": "record_verdict",
                     "input": verdict("SUPPORTED", QUOTE_ALPHA)}]}))
    out = tmp_path / "r.md"
    result = run(project, CLAIMS, "--cloud", "anthropic", "--model", "claude-test-1", "-o", out,
                 sources=["crossref"])
    assert result.code == 0, result.err
    call = http.posts()[0]
    assert call.body["tool_choice"] == {"type": "tool", "name": "record_verdict"}
    assert call.body["tools"][0]["input_schema"] == VERDICT_SCHEMA and call.body["system"] == SYSTEM_PROMPT
    assert call.headers["x-api-key"] == key
    assert claim_rows(result.data)[1]["verdict"] == "SUPPORTED"
    assert key not in out.read_text() + result.err


def test_provider_failure_gives_exit_4_without_switch_and_redacts_the_key(project, http, run, monkeypatch, tmp_path):
    key = "dummy-openai-value-444444"
    monkeypatch.setenv("OPENAI_API_KEY", key)
    monkeypatch.setenv("GEMINI_API_KEY", "dummy-gemini-value-555555")
    route_sources(http)
    http.on("POST", "api.openai.com/v1/chat/completions",
            make_response(401, {"error": {"message": f"invalid key {key}"}}))
    out = tmp_path / "r.md"
    result = run(project, CLAIMS, "--cloud", "openai", "--model", "g-1", "-o", out, sources=["crossref"])
    assert result.code == 4
    assert len(http.posts()) == 1 and "api.openai.com" in http.posts()[0].url  # no other provider
    assert key not in result.err and key not in out.read_text()


def test_provider_error_text_is_redacted_before_it_is_cut(project, http, run, monkeypatch, tmp_path):
    key = "dummy-openai-" + "k" * 40
    monkeypatch.setenv("OPENAI_API_KEY", key)
    route_sources(http)
    http.on("POST", "api.openai.com/v1/chat/completions",
            make_response(404, {"error": {"message": "x" * 190 + key}}))  # the key spans character 200
    out = tmp_path / "r.md"
    result = run(project, CLAIMS, "--cloud", "openai", "--model", "g-1", "-o", out, sources=["crossref"])
    assert result.code == 4
    text = result.err + out.read_text()
    assert not any(key[i:i + 8] in text for i in range(len(key) - 7))
