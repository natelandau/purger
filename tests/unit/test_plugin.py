"""Tests for the plugin data model."""

from collections.abc import Callable
from typing import Any

import pytest

from purger.plugin import Action, Detail, Plugin, Setting, Tier
from tests.conftest import make_action, make_plugin


def test_action_requires_command_paths_or_prepared() -> None:
    """Verify an action with no command, no paths, and not prepared is rejected."""
    # Given / When / Then
    with pytest.raises(ValueError, match="command, paths, or prepared"):
        Action(name="x", tier=Tier.SAFE, help="h")


def test_prepared_action_needs_no_command_or_paths() -> None:
    """Verify an action whose paths come from the prepare hook is valid on its own."""
    # Given / When
    action = Action(name="x", tier=Tier.SAFE, help="h", prepared=True)

    # Then
    assert action.prepared is True


def test_prepared_action_sudo_requires_command() -> None:
    """Verify a prepared action still needs a command to set needs_sudo."""
    # Given / When / Then
    with pytest.raises(ValueError, match="needs_sudo"):
        Action(name="x", tier=Tier.SAFE, help="h", prepared=True, needs_sudo=True)


def test_action_rejects_both_paths_and_prepared() -> None:
    """Verify an action cannot take its paths from both globs and the prepare hook."""
    # Given / When / Then
    with pytest.raises(ValueError, match="sets both paths and prepared"):
        Action(name="x", tier=Tier.SAFE, help="h", paths=("~/a",), prepared=True)


def test_prepared_action_may_have_a_command() -> None:
    """Verify a prepared action can run a command whose removals its paths list."""
    # Given / When
    action = Action(name="x", tier=Tier.SAFE, help="h", command=("true",), prepared=True)

    # Then
    assert action.command == ("true",)


def test_plugin_with_prepared_action_requires_prepare_hook() -> None:
    """Verify a plugin rejects a prepared action when it has no prepare hook to supply its paths."""
    # Given
    action = Action(name="x", tier=Tier.SAFE, help="h", prepared=True)

    # When / Then
    with pytest.raises(ValueError, match="no prepare hook"):
        make_plugin(actions=(action,))
    assert make_plugin(actions=(action,), prepare=lambda _: {}).actions == (action,)


def test_action_sudo_requires_command() -> None:
    """Verify needs_sudo is rejected on a paths-only action."""
    # Given / When / Then
    with pytest.raises(ValueError, match="needs_sudo"):
        Action(name="x", tier=Tier.SAFE, help="h", paths=("~/a",), needs_sudo=True)


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


@pytest.mark.parametrize("help_text", ["", "   "])
def test_action_requires_help(help_text: str) -> None:
    """Verify every action explains itself, since prompts show the help in place of the name."""
    # Given / When / Then
    with pytest.raises(ValueError, match="needs help text"):
        make_action(help=help_text)


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


def _setting(**overrides: Any) -> Setting:
    """Build a valid Setting, overriding only the fields a test cares about."""
    fields: dict[str, Any] = {"name": "max_depth", "kind": "int", "default": 3, "help": "Depth."}
    fields.update(overrides)
    return Setting(**fields)


def _reject_every_value(_: object) -> None:
    """Act as a setting check that rejects any value."""
    msg = "nope"
    raise ValueError(msg)


@pytest.mark.parametrize("name", ["Max", "max-depth", "_x", "", "a__b"])
def test_setting_rejects_non_snake_name(name: str) -> None:
    """Verify setting names must be snake_case."""
    # Given / When / Then
    with pytest.raises(ValueError, match="snake_case"):
        _setting(name=name)


@pytest.mark.parametrize(
    ("kind", "default"),
    [("int", True), ("int", "3"), ("bool", 1), ("str", 1), ("str_list", ["a"]), ("str_list", (1,))],
)
def test_setting_rejects_default_of_wrong_kind(kind: str, default: object) -> None:
    """Verify a default must match the setting's kind, and a bool is not an int."""
    # Given / When / Then
    with pytest.raises(ValueError, match="does not match kind"):
        _setting(kind=kind, default=default)


def test_setting_rejects_empty_help() -> None:
    """Verify a setting needs help text."""
    # Given / When / Then
    with pytest.raises(ValueError, match="help"):
        _setting(help="  ")


def test_plugin_rejects_duplicate_setting_names() -> None:
    """Verify a plugin cannot declare the same setting twice."""
    # Given / When / Then
    with pytest.raises(ValueError, match="duplicate setting"):
        make_plugin(settings=(_setting(), _setting()))


def test_setting_rejects_default_failing_its_check() -> None:
    """Verify a default that its own check rejects fails at declaration."""
    # Given / When / Then
    with pytest.raises(ValueError, match="fails its own check"):
        _setting(check=_reject_every_value)


def test_detail_rejects_blank_label() -> None:
    """Verify a detail row needs a label to show."""
    # Given / When / Then
    with pytest.raises(ValueError, match="label"):
        Detail(label=" ", size_bytes=1)
