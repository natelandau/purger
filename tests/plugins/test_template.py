"""Tests that keep the plugin template complete and out of the registry."""

import dataclasses
import inspect
import re

from purger.plugin import Action, Plugin
from purger.plugins import _template
from purger.registry import discover_plugins


def _template_sections() -> tuple[str, str]:
    """Split the template's PLUGIN source into its plugin fields and its action fields."""
    _, _, plugin_source = inspect.getsource(_template).partition("PLUGIN = Plugin(")
    plugin_part, marker, action_part = plugin_source.partition("actions=(")
    return plugin_part + marker, action_part


def _undocumented(cls: type[Plugin | Action], source: str) -> list[str]:
    """List the dataclass fields that the source neither sets nor names in a comment."""
    return [
        field.name
        for field in dataclasses.fields(cls)
        if not re.search(rf"\b{field.name}=", source)
    ]


def test_template_documents_every_plugin_field() -> None:
    """Verify the template sets or names every Plugin field, so new fields reach it."""
    # Given
    plugin_source, _ = _template_sections()

    # When
    missing = _undocumented(Plugin, plugin_source)

    # Then
    assert missing == []


def test_template_documents_every_action_field() -> None:
    """Verify the template sets or names every Action field, so new fields reach it."""
    # Given
    _, action_source = _template_sections()

    # When
    missing = _undocumented(Action, action_source)

    # Then
    assert missing == []


def test_template_is_never_registered() -> None:
    """Verify discovery skips the template, so its example tool never runs."""
    # Given
    template_name = _template.PLUGIN.name

    # When
    registry = discover_plugins()

    # Then
    assert template_name not in registry
