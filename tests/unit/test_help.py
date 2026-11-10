"""Tests for the generated help text."""

import contextlib
import io
import sys

import pytest
from rich.console import Console

from purger.cli import app, attach_plugin_catalog
from purger.help_text import plugin_catalog
from purger.registry import discover_plugins
from purger.reports import Status
from tests.conftest import make_action, make_plugin


def _help(*args: str) -> str:
    """Render help for a command into plain text wide enough that nothing wraps."""
    buffer = io.StringIO()
    console = Console(file=buffer, width=300, color_system=None)
    attach_plugin_catalog()
    with contextlib.suppress(SystemExit):
        app.meta([*args, "--help"], console=console, result_action="return_value")
    return buffer.getvalue()


@pytest.mark.parametrize("command", [(), ("info",), ("clean",)])
def test_help_lists_every_plugin_on_one_line(command: tuple[str, ...]) -> None:
    """Verify each help page lists every plugin on one line without its actions."""
    # Given
    plugins = discover_plugins()

    # When
    text = _help(*command)

    # Then
    lines = text.splitlines()
    for plugin in plugins.values():
        assert any(f"{plugin.name}: {plugin.description}" in line for line in lines)
        for action in plugin.actions:
            if action.help:
                assert action.help not in text


def test_top_level_help_has_agent_notes() -> None:
    """Verify top-level help explains tiers, JSON use, and the root refusal."""
    # Given / When rendering help with the registered plugins
    text = _help()

    # Then
    for needle in ("--json", "safe", "caution", "dangerous", "root", "info", "clean"):
        assert needle in text


def test_command_help_has_exit_codes_and_examples() -> None:
    """Verify clean help lists exit codes, examples, flags, and every JSON status value."""
    # Given / When rendering help with the registered plugins
    text = _help("clean")

    # Then
    for needle in ("130", "--dry-run", "purger clean --json --yes", "--dangerous", "--yes"):
        assert needle in text
    for status in Status:
        assert status.value in text


def test_info_help_has_exit_codes_and_examples() -> None:
    """Verify info help lists exit codes, an example, and the JSON shape."""
    # Given / When rendering help with the registered plugins
    text = _help("info")

    # Then
    for needle in ("purger info --json", "schema_version", "total_bytes", "skipped", "2"):
        assert needle in text


def test_plugin_catalog_marks_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify a plugin for another platform is listed and marked as unavailable."""
    # Given
    monkeypatch.setattr(sys, "platform", "darwin")
    linux_only = make_plugin(
        name="linux-only",
        description="Linux cache",
        platforms=frozenset({"linux"}),
        actions=(make_action(name="wipe", help="Wipe it"),),
    )
    everywhere = make_plugin(name="everywhere", description="Everywhere cache")

    # When
    text = plugin_catalog([linux_only, everywhere])

    # Then
    linux_line = next(line for line in text.splitlines() if "linux-only" in line)
    everywhere_line = next(line for line in text.splitlines() if "everywhere" in line)
    assert "not available on this machine" in linux_line
    assert "not available on this machine" not in everywhere_line
    assert "wipe" not in text
    assert "Wipe it" not in text


@pytest.mark.parametrize("command", [(), ("info",), ("clean",)])
def test_help_hides_boolean_defaults(command: tuple[str, ...]) -> None:
    """Verify help does not print `[default: False]` next to flags."""
    # Given / When rendering help with the registered plugins
    text = _help(*command)

    # Then
    assert "default: False" not in text


def test_top_level_help_lists_verbose() -> None:
    """Verify the global -v/--verbose flag appears in top-level help."""
    # Given / When rendering help with the registered plugins
    text = _help()

    # Then
    assert "--verbose" in text


@pytest.mark.parametrize("command", [("info",), ("clean",)])
def test_command_help_lists_json_error_codes(command: tuple[str, ...]) -> None:
    """Verify each command's help lists every JSON error code, including usage."""
    # Given / When rendering help with the registered plugins
    text = _help(*command)

    # Then
    for code in ("usage", "unknown_plugin", "running_as_root", "interrupted"):
        assert f"`{code}`" in text or code in text


def test_top_level_help_qualifies_the_no_data_loss_promise() -> None:
    """Verify the promise not to delete user data is tied to --dangerous and confirmation."""
    # Given / When rendering help with the registered plugins
    text = _help()

    # Then
    assert "Without --dangerous and your explicit confirmation" in text
