"""Tests for the plugin registry."""

import importlib
import sys
import textwrap
from collections.abc import Callable, Iterator
from pathlib import Path
from types import ModuleType

import pytest
from pytest_mock import MockerFixture

from purger.registry import (
    DuplicatePluginError,
    UnknownPluginError,
    discover_plugins,
    select_plugins,
    skip_reason,
)
from purger.reports import SkipReason
from tests.conftest import make_plugin

_PLUGIN_SRC = """
from purger.plugin import Action, Plugin, Tier

PLUGIN = Plugin(
    name={name!r},
    description="d",
    actions=(Action(name="a", tier=Tier.SAFE, help="Remove a.", command=("true",)),),
)
"""


@pytest.fixture
def build_package(tmp_path: Path) -> Iterator[Callable[[dict[str, str]], ModuleType]]:
    """Provide a builder for uniquely named plugin packages, unloading them at teardown.

    A unique name keeps parallel tests from sharing sys.modules entries.
    """
    pkg_name = f"pkg_{tmp_path.name.replace('-', '_')}"

    def _build(modules: dict[str, str]) -> ModuleType:
        pkg_dir = tmp_path / pkg_name
        pkg_dir.mkdir()
        (pkg_dir / "__init__.py").write_text("")
        for mod, source in modules.items():
            (pkg_dir / f"{mod}.py").write_text(textwrap.dedent(source))
        sys.path.insert(0, str(tmp_path))
        importlib.invalidate_caches()
        return importlib.import_module(pkg_name)

    yield _build

    if str(tmp_path) in sys.path:
        sys.path.remove(str(tmp_path))
    for name in [n for n in sys.modules if n == pkg_name or n.startswith(f"{pkg_name}.")]:
        del sys.modules[name]


def test_discover_collects_plugin_constants(
    build_package: Callable[[dict[str, str]], ModuleType],
) -> None:
    """Verify discovery returns every public module's PLUGIN keyed by name and sorted."""
    # Given a package with two plugin modules, one without PLUGIN, and one private
    package = build_package(
        {
            "zed": _PLUGIN_SRC.format(name="zed"),
            "alpha": _PLUGIN_SRC.format(name="alpha"),
            "helper": "X = 1\n",
            "_private": _PLUGIN_SRC.format(name="private"),
        },
    )

    # When discovering
    found = discover_plugins(package)

    # Then only the public plugins are returned, sorted by name
    assert list(found) == ["alpha", "zed"]


def test_discover_rejects_duplicate_names(
    build_package: Callable[[dict[str, str]], ModuleType],
) -> None:
    """Verify two modules defining the same plugin name raise DuplicatePluginError."""
    # Given two modules with the same plugin name
    package = build_package(
        {"one": _PLUGIN_SRC.format(name="dup"), "two": _PLUGIN_SRC.format(name="dup")}
    )

    # When
    with pytest.raises(DuplicatePluginError) as exc_info:
        discover_plugins(package)

    # Then the error names the plugin and both modules
    message = str(exc_info.value)
    assert "dup" in message
    assert f"{package.__name__}.one" in message
    assert f"{package.__name__}.two" in message


def test_discover_rejects_non_plugin_constant(
    build_package: Callable[[dict[str, str]], ModuleType],
) -> None:
    """Verify a PLUGIN that is not a Plugin raises TypeError naming the module."""
    # Given a module whose PLUGIN is a string
    package = build_package({"bad": 'PLUGIN = "nope"\n'})

    # When / Then
    with pytest.raises(TypeError, match="bad"):
        discover_plugins(package)


def test_discover_default_package_is_a_dict() -> None:
    """Verify discovery of the built-in package returns a name-ordered dict."""
    # Given / When discovering the built-in plugin package
    found = discover_plugins()

    # Then
    assert list(found) == sorted(found)


def test_skip_reason_platform(monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify a plugin for another platform is skipped as PLATFORM."""
    # Given a darwin-only plugin on linux
    monkeypatch.setattr(sys, "platform", "linux")
    plugin = make_plugin(platforms=frozenset({"darwin"}))

    # When
    reason = skip_reason(plugin)

    # Then
    assert reason is SkipReason.PLATFORM


def test_skip_reason_missing_binary(mocker: MockerFixture) -> None:
    """Verify a missing required binary is skipped as MISSING_BINARY."""
    # Given a plugin whose required binary is not on PATH
    which = mocker.patch("purger.registry.which", autospec=True, return_value=None)
    plugin = make_plugin(requires="tool")

    # When
    reason = skip_reason(plugin)

    # Then
    assert reason is SkipReason.MISSING_BINARY
    which.assert_called_once_with("tool")


def test_skip_reason_unavailable() -> None:
    """Verify a False is_available is skipped unless the check is disabled."""
    # Given a plugin whose availability hook says no
    plugin = make_plugin(is_available=lambda: False)

    # When
    checked = skip_reason(plugin)
    unchecked = skip_reason(plugin, check_available=False)

    # Then
    assert checked is SkipReason.UNAVAILABLE
    assert unchecked is None


def test_skip_reason_is_available_raises() -> None:
    """Verify an is_available hook that raises counts as UNAVAILABLE."""

    # Given an availability hook that raises
    def boom() -> bool:
        msg = "boom"
        raise RuntimeError(msg)

    # When
    reason = skip_reason(make_plugin(is_available=boom))

    # Then
    assert reason is SkipReason.UNAVAILABLE


def test_skip_reason_applicable(monkeypatch: pytest.MonkeyPatch, mocker: MockerFixture) -> None:
    """Verify an applicable plugin has no skip reason."""
    # Given a linux host where the required binary exists and the hook says yes
    monkeypatch.setattr(sys, "platform", "linux")
    mocker.patch("purger.registry.which", autospec=True, return_value=Path("/usr/bin/tool"))
    plugin = make_plugin(requires="tool", is_available=lambda: True)

    # When
    reason = skip_reason(plugin)

    # Then
    assert reason is None


def test_select_plugins() -> None:
    """Verify selection handles empty, ordered, duplicate, and unknown names."""
    # Given
    registry = {"a": make_plugin(name="a"), "b": make_plugin(name="b")}

    # When
    everything = select_plugins(registry, [])
    reordered = select_plugins(registry, ["b", "a"])
    repeated = select_plugins(registry, ["a", "a"])
    with pytest.raises(UnknownPluginError) as exc_info:
        select_plugins(registry, ["nope"])

    # Then
    assert [p.name for p in everything] == ["a", "b"]
    assert [p.name for p in reordered] == ["b", "a"]
    assert [p.name for p in repeated] == ["a"]
    assert str(exc_info.value) == "unknown plugin(s): nope. valid plugins: a, b"


def test_discover_plugins_default_returns_independent_copies() -> None:
    """Verify the cached built-in registry cannot be changed through a returned dict."""
    # Given a discovered registry that a caller mutates
    first = discover_plugins()
    first.clear()

    # When discovering again
    second = discover_plugins()

    # Then the built-in plugins are still all there
    assert second
    assert second is not first
