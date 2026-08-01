"""The dependency rule, enforced by a test instead of by discipline.

Architecture documents rot; a failing test does not. This module parses every
module under ``src/mediahub`` and asserts that its imports stay inside the
layer's allowance. A violation fails CI with the exact file and the exact
import, which is what keeps the boundaries real after the first deadline.

The rules:

===============  ==================================================
Layer            May import
===============  ==================================================
``domain``       ``domain`` only, plus the standard library
``application``  ``domain``, ``application``, ``shared``, Loguru
``infrastructure`` everything except ``presentation``
``presentation`` everything, but ``infrastructure`` only in the
                 composition root (``app``, ``lifespan``,
                 ``dependencies``)
``shared``       ``shared`` only
===============  ==================================================
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.architecture

SOURCE_ROOT = Path(__file__).resolve().parents[2] / "src" / "mediahub"
PACKAGE = "mediahub"

LAYERS = ("domain", "application", "infrastructure", "presentation", "shared")

ALLOWED_LAYERS: dict[str, frozenset[str]] = {
    "domain": frozenset({"domain"}),
    "application": frozenset({"domain", "application", "shared"}),
    "infrastructure": frozenset({"domain", "application", "infrastructure", "shared"}),
    "presentation": frozenset(
        {"domain", "application", "presentation", "shared", "infrastructure"}
    ),
    "shared": frozenset({"shared"}),
}

# Third-party packages each layer may import. The domain allows none at all.
ALLOWED_THIRD_PARTY: dict[str, frozenset[str]] = {
    "domain": frozenset(),
    "application": frozenset({"loguru"}),
    "infrastructure": frozenset({"loguru", "sqlalchemy", "alembic"}),
    "presentation": frozenset({"fastapi", "starlette", "pydantic", "loguru"}),
    "shared": frozenset({"loguru", "pydantic", "pydantic_settings"}),
}

# Packages that are confined to one part of a layer rather than the whole of it.
# An engine-specific dependency must not leak into sibling adapters: the moment
# `yt_dlp` is importable from, say, a delivery provider, "the rest of the system
# does not know yt-dlp exists" has quietly stopped being true.
SCOPED_THIRD_PARTY: dict[str, tuple[str, ...]] = {
    "yt_dlp": (
        "mediahub.infrastructure.download.",
        "mediahub.infrastructure.sources.",
    ),
    "telegram": ("mediahub.infrastructure.delivery.telegram.",),
}

# Only a composition root may reach into infrastructure.
COMPOSITION_ROOT_MODULES = frozenset(
    {
        "mediahub.presentation.api.app",
        "mediahub.presentation.api.lifespan",
        "mediahub.presentation.api.dependencies",
        "mediahub.presentation.telegram.__main__",
        "mediahub.presentation.worker.__main__",
    }
)

# Words that belong to one external system. Finding them anywhere else means a
# vendor's model has leaked into ours, and "Telegram is one interface" has
# quietly stopped being true. `message_id` is deliberately absent: it is a
# generic concept the delivery contract legitimately uses.
FOREIGN_VOCABULARY: tuple[str, ...] = (
    "chat_id",
    "file_id",
    "file_unique_id",
    "callback_query",
    "inline_keyboard",
    "reply_markup",
    "answer_callback",
)

VOCABULARY_EXEMPT_PREFIXES: tuple[str, ...] = (
    "mediahub.presentation.telegram",
    "mediahub.infrastructure.delivery.telegram",
)


def iter_source_modules() -> list[tuple[str, Path]]:
    """Return ``(module_name, path)`` for every module under ``src/mediahub``."""
    modules: list[tuple[str, Path]] = []
    for path in sorted(SOURCE_ROOT.rglob("*.py")):
        relative = path.relative_to(SOURCE_ROOT).with_suffix("")
        parts = list(relative.parts)
        if parts[-1] == "__init__":
            parts.pop()
        modules.append((".".join([PACKAGE, *parts]), path))
    return modules


def imported_modules(path: Path) -> set[str]:
    """Return every absolute module name imported by the file at ``path``."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    imports: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            imports.add(node.module)
    return imports


def layer_of(module_name: str) -> str | None:
    """Return the layer a ``mediahub.*`` module belongs to, if any."""
    parts = module_name.split(".")
    if len(parts) < 2 or parts[0] != PACKAGE:
        return None
    return parts[1] if parts[1] in LAYERS else None


SOURCE_MODULES = iter_source_modules()


def test_the_scan_actually_found_the_source_tree() -> None:
    """Guard against the rules silently passing because nothing was scanned."""
    assert SOURCE_ROOT.is_dir()
    assert len(SOURCE_MODULES) > 30


@pytest.mark.parametrize(
    ("module_name", "path"),
    SOURCE_MODULES,
    ids=[name for name, _ in SOURCE_MODULES],
)
def test_layer_only_imports_allowed_layers(module_name: str, path: Path) -> None:
    """No module may import a layer its own layer is not allowed to know."""
    layer = layer_of(module_name)
    if layer is None:
        return

    allowed = ALLOWED_LAYERS[layer]
    for imported in imported_modules(path):
        target_layer = layer_of(imported)
        if target_layer is None or target_layer in allowed:
            continue
        pytest.fail(
            f"{module_name} ({layer}) imports {imported} ({target_layer}); "
            f"{layer} may only import {sorted(allowed)}"
        )


@pytest.mark.parametrize(
    ("module_name", "path"),
    SOURCE_MODULES,
    ids=[name for name, _ in SOURCE_MODULES],
)
def test_layer_only_imports_allowed_third_party(module_name: str, path: Path) -> None:
    """Inner layers stay free of frameworks; the domain has no dependencies."""
    layer = layer_of(module_name)
    if layer is None:
        return

    allowed = ALLOWED_THIRD_PARTY[layer]
    for imported in imported_modules(path):
        root = imported.split(".")[0]
        if root == PACKAGE or root in sys.stdlib_module_names or root in allowed:
            continue
        scopes = SCOPED_THIRD_PARTY.get(root)
        if scopes is not None:
            if module_name.startswith(scopes):
                continue
            pytest.fail(f"{module_name} imports '{root}', which is confined to {list(scopes)}")
        pytest.fail(
            f"{module_name} ({layer}) imports third-party '{root}'; "
            f"{layer} may only import {sorted(allowed) or 'the standard library'}"
        )


def test_only_the_composition_root_touches_infrastructure() -> None:
    """Routers and schemas must not know which adapters exist."""
    offenders: list[str] = []
    for module_name, path in SOURCE_MODULES:
        if layer_of(module_name) != "presentation":
            continue
        if module_name in COMPOSITION_ROOT_MODULES:
            continue
        infrastructure_imports = [
            imported
            for imported in imported_modules(path)
            if layer_of(imported) == "infrastructure"
        ]
        offenders.extend(f"{module_name} -> {imported}" for imported in infrastructure_imports)

    assert offenders == [], "Only the API composition root may import infrastructure: " + ", ".join(
        offenders
    )


def _code_words(path: Path) -> set[str]:
    """Return every identifier and string constant a module actually uses.

    Deliberately not a text search: documentation is allowed - and expected - to
    name the vendor concepts it explains. What must not exist is a *field*, a
    *parameter* or a *key* carrying a foreign name.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    words: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            words.add(node.id)
        elif isinstance(node, ast.Attribute):
            words.add(node.attr)
        elif isinstance(node, (ast.arg, ast.keyword)):
            name = node.arg
            if name:
                words.add(name)
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            words.add(node.value)
    return words


def test_no_foreign_vocabulary_outside_its_adapter() -> None:
    """A vendor's words must not appear outside the adapter that owns them.

    This is the mechanical enforcement of "Telegram is not the product". Without
    it, ``chat_id`` becomes a field on an application DTO within a quarter and
    every claim about swapping the interface becomes aspirational.
    """
    offenders: list[str] = []
    for module_name, path in SOURCE_MODULES:
        if module_name.startswith(VOCABULARY_EXEMPT_PREFIXES):
            continue
        used = _code_words(path)
        offenders.extend(f"{module_name}: '{word}'" for word in FOREIGN_VOCABULARY if word in used)

    assert offenders == [], "Telegram vocabulary escaped its adapter: " + ", ".join(offenders)


def test_every_module_is_documented() -> None:
    """Every module carries a docstring - the project's standing requirement."""
    undocumented = [
        module_name
        for module_name, path in SOURCE_MODULES
        if not ast.get_docstring(ast.parse(path.read_text(encoding="utf-8")))
    ]

    assert undocumented == [], f"Modules without a docstring: {undocumented}"
