"""Tests for rules that every built-in plugin definition must follow."""

import importlib
import pkgutil

import pytest

import purger.plugins
from purger.plugin import Action, Plugin, RiskLevel, RunCommand, SizeUnknown
from purger.registry import discover_plugins

_PLUGINS = list(discover_plugins().values())


def test_discover_plugins_finds_builtin_set() -> None:
    """Verify the built-in plugin set is exactly the reviewed plugins."""
    # Given / When
    names = set(discover_plugins())

    # Then
    assert names == {
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
        "poetry",
        "podman",
        "pre-commit",
        "prek",
        "projects",
        "rip2",
        "ruby",
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


def _command(action: Action) -> tuple[str, ...] | None:
    """Return the argv an action runs, or None when it deletes paths."""
    return action.cleanup.argv if isinstance(action.cleanup, RunCommand) else None


@pytest.mark.parametrize("plugin", _PLUGINS, ids=lambda p: p.name)
def test_plugin_with_unknown_command_sizes_declares_total_size(plugin: Plugin) -> None:
    """Verify a plugin whose command actions cannot all be sized declares its total size.

    Summing action sizes is right only when every command action measures what
    it frees, so a plugin with two or more commands and an unknown size among
    them must say how much its tool's data uses.
    """
    # Given
    command_actions = [a for a in plugin.actions if isinstance(a.cleanup, RunCommand)]

    # When
    any_unknown = any(isinstance(a.size, SizeUnknown) for a in command_actions)

    # Then
    if len(command_actions) >= 2 and any_unknown:
        assert plugin.total_size is not None


@pytest.mark.parametrize("plugin", _PLUGINS, ids=lambda p: p.name)
def test_plugin_risky_actions_explain_cost(plugin: Plugin) -> None:
    """Verify every caution and dangerous action has help text and no em-dash."""
    # Given / When / Then
    for action in plugin.actions:
        if action.risk_level in {RiskLevel.CAUTION, RiskLevel.DANGEROUS}:
            assert action.description.strip()
        assert "\u2014" not in action.description


@pytest.mark.parametrize("plugin", _PLUGINS, ids=lambda p: p.name)
def test_plugin_volume_prune_is_dangerous(plugin: Plugin) -> None:
    """Verify no action prunes volumes below the dangerous risk level."""
    # Given / When / Then
    for action in plugin.actions:
        command = _command(action) or ()
        if "volume" in command and "prune" in command:
            assert action.risk_level is RiskLevel.DANGEROUS


_EXPECTED_ACTIONS: list[tuple[str, str, RiskLevel, tuple[str, ...] | None]] = [
    ("conda", "clean", RiskLevel.SAFE, ("conda", "clean", "--all", "--yes")),
    ("dnf", "packages", RiskLevel.SAFE, ("dnf", "clean", "packages")),
    (
        "flatpak",
        "unused-system",
        RiskLevel.SAFE,
        ("flatpak", "uninstall", "--unused", "-y", "--noninteractive", "--system"),
    ),
    (
        "flatpak",
        "unused-user",
        RiskLevel.SAFE,
        ("flatpak", "uninstall", "--unused", "-y", "--noninteractive", "--user"),
    ),
    ("pacman", "old-versions", RiskLevel.SAFE, ("paccache", "--remove")),
    (
        "pacman",
        "uninstalled",
        RiskLevel.SAFE,
        ("paccache", "--remove", "--uninstalled", "--keep", "0"),
    ),
    ("podman", "system", RiskLevel.CAUTION, ("podman", "system", "prune", "--all", "--force")),
    ("podman", "volumes", RiskLevel.DANGEROUS, ("podman", "volume", "prune", "--force")),
    ("thumbnails", "cache", RiskLevel.SAFE, None),
    ("apt", "autoclean", RiskLevel.SAFE, ("apt-get", "autoclean")),
    ("apt", "clean", RiskLevel.SAFE, ("apt-get", "clean")),
    ("brew", "cleanup", RiskLevel.SAFE, ("brew", "cleanup", "--prune=all")),
    ("brew", "autoremove", RiskLevel.CAUTION, ("brew", "autoremove")),
    ("docker", "builder", RiskLevel.SAFE, ("docker", "builder", "prune", "--all", "--force")),
    ("docker", "system", RiskLevel.CAUTION, ("docker", "system", "prune", "--all", "--force")),
    ("docker", "volumes", RiskLevel.DANGEROUS, ("docker", "volume", "prune", "--all", "--force")),
    ("go", "build-cache", RiskLevel.SAFE, ("go", "clean", "-cache")),
    ("go", "modcache", RiskLevel.CAUTION, ("go", "clean", "-modcache")),
    (
        "macports",
        "clean",
        RiskLevel.SAFE,
        ("/opt/local/bin/port", "-N", "clean", "--all", "installed"),
    ),
    ("maestro", "test-output", RiskLevel.CAUTION, None),
    ("macports", "reclaim", RiskLevel.CAUTION, ("/opt/local/bin/port", "-N", "reclaim")),
    ("mise", "cache-prune", RiskLevel.SAFE, ("mise", "cache", "prune")),
    ("mise", "prune", RiskLevel.CAUTION, ("mise", "prune", "--yes")),
    ("mise", "cache-clear", RiskLevel.DANGEROUS, ("mise", "cache", "clear")),
    ("npm", "verify", RiskLevel.SAFE, ("npm", "cache", "verify")),
    ("npm", "npx", RiskLevel.SAFE, ("npm", "cache", "npx", "rm", "--force")),
    ("npm", "clean", RiskLevel.DANGEROUS, ("npm", "cache", "clean", "--force")),
    ("pip", "cache", RiskLevel.CAUTION, None),
    ("poetry", "cache", RiskLevel.CAUTION, None),
    ("poetry", "virtualenvs", RiskLevel.CAUTION, None),
    ("pnpm", "metadata-prune", RiskLevel.SAFE, ("pnpm", "cache", "prune", "--yes")),
    ("pnpm", "prune", RiskLevel.SAFE, ("pnpm", "store", "prune")),
    ("pre-commit", "gc", RiskLevel.SAFE, ("pre-commit", "gc")),
    ("pre-commit", "clean", RiskLevel.DANGEROUS, ("pre-commit", "clean")),
    ("prek", "gc", RiskLevel.SAFE, ("prek", "cache", "gc")),
    ("prek", "clean", RiskLevel.DANGEROUS, ("prek", "cache", "clean")),
    ("uv", "prune", RiskLevel.SAFE, ("uv", "cache", "prune")),
    ("uv", "clean", RiskLevel.DANGEROUS, ("uv", "cache", "clean", "--force")),
    ("projects", "caches", RiskLevel.SAFE, None),
    ("projects", "dependencies", RiskLevel.CAUTION, None),
    ("projects", "builds", RiskLevel.CAUTION, None),
    ("projects", "custom", RiskLevel.CAUTION, None),
    ("rip2", "decompose", RiskLevel.CAUTION, ("rip", "--decompose", "--force")),
    ("ruby", "old-versions", RiskLevel.CAUTION, ("gem", "cleanup", "--no-user-install")),
    ("trash", "empty", RiskLevel.CAUTION, None),
    ("xcode", "derived-data", RiskLevel.SAFE, None),
    ("xcode", "device-support", RiskLevel.CAUTION, None),
    ("xcode", "archives", RiskLevel.DANGEROUS, None),
    ("yarn", "cache", RiskLevel.CAUTION, None),
    ("bun", "prune", RiskLevel.SAFE, ("bun", "pm", "cache", "prune", "--max-age", "30")),
    ("bun", "clean", RiskLevel.DANGEROUS, ("bun", "pm", "cache", "rm")),
    ("corepack", "cache", RiskLevel.CAUTION, ("corepack", "cache", "clean")),
    ("playwright", "old-browsers", RiskLevel.CAUTION, None),
    ("cocoapods", "cache", RiskLevel.CAUTION, ("pod", "cache", "clean", "--all")),
    ("swiftpm", "purge-cache", RiskLevel.CAUTION, ("swift", "package", "purge-cache")),
    ("swiftpm", "artifacts", RiskLevel.CAUTION, None),
    ("simulators", "unavailable", RiskLevel.SAFE, ("xcrun", "simctl", "delete", "unavailable")),
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
    ("plugin_name", "action_name", "risk_level", "command"),
    _EXPECTED_ACTIONS,
    ids=[f"{plugin}/{action}" for plugin, action, _, _ in _EXPECTED_ACTIONS],
)
def test_plugin_action_risk_level_and_command(
    plugin_name: str, action_name: str, risk_level: RiskLevel, command: tuple[str, ...] | None
) -> None:
    """Verify each built-in action keeps its reviewed risk level and command."""
    # Given
    plugin = discover_plugins()[plugin_name]

    # When
    action = next(a for a in plugin.actions if a.name == action_name)

    # Then
    assert action.risk_level is risk_level
    assert _command(action) == command


@pytest.mark.parametrize(
    ("plugin_name", "action_name"), [("brew", "autoremove"), ("macports", "reclaim")]
)
def test_uninstalling_actions_need_confirmation(plugin_name: str, action_name: str) -> None:
    """Verify actions that uninstall packages are never in the safe risk level."""
    # Given
    plugin = discover_plugins()[plugin_name]

    # When
    action = next(a for a in plugin.actions if a.name == action_name)

    # Then
    assert action.risk_level is not RiskLevel.SAFE


@pytest.mark.parametrize("plugin", _PLUGINS, ids=lambda p: p.name)
def test_plugin_action_description_starts_with_a_verb(plugin: Plugin) -> None:
    """Verify every action description starts with a verb, since prompts show it in place of the name."""
    # Given
    verbs = {"Remove", "Wipe", "Uninstall", "Garbage-collect"}

    # When
    first_words = {action.name: action.description.split(" ", 1)[0] for action in plugin.actions}

    # Then
    assert {name: word for name, word in first_words.items() if word not in verbs} == {}
