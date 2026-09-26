"""Source registry: built-in sources plus sources installed as plugins.

A plugin is an installed package that declares an entry point in the group
:data:`ENTRY_POINT_GROUP`. The entry point refers to a callable (a function
or a class) that is called without arguments and returns an object
implementing the ``Source`` protocol of ``latex_claim_and_bib_checker``:
``name`` (str), ``order`` (int), ``available()`` and ``fetch(query)``, and
optionally ``required_env`` (tuple of environment variable names).

Only installed packages are considered; nothing is loaded from paths.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from importlib.metadata import entry_points
from typing import Any, Iterable

ENTRY_POINT_GROUP = "latex_claim_and_bib_checker.sources"


class SourceSetupError(Exception):
    """The set of sources cannot be used as requested; the run stops."""


@dataclass
class SourceInfo:
    """One source together with what the registry learned about it."""

    source: Any
    name: str
    order: int
    available: bool
    plugin: str | None = None  # entry point name for plugins
    required_env: tuple[str, ...] = ()


@dataclass
class Registry:
    sources: list[SourceInfo] = field(default_factory=list)
    failed_plugins: dict[str, str] = field(default_factory=dict)  # entry point -> message

    def sorted(self) -> list[SourceInfo]:
        return sorted(self.sources, key=lambda s: (s.order, s.name))


def _describe(source: Any, label: str) -> tuple[str, int, bool, tuple[str, ...]]:
    """Validate a source object; raise ``ValueError`` with a message if unusable."""
    name = getattr(source, "name", None)
    if not isinstance(name, str) or not name.strip():
        raise ValueError(f"{label}: 'name' must be a non-empty string")
    order = getattr(source, "order", None)
    if isinstance(order, bool) or not isinstance(order, int):
        raise ValueError(f"{label}: 'order' must be an integer, got {type(order).__name__}")
    if not callable(getattr(source, "fetch", None)):
        raise ValueError(f"{label}: missing fetch(query)")
    if not callable(getattr(source, "available", None)):
        raise ValueError(f"{label}: missing available()")
    try:
        available = bool(source.available())
    except Exception as exc:
        raise ValueError(f"{label}: available() raised {type(exc).__name__}: {exc}") from None
    required = getattr(source, "required_env", ()) or ()
    if isinstance(required, str):
        required = (required,)
    try:
        required_env = tuple(str(v) for v in required)
    except TypeError:
        required_env = ()
    return name.strip(), order, available, required_env


def _entry_points() -> Iterable[Any]:
    return entry_points(group=ENTRY_POINT_GROUP)


def build_registry(builtins: Iterable[Any]) -> tuple[Registry, list[str]]:
    """Register the built-in sources and every installed plugin.

    Returns the registry and a list of warnings for plugins that could not
    be loaded. Raises :class:`SourceSetupError` if two sources share a name.
    """
    registry = Registry()
    warnings: list[str] = []
    seen: dict[str, str] = {}

    for source in builtins:
        name, order, available, required_env = _describe(source, f"built-in source {source!r}")
        registry.sources.append(SourceInfo(source, name, order, available, None, required_env))
        seen[name] = "a built-in source"

    for ep in sorted(_entry_points(), key=lambda e: e.name):
        label = f"source plugin '{ep.name}'"
        try:
            factory = ep.load()
            source = factory()
            name, order, available, required_env = _describe(source, label)
        except Exception as exc:
            message = str(exc) if isinstance(exc, ValueError) and str(exc).startswith(label) else (
                f"{label}: {type(exc).__name__}: {exc}"
            )
            registry.failed_plugins[ep.name] = message
            warnings.append(f"warning: could not load {message}, continuing without it")
            continue
        if name in seen:
            raise SourceSetupError(
                f"{label} provides the source name '{name}', which is already used by {seen[name]}"
            )
        seen[name] = f"source plugin '{ep.name}'"
        registry.sources.append(SourceInfo(source, name, order, available, ep.name, required_env))
    return registry, warnings


def missing_env(info: SourceInfo, environ: Any) -> list[str]:
    """Names from ``required_env`` that are not set (values are never read out)."""
    return [name for name in info.required_env if not (environ.get(name) or "").strip()]


def select_sources(registry: Registry, requested: list[str] | None) -> list[SourceInfo]:
    """Active sources, sorted by (order, name).

    Without ``requested`` every available source is active. With it (an
    internal setting, there is no command-line option), only the named
    sources are, and naming an unknown, unavailable or broken source stops
    the run.
    """
    by_name = {s.name: s for s in registry.sources}
    if requested:
        chosen: list[SourceInfo] = []
        for name in requested:
            if name in by_name:
                info = by_name[name]
                if not info.available:
                    raise SourceSetupError(f"source '{name}' is not available in this environment")
                if info not in chosen:
                    chosen.append(info)
            elif name in registry.failed_plugins:
                raise SourceSetupError(f"source '{name}' could not be loaded: "
                                       f"{registry.failed_plugins[name]}")
            else:
                known = ", ".join(s.name for s in registry.sorted())
                raise SourceSetupError(f"unknown source '{name}' (known: {known})")
        active = chosen
    else:
        active = [s for s in registry.sources if s.available]
    active = sorted(active, key=lambda s: (s.order, s.name))
    if not active:
        raise SourceSetupError("no abstract source is available")
    return active
