"""Tests that validate every built-in plugin definition."""

import importlib
import pkgutil

import pytest

import purger.plugins
from purger.plugin import Plugin, Tier
from purger.registry import discover_plugins

_PLUGINS = list(discover_plugins().values())


def test_discover_plugins_finds_builtin_set() -> None:
    """Verify the built-in plugin set is exactly the reviewed plugins."""
    # Given / When
    names = set(discover_plugins())

    # Then
    assert names == {
        "ansible",
        "apt",
        "brew",
        "bun",
        "cocoapods",
        "conda",
        "corepack",
        "dnf",
        "docker",
        "flatpak",
        "go",
        "macports",
        "maestro",
        "mise",
        "npm",
        "pacman",
        "pip",
        "playwright",
        "pnpm",
        "podman",
        "pre-commit",
        "prek",
        "rip2",
        "simulators",
        "swiftpm",
        "thumbnails",
        "trash",
        "uv",
        "xcode",
        "yarn",
    }


_MODULE_NAMES = sorted(
    m.name for m in pkgutil.iter_modules(purger.plugins.__path__) if not m.name.startswith("_")
)


@pytest.mark.parametrize("module_name", _MODULE_NAMES)
def test_plugin_name_matches_module(module_name: str) -> None:
    """Verify each plugin module defines a plugin named after the module, with hyphens for underscores."""
    # Given
    module = importlib.import_module(f"purger.plugins.{module_name}")

    # When
    plugin = module.PLUGIN

    # Then
    assert plugin.name == module_name.replace("_", "-")


@pytest.mark.parametrize("plugin", _PLUGINS, ids=lambda p: p.name)
def test_plugin_multiple_commands_declare_footprint(plugin: Plugin) -> None:
    """Verify plugins with two or more command actions size themselves.

    Summing action sizes is safe only when every command action measures exactly
    what it frees, so a plugin is exempt only when each one has its own size.
    """
    # Given
    actions = plugin.actions

    # When
    command_actions = [a for a in actions if a.command is not None]
    all_sized = all(a.size is not None or a.size_cmd is not None for a in command_actions)

    # Then
    if len(command_actions) >= 2 and not all_sized:
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
    ("conda", "clean", Tier.SAFE, ("conda", "clean", "--all", "--yes")),
    ("dnf", "packages", Tier.SAFE, ("dnf", "clean", "packages")),
    (
        "flatpak",
        "unused-system",
        Tier.SAFE,
        ("flatpak", "uninstall", "--unused", "-y", "--noninteractive", "--system"),
    ),
    (
        "flatpak",
        "unused-user",
        Tier.SAFE,
        ("flatpak", "uninstall", "--unused", "-y", "--noninteractive", "--user"),
    ),
    ("pacman", "old-versions", Tier.SAFE, ("paccache", "--remove")),
    (
        "pacman",
        "uninstalled",
        Tier.SAFE,
        ("paccache", "--remove", "--uninstalled", "--keep", "0"),
    ),
    ("podman", "system", Tier.CAUTION, ("podman", "system", "prune", "--all", "--force")),
    ("podman", "volumes", Tier.DANGEROUS, ("podman", "volume", "prune", "--force")),
    ("thumbnails", "cache", Tier.SAFE, None),
    ("ansible", "galaxy-cache", Tier.SAFE, None),
    ("ansible", "tmp", Tier.SAFE, None),
    ("apt", "autoclean", Tier.SAFE, ("apt-get", "autoclean")),
    ("apt", "clean", Tier.SAFE, ("apt-get", "clean")),
    ("brew", "cleanup", Tier.SAFE, ("brew", "cleanup", "--prune=all")),
    ("brew", "autoremove", Tier.CAUTION, ("brew", "autoremove")),
    ("docker", "builder", Tier.SAFE, ("docker", "builder", "prune", "--all", "--force")),
    ("docker", "system", Tier.CAUTION, ("docker", "system", "prune", "--all", "--force")),
    ("docker", "volumes", Tier.DANGEROUS, ("docker", "volume", "prune", "--all", "--force")),
    ("go", "build-cache", Tier.SAFE, ("go", "clean", "-cache")),
    ("go", "modcache", Tier.CAUTION, ("go", "clean", "-modcache")),
    (
        "macports",
        "clean",
        Tier.SAFE,
        ("/opt/local/bin/port", "-N", "clean", "--all", "installed"),
    ),
    ("maestro", "test-output", Tier.CAUTION, None),
    ("macports", "reclaim", Tier.CAUTION, ("/opt/local/bin/port", "-N", "reclaim")),
    ("mise", "cache-prune", Tier.SAFE, ("mise", "cache", "prune")),
    ("mise", "prune", Tier.CAUTION, ("mise", "prune", "--yes")),
    ("mise", "cache-clear", Tier.DANGEROUS, ("mise", "cache", "clear")),
    ("npm", "verify", Tier.SAFE, ("npm", "cache", "verify")),
    ("npm", "npx", Tier.SAFE, ("npm", "cache", "npx", "rm", "--force")),
    ("npm", "clean", Tier.DANGEROUS, ("npm", "cache", "clean", "--force")),
    ("pip", "cache", Tier.CAUTION, None),
    ("pnpm", "metadata-prune", Tier.SAFE, ("pnpm", "cache", "prune", "--yes")),
    ("pnpm", "prune", Tier.SAFE, ("pnpm", "store", "prune")),
    ("pre-commit", "gc", Tier.SAFE, ("pre-commit", "gc")),
    ("pre-commit", "clean", Tier.DANGEROUS, ("pre-commit", "clean")),
    ("prek", "gc", Tier.SAFE, ("prek", "cache", "gc")),
    ("prek", "clean", Tier.DANGEROUS, ("prek", "cache", "clean")),
    ("uv", "prune", Tier.SAFE, ("uv", "cache", "prune")),
    ("uv", "clean", Tier.DANGEROUS, ("uv", "cache", "clean", "--force")),
    ("rip2", "decompose", Tier.CAUTION, ("rip", "--decompose", "--force")),
    ("trash", "empty", Tier.CAUTION, None),
    ("xcode", "derived-data", Tier.SAFE, None),
    ("xcode", "device-support", Tier.CAUTION, None),
    ("xcode", "archives", Tier.DANGEROUS, None),
    ("yarn", "cache", Tier.CAUTION, None),
    ("bun", "prune", Tier.SAFE, ("bun", "pm", "cache", "prune", "--max-age", "30")),
    ("bun", "clean", Tier.DANGEROUS, ("bun", "pm", "cache", "rm")),
    ("corepack", "cache", Tier.CAUTION, ("corepack", "cache", "clean")),
    ("playwright", "old-browsers", Tier.CAUTION, None),
    ("cocoapods", "cache", Tier.CAUTION, ("pod", "cache", "clean", "--all")),
    ("swiftpm", "purge-cache", Tier.CAUTION, ("swift", "package", "purge-cache")),
    ("swiftpm", "artifacts", Tier.CAUTION, None),
    ("simulators", "unavailable", Tier.SAFE, ("xcrun", "simctl", "delete", "unavailable")),
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


@pytest.mark.parametrize(
    ("plugin_name", "action_name"), [("brew", "autoremove"), ("macports", "reclaim")]
)
def test_uninstalling_actions_need_confirmation(plugin_name: str, action_name: str) -> None:
    """Verify actions that uninstall packages are never in the safe tier."""
    # Given
    plugin = discover_plugins()[plugin_name]

    # When
    action = next(a for a in plugin.actions if a.name == action_name)

    # Then
    assert action.tier is not Tier.SAFE


def test_mise_prune_counts_outside_footprint() -> None:
    """Verify pruned tool versions add to the footprint, since the mise cache excludes them."""
    # Given
    plugin = discover_plugins()["mise"]

    # When
    flags = {a.name: a.outside_footprint for a in plugin.actions}

    # Then
    assert flags == {"cache-prune": False, "prune": True, "cache-clear": False}


@pytest.mark.parametrize("plugin", _PLUGINS, ids=lambda p: p.name)
def test_plugin_action_help_starts_with_a_verb(plugin: Plugin) -> None:
    """Verify every action's help says what it does, since prompts show it in place of the name."""
    # Given
    verbs = {"Remove", "Wipe", "Uninstall", "Garbage-collect"}

    # When
    first_words = {action.name: action.help.split(" ", 1)[0] for action in plugin.actions}

    # Then
    assert {name: word for name, word in first_words.items() if word not in verbs} == {}
