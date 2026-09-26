"""Model providers: exactly one per run, no fallback.

Two transports, both on plain ``requests``:

* an OpenAI-compatible chat-completions client (local servers such as
  LM Studio, Ollama, llama.cpp or vLLM, OpenAI, and Gemini's
  OpenAI-compatible endpoint) that asks for structured output with
  ``response_format`` / ``json_schema``,
* a native Anthropic Messages client that obtains structured output through
  a forced tool call, because Anthropic's OpenAI-compatible layer ignores
  JSON schemas.

In local mode no cloud key variable is read and proxy settings are ignored.
Redirects are never followed. Keys travel only in request headers and are
removed from every error message.
"""

from __future__ import annotations

import ipaddress
import json
import os
import socket
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

import requests

from ._http import USER_AGENT, redact
from ._prompt import REPAIR_PROMPT, SCHEMA_NAME, SYSTEM_PROMPT, VERDICT_SCHEMA, parse_verdict_text, validate_verdict

LOCAL_DEFAULT_BASE_URL = "http://127.0.0.1:1234/v1"
OPENAI_BASE_URL = "https://api.openai.com/v1"
GEMINI_BASE_URL = "https://generativelanguage.googleapis.com/v1beta/openai/"
ANTHROPIC_BASE_URL = "https://api.anthropic.com/v1"
ANTHROPIC_VERSION = "2023-06-01"

CLOUD_KEY_ENV = {
    "openai": "OPENAI_API_KEY",
    "gemini": "GEMINI_API_KEY",
    "anthropic": "ANTHROPIC_API_KEY",
}
CLOUD_PROVIDERS = ("openai", "gemini", "anthropic")
LOCAL_URL_ENV = "LOCAL_LLM_URL"

MAX_OUTPUT_TOKENS = 1024
REQUEST_TIMEOUT = 300.0

_PRIVATE_NETWORKS = tuple(ipaddress.ip_network(n) for n in (
    "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "169.254.0.0/16", "fc00::/7", "fe80::/10",
))

LOCAL_SETUP_HINT = (
    "Start a local model server first, for example LM Studio: load a model and enable "
    "its local server (default http://127.0.0.1:1234/v1). For Ollama, llama.cpp or vLLM "
    f"set {LOCAL_URL_ENV}. To check only the bibliography, use --bib-check-only. "
    "To use a cloud provider, use --cloud PROVIDER --model NAME."
)


class ProviderSetupError(Exception):
    """The provider cannot be used as configured; nothing has been sent to a model."""


class ProviderFailure(Exception):
    """The provider stopped working during the run (e.g. unreachable, access denied)."""


@dataclass
class ProviderConfig:
    mode: str  # "local" or "cloud"
    provider: str  # "local", "openai", "gemini" or "anthropic"
    base_url: str
    model: str | None
    api_key: str | None = field(default=None, repr=False)


@dataclass
class Usage:
    calls: int = 0
    input_tokens: int | None = None
    output_tokens: int | None = None
    cached_tokens: int | None = None

    def add(self, name: str, value: Any) -> None:
        if isinstance(value, bool) or not isinstance(value, int):
            return
        current = getattr(self, name)
        setattr(self, name, value if current is None else current + value)


@dataclass
class Reply:
    verdict: dict[str, str] | None  # validated object, or None if the output was invalid
    requests: int


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


def _host_class(host: str) -> str:
    """``loopback``, ``private`` or ``public`` for an IP literal; ``name`` otherwise."""
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return "loopback" if host.lower() == "localhost" else "name"
    if address.is_loopback:
        return "loopback"
    if any(address in net for net in _PRIVATE_NETWORKS):
        return "private"
    return "public"


def _resolve_name(host: str) -> list[str]:
    try:
        infos = socket.getaddrinfo(host, None)
    except OSError:
        return []
    return sorted({str(info[4][0]) for info in infos})


def check_local_url(base_url: str, allow_private_host: bool = True) -> str:
    """Validate a local model server URL and return it without a trailing slash.

    Loopback and private network addresses (RFC 1918, link-local) are
    accepted. A host name is accepted if it resolves only to such
    addresses. Public addresses are refused.
    """
    parts = urlsplit(base_url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ProviderSetupError(
            f"invalid {LOCAL_URL_ENV} {redact(base_url)!r}: expected http(s)://host:port/v1"
        )
    host = parts.hostname
    kind = _host_class(host)
    if kind == "name" and allow_private_host:
        classes = {_host_class(a.split("%", 1)[0]) for a in _resolve_name(host)}
        kind = "private" if classes and classes <= {"loopback", "private"} else "public"
    if kind == "loopback" or (kind == "private" and allow_private_host):
        return base_url.rstrip("/")
    raise ProviderSetupError(
        f"{host} is not a loopback or private network address. The local model server must run "
        "on this machine or in the private network. To send claims to a cloud provider use --cloud."
    )


def local_config(base_url: str | None, model: str | None, allow_private_host: bool = True) -> ProviderConfig:
    url = check_local_url(base_url or LOCAL_DEFAULT_BASE_URL, allow_private_host)
    return ProviderConfig("local", "local", url, model)


def cloud_config(provider: str, model: str | None, environ: Any = None) -> ProviderConfig:
    """Configuration for ``--cloud PROVIDER``: needs ``--model`` and the provider's key."""
    environ = os.environ if environ is None else environ
    if provider not in CLOUD_PROVIDERS:
        raise ProviderSetupError(f"unknown provider {provider!r} (choose from {', '.join(CLOUD_PROVIDERS)})")
    if not model:
        raise ProviderSetupError("--cloud needs --model")
    key_var = CLOUD_KEY_ENV[provider]
    url = {"openai": OPENAI_BASE_URL, "gemini": GEMINI_BASE_URL, "anthropic": ANTHROPIC_BASE_URL}[provider]
    key = (environ.get(key_var) or "").strip()
    if not key:
        raise ProviderSetupError(f"{key_var} is not set")
    return ProviderConfig("cloud", provider, url.rstrip("/"), model, key)


# ---------------------------------------------------------------------------
# Clients
# ---------------------------------------------------------------------------


def _error_detail(response: Any, secrets: tuple[str | None, ...] = ()) -> str:
    """The provider's error message, redacted and then cut to 200 characters."""
    try:
        data = response.json()
    except ValueError:
        return ""
    if isinstance(data, dict):
        error = data.get("error")
        if isinstance(error, dict):
            return redact(str(error.get("message") or ""), secrets)[:200]
        if isinstance(error, str):
            return redact(error, secrets)[:200]
    return ""


REDIRECT_REFUSED = "the model server answered with a redirect, which is not followed"


class _Transient(Exception):
    """A single request failed (rate limit, server error, timeout); the run continues."""


class ChatClient:
    """Base class: HTTP handling, usage accounting and the repair step."""

    def __init__(self, config: ProviderConfig, session: Any) -> None:
        self.config = config
        self.session = session
        self.usage = Usage()

    @property
    def label(self) -> str:
        return self.config.provider

    def _clean(self, text: str) -> str:
        return redact(text, (self.config.api_key,))

    def _detail(self, response: Any) -> str:
        return _error_detail(response, (self.config.api_key,))

    def _post(self, url: str, body: dict[str, Any], headers: dict[str, str]) -> dict[str, Any]:
        """POST and return the JSON body; raise :class:`ProviderFailure` or ``_Transient``."""
        self.usage.calls += 1
        try:
            response = self.session.post(
                url, data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
                headers={"User-Agent": USER_AGENT, "Content-Type": "application/json", **headers},
                timeout=REQUEST_TIMEOUT, allow_redirects=False,
            )
        except requests.Timeout:
            raise _Transient("model request timed out") from None
        except requests.RequestException as exc:
            raise ProviderFailure(self._clean(f"model server unreachable ({type(exc).__name__})")) from None
        code = response.status_code
        if 300 <= code < 400:
            raise ProviderFailure(f"{REDIRECT_REFUSED} (HTTP {code})")
        if code in (401, 403):
            raise ProviderFailure(self._clean(f"HTTP {code} from the model provider (access denied)"))
        if code == 404:
            detail = self._detail(response)
            raise ProviderFailure(f"HTTP 404 from the model provider{': ' + detail if detail else ''}")
        if code in (400, 413, 422, 429) or code >= 500:
            detail = self._detail(response)
            raise _Transient(f"model request failed (HTTP {code}){': ' + detail if detail else ''}")
        if not 200 <= code < 300:
            detail = self._detail(response)
            raise ProviderFailure(f"HTTP {code} from the model provider{': ' + detail if detail else ''}")
        try:
            data = response.json()
        except ValueError:
            return {}
        return data if isinstance(data, dict) else {}

    def judge(self, system: str, user: str) -> Reply:
        """One claim: a request plus at most one repair request."""
        first = self._first(system, user)
        verdict = self._extract(first)
        if verdict is not None:
            return Reply(verdict, 1)
        second = self._repair(system, user, first)
        return Reply(self._extract(second), 2)

    # subclasses
    def _first(self, system: str, user: str) -> dict[str, Any]:  # pragma: no cover
        raise NotImplementedError

    def _repair(self, system: str, user: str, previous: dict[str, Any]) -> dict[str, Any]:  # pragma: no cover
        raise NotImplementedError

    def _extract(self, data: dict[str, Any]) -> dict[str, str] | None:  # pragma: no cover
        raise NotImplementedError


class OpenAICompatibleClient(ChatClient):
    """Chat completions with ``response_format`` = ``json_schema``."""

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.config.api_key}"} if self.config.api_key else {}

    def _body(self, messages: list[dict[str, str]]) -> dict[str, Any]:
        return {
            "model": self.config.model,
            "messages": messages,
            "temperature": 0,
            "response_format": {
                "type": "json_schema",
                "json_schema": {"name": SCHEMA_NAME, "strict": True, "schema": VERDICT_SCHEMA},
            },
        }

    @staticmethod
    def messages(system: str, user: str) -> list[dict[str, str]]:
        return [{"role": "system", "content": system}, {"role": "user", "content": user}]

    def _send(self, messages: list[dict[str, str]]) -> dict[str, Any]:
        data = self._post(f"{self.config.base_url}/chat/completions", self._body(messages), self._headers())
        usage = data.get("usage")
        if isinstance(usage, dict):
            self.usage.add("input_tokens", usage.get("prompt_tokens"))
            self.usage.add("output_tokens", usage.get("completion_tokens"))
            details = usage.get("prompt_tokens_details")
            if isinstance(details, dict):
                self.usage.add("cached_tokens", details.get("cached_tokens"))
        return data

    def _first(self, system: str, user: str) -> dict[str, Any]:
        return self._send(self.messages(system, user))

    @staticmethod
    def _content(data: dict[str, Any]) -> str:
        try:
            content = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError):
            return ""
        return content if isinstance(content, str) else ""

    def _repair(self, system: str, user: str, previous: dict[str, Any]) -> dict[str, Any]:
        messages = self.messages(system, user) + [
            {"role": "assistant", "content": self._content(previous) or "(empty reply)"},
            {"role": "user", "content": REPAIR_PROMPT},
        ]
        return self._send(messages)

    def _extract(self, data: dict[str, Any]) -> dict[str, str] | None:
        return parse_verdict_text(self._content(data))

    def list_models(self) -> list[str]:
        """Model identifiers offered by a local server (``GET /models``)."""
        where = redact(self.config.base_url)
        try:
            response = self.session.get(
                f"{self.config.base_url}/models", headers={"User-Agent": USER_AGENT}, timeout=10,
                allow_redirects=False,
            )
        except requests.RequestException:
            raise ProviderSetupError(f"no model server answers at {where}. {LOCAL_SETUP_HINT}") from None
        if 300 <= response.status_code < 400:
            raise ProviderSetupError(f"{REDIRECT_REFUSED} (HTTP {response.status_code} at {where})")
        if response.status_code != 200:
            raise ProviderSetupError(
                f"the model server at {where} answered HTTP {response.status_code} "
                f"to GET /models. {LOCAL_SETUP_HINT}"
            )
        try:
            data = response.json()
        except ValueError:
            data = None
        items = data.get("data") if isinstance(data, dict) else None
        if not isinstance(items, list):
            raise ProviderSetupError(f"the model server at {where} did not return a model list. {LOCAL_SETUP_HINT}")
        return [str(item["id"]) for item in items if isinstance(item, dict) and item.get("id")]


_TOOL_NAME = "record_verdict"


class AnthropicClient(ChatClient):
    """Native Messages API with a forced tool call for schema-conform output."""

    def _headers(self) -> dict[str, str]:
        return {"x-api-key": self.config.api_key or "", "anthropic-version": ANTHROPIC_VERSION}

    def _body(self, system: str, messages: list[dict[str, Any]]) -> dict[str, Any]:
        return {
            "model": self.config.model,
            "max_tokens": MAX_OUTPUT_TOKENS,
            "temperature": 0,
            "system": system,
            "messages": messages,
            "tools": [{
                "name": _TOOL_NAME,
                "description": "Record the verdict for the claim.",
                "input_schema": VERDICT_SCHEMA,
            }],
            "tool_choice": {"type": "tool", "name": _TOOL_NAME},
        }

    def _send(self, system: str, messages: list[dict[str, Any]]) -> dict[str, Any]:
        data = self._post(f"{self.config.base_url}/messages", self._body(system, messages), self._headers())
        usage = data.get("usage")
        if isinstance(usage, dict):
            self.usage.add("input_tokens", usage.get("input_tokens"))
            self.usage.add("output_tokens", usage.get("output_tokens"))
            self.usage.add("cached_tokens", usage.get("cache_read_input_tokens"))
        return data

    @staticmethod
    def _tool_use(data: dict[str, Any]) -> dict[str, Any] | None:
        for block in data.get("content") or ():
            if isinstance(block, dict) and block.get("type") == "tool_use":
                return block
        return None

    def _first(self, system: str, user: str) -> dict[str, Any]:
        return self._send(system, [{"role": "user", "content": user}])

    def _repair(self, system: str, user: str, previous: dict[str, Any]) -> dict[str, Any]:
        block = self._tool_use(previous)
        if block is None or not block.get("id"):
            messages = [
                {"role": "user", "content": user},
                {"role": "assistant", "content": "(no tool call)"},
                {"role": "user", "content": REPAIR_PROMPT},
            ]
        else:
            messages = [
                {"role": "user", "content": user},
                {"role": "assistant", "content": [block]},
                {"role": "user", "content": [{
                    "type": "tool_result", "tool_use_id": block["id"], "is_error": True,
                    "content": REPAIR_PROMPT,
                }]},
            ]
        return self._send(system, messages)

    def _extract(self, data: dict[str, Any]) -> dict[str, str] | None:
        block = self._tool_use(data)
        return validate_verdict(block.get("input")) if block else None


def make_client(config: ProviderConfig, session: Any) -> ChatClient:
    """The client for ``config``. A local client ignores proxy settings of the environment."""
    if config.mode == "local":
        session.trust_env = False
    if config.provider == "anthropic":
        return AnthropicClient(config, session)
    return OpenAICompatibleClient(config, session)


def choose_local_model(client: OpenAICompatibleClient, requested: str | None) -> str:
    """Check the local server before the first claim and settle the model name."""
    models = client.list_models()
    if requested:
        if requested in models:
            return requested
        listing = ", ".join(models) if models else "none"
        raise ProviderSetupError(f"model {requested!r} is not available on the local server (available: {listing})")
    if len(models) == 1:
        return models[0]
    if not models:
        raise ProviderSetupError(f"the local server offers no model. {LOCAL_SETUP_HINT}")
    raise ProviderSetupError("several models are available, choose one with --model (available: "
                             + ", ".join(models) + ")")


__all__ = [
    "ChatClient", "ProviderConfig", "ProviderFailure", "ProviderSetupError", "Reply", "Usage",
    "cloud_config", "local_config", "make_client", "choose_local_model", "SYSTEM_PROMPT",
]
