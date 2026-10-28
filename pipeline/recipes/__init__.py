"""Recipes: one query plus one chart set plus one script template.

A recipe turns a slice of facts into one episode. Running the same recipe
over a later window produces a genuinely new episode, which is where "a
new story every time" comes from -- architecture doc, section 11.
"""

from __future__ import annotations

from typing import Callable, Protocol

from ..errors import ConfigError


class Recipe(Protocol):
    name: str
    tier: str
    title_template: str

    def export(self, **params: object) -> dict: ...


_REGISTRY: dict[str, Recipe] = {}


def register(recipe: Recipe) -> Recipe:
    _REGISTRY[recipe.name] = recipe
    return recipe


def get(name: str) -> Recipe:
    if name not in _REGISTRY:
        _load_builtin()
    if name not in _REGISTRY:
        raise ConfigError(
            f"unknown recipe {name!r}; available: {sorted(_REGISTRY) or 'none loaded'}"
        )
    return _REGISTRY[name]


def available() -> list[str]:
    _load_builtin()
    return sorted(_REGISTRY)


def _load_builtin() -> None:
    from . import fee_drag  # noqa: F401  (registers on import)
