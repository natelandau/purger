"""Shared test factories."""

from collections.abc import Callable
from typing import Any

import pytest

from purger.plugin import Action, Plugin, Tier


def make_action(**overrides: Any) -> Action:
    """Build a valid Action, letting tests override only the fields they care about.

    The overrides are typed Any because they are forwarded to the dataclass, which
    validates each field itself.
    """
    fields: dict[str, Any] = {
        "name": "a",
        "tier": Tier.SAFE,
        "help": "Remove things.",
        "command": ("true",),
    }
    fields.update(overrides)
    return Action(**fields)


def make_plugin(**overrides: Any) -> Plugin:
    """Build a valid Plugin, letting tests override only the fields they care about."""
    fields: dict[str, Any] = {"name": "p", "description": "d", "actions": (make_action(),)}
    fields.update(overrides)
    return Plugin(**fields)


def table_row(text: str, name: str) -> list[str]:
    """Return the stripped cells of the first rendered table row that names a plugin."""
    line = next(line for line in text.splitlines() if f" {name} " in line)
    return [cell.strip() for cell in line.split("\u2502")[1:-1]]


@pytest.fixture(name="make_action")
def make_action_fixture() -> Callable[..., Action]:
    """Provide the make_action factory as a fixture."""
    return make_action


@pytest.fixture(name="make_plugin")
def make_plugin_fixture() -> Callable[..., Plugin]:
    """Provide the make_plugin factory as a fixture."""
    return make_plugin
