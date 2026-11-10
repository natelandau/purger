"""Tests that validate every built-in plugin definition."""

import importlib
import pkgutil

import pytest

import purger.plugins
from purger.plugin import Plugin, Tier
from purger.registry import discover_plugins

_PLUGINS = list(discover_plugins().values())


def test_discover_plugins_finds_v1_set() -> None:
    """Verify the built-in plugin set is exactly the v1 plugins."""
    # Given / When
    names = set(discover_plugins())

    # Then
    assert names == {"apt", "brew", "docker", "prek", "uv", "xcode"}


_MODULE_NAMES = sorted(m.name for m in pkgutil.iter_modules(purger.plugins.__path__))


@pytest.mark.parametrize("module_name", _MODULE_NAMES)
def test_plugin_name_matches_module(module_name: str) -> None:
    """Verify each plugin module defines a plugin named after the module."""
    # Given
    module = importlib.import_module(f"purger.plugins.{module_name}")

    # When
    plugin = module.PLUGIN

    # Then
    assert plugin.name == module_name


@pytest.mark.parametrize("plugin", _PLUGINS, ids=lambda p: p.name)
def test_plugin_multiple_commands_declare_footprint(plugin: Plugin) -> None:
    """Verify plugins with two or more command actions size themselves."""
    # Given
    actions = plugin.actions

    # When
    command_actions = [a for a in actions if a.command is not None]

    # Then
    if len(command_actions) >= 2:
        assert plugin.has_own_footprint


@pytest.mark.parametrize("plugin", _PLUGINS, ids=lambda p: p.name)
def test_plugin_risky_actions_explain_cost(plugin: Plugin) -> None:
    """Verify every caution and dangerous action has help text and no em-dash."""
    # Given / When / Then
    for action in plugin.actions:
        if action.tier in {Tier.CAUTION, Tier.DANGEROUS}:
            assert action.help.strip()
        assert "\u2014" not in action.help


@pytest.mark.parametrize("plugin", _PLUGINS, ids=lambda p: p.name)
def test_plugin_volume_prune_is_dangerous(plugin: Plugin) -> None:
    """Verify no action prunes volumes below the dangerous tier."""
    # Given / When / Then
    for action in plugin.actions:
        command = action.command or ()
        if "volume" in command and "prune" in command:
            assert action.tier is Tier.DANGEROUS


_EXPECTED_ACTIONS: list[tuple[str, str, Tier, tuple[str, ...] | None]] = [
    ("apt", "autoclean", Tier.SAFE, ("apt-get", "autoclean")),
    ("apt", "clean", Tier.SAFE, ("apt-get", "clean")),
    ("brew", "cleanup", Tier.SAFE, ("brew", "cleanup", "--prune=all")),
    ("docker", "builder", Tier.SAFE, ("docker", "builder", "prune", "--all", "--force")),
    ("docker", "system", Tier.CAUTION, ("docker", "system", "prune", "--all", "--force")),
    ("docker", "volumes", Tier.DANGEROUS, ("docker", "volume", "prune", "--all", "--force")),
    ("prek", "gc", Tier.SAFE, ("prek", "cache", "gc")),
    ("prek", "clean", Tier.DANGEROUS, ("prek", "cache", "clean")),
    ("uv", "prune", Tier.SAFE, ("uv", "cache", "prune")),
    ("uv", "clean", Tier.DANGEROUS, ("uv", "cache", "clean", "--force")),
    ("xcode", "derived-data", Tier.SAFE, None),
    ("xcode", "device-support", Tier.CAUTION, None),
    ("xcode", "archives", Tier.DANGEROUS, None),
]


def test_plugin_actions_are_pinned() -> None:
    """Verify the full set of built-in actions matches the reviewed list."""
    # Given
    plugins = discover_plugins()

    # When
    actual = [(p.name, a.name) for p in plugins.values() for a in p.actions]

    # Then
    assert sorted(actual) == sorted((plugin, action) for plugin, action, _, _ in _EXPECTED_ACTIONS)


@pytest.mark.parametrize(
    ("plugin_name", "action_name", "tier", "command"),
    _EXPECTED_ACTIONS,
    ids=[f"{plugin}/{action}" for plugin, action, _, _ in _EXPECTED_ACTIONS],
)
def test_plugin_action_tier_and_command(
    plugin_name: str, action_name: str, tier: Tier, command: tuple[str, ...] | None
) -> None:
    """Verify each built-in action keeps its reviewed tier and command."""
    # Given
    plugin = discover_plugins()[plugin_name]

    # When
    action = next(a for a in plugin.actions if a.name == action_name)

    # Then
    assert action.tier is tier
    assert action.command == command


def test_docker_volumes_count_outside_footprint() -> None:
    """Verify Docker volumes are added to the footprint, since docker df reclaimable excludes them."""
    # Given
    plugin = discover_plugins()["docker"]

    # When
    flags = {a.name: a.outside_footprint for a in plugin.actions}

    # Then
    assert flags == {"builder": False, "system": False, "volumes": True}
