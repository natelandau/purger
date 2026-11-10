"""Tests for the plugin data model."""

from collections.abc import Callable

import pytest

from purger.plugin import Action, Plugin, Tier
from tests.conftest import make_action, make_plugin


def test_action_requires_command_or_paths() -> None:
    """Verify an action with neither command nor paths is rejected."""
    # Given / When / Then
    with pytest.raises(ValueError, match="command or paths"):
        Action(name="x", tier=Tier.SAFE)


def test_action_sudo_requires_command() -> None:
    """Verify needs_sudo is rejected on a paths-only action."""
    # Given / When / Then
    with pytest.raises(ValueError, match="needs_sudo"):
        Action(name="x", tier=Tier.SAFE, paths=("~/a",), needs_sudo=True)


@pytest.mark.parametrize("name", ["Derived_Data", "derived data", "-a", "a-", "", "abc\n"])
def test_action_rejects_non_kebab_name(name: str) -> None:
    """Verify action names must be kebab-case."""
    # Given / When / Then
    with pytest.raises(ValueError, match="kebab-case"):
        make_action(name=name)


@pytest.mark.parametrize("name", ["Xcode", "xcode\n"])
def test_plugin_rejects_non_kebab_name(name: str) -> None:
    """Verify plugin names must be kebab-case, with nothing trailing."""
    # Given / When / Then
    with pytest.raises(ValueError, match="kebab-case"):
        make_plugin(name=name)


def test_names_accept_kebab_case() -> None:
    """Verify kebab-case names are accepted."""
    # Given / When
    action = make_action(name="derived-data")
    plugin = make_plugin(name="xcode-tools")

    # Then
    assert action.name == "derived-data"
    assert plugin.name == "xcode-tools"


def test_plugin_rejects_duplicate_action_names() -> None:
    """Verify duplicate action names within a plugin are rejected."""
    # Given / When / Then
    with pytest.raises(ValueError, match="duplicate"):
        make_plugin(actions=(make_action(name="a"), make_action(name="a")))


def test_plugin_rejects_empty_actions() -> None:
    """Verify a plugin needs at least one action."""
    # Given / When / Then
    with pytest.raises(ValueError, match="at least one action"):
        make_plugin(actions=())


def test_plugin_rejects_unknown_platform() -> None:
    """Verify only darwin and linux platforms are accepted."""
    # Given / When / Then
    with pytest.raises(ValueError, match="platform"):
        make_plugin(platforms=frozenset({"win32"}))


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        ({}, False),
        ({"paths": ("~/x",)}, True),
        ({"size_cmd": ("echo", "1")}, True),
        ({"size": lambda: 1}, True),
    ],
)
def test_plugin_has_own_footprint(overrides: dict[str, object], expected: bool) -> None:
    """Verify has_own_footprint reflects a size hook, size_cmd, or paths."""
    # Given / When
    plugin = make_plugin(**overrides)

    # Then
    assert plugin.has_own_footprint is expected


def test_factory_fixtures_return_the_functions(
    make_action: Callable[..., Action], make_plugin: Callable[..., Plugin]
) -> None:
    """Verify the fixtures expose the same factories."""
    # Given / When / Then
    assert make_action().name == "a"
    assert make_plugin().name == "p"
