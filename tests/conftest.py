"""Shared test factories."""

from collections.abc import Callable
from typing import Any

import pytest
from rich.table import Table
from rich.text import Text

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


def table_row(table: Table, name: str) -> list[str]:
    """Return the plain cell text of the row whose first cell is name, or the footer for "Total".

    Reads the table's data rather than its rendering, so the tests survive style changes.
    """
    if name == "Total":
        cells = [column.footer for column in table.columns]
    else:
        rows = zip(*(list(column.cells) for column in table.columns), strict=True)
        cells = next(row for row in rows if Text.from_markup(str(row[0])).plain == name)
    return [Text.from_markup(str(cell)).plain for cell in cells]


@pytest.fixture(name="make_action")
def make_action_fixture() -> Callable[..., Action]:
    """Provide the make_action factory as a fixture."""
    return make_action


@pytest.fixture(name="make_plugin")
def make_plugin_fixture() -> Callable[..., Plugin]:
    """Provide the make_plugin factory as a fixture."""
    return make_plugin
