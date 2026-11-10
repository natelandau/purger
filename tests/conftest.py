"""Shared test factories."""

import importlib
import pkgutil
import shutil
import sys
from collections.abc import Callable, Sequence
from functools import cache
from types import ModuleType
from typing import Any

import pytest
from rich.table import Table
from rich.text import Text

import purger
from purger.plugin import Action, Plugin, Tier

_SPAWNERS = ("run_command", "run_interactive")
_HARMLESS = frozenset({"echo", "true", "false"})


@cache
def _import_all() -> None:
    """Import every purger module once per session, plugins included."""
    for info in pkgutil.walk_packages(purger.__path__, prefix="purger."):
        importlib.import_module(info.name)


def _purger_modules() -> list[ModuleType]:
    """Return every imported purger module."""
    _import_all()
    return [module for name, module in sys.modules.items() if name.startswith("purger.")]


@pytest.fixture(autouse=True)
def _block_real_commands(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make every command purger could spawn raise unless the test mocks it.

    A cleanup command that escapes a test would prune this machine's real caches.
    Each module binds `run_command` at import, so each binding is replaced. A test's
    own `mocker.patch` of the same name layers on top and wins. Commands that
    cannot change anything still run, so tests can exercise the real spawn path.
    """

    def guard(real: Callable[..., object]) -> Callable[..., object]:
        def run(argv: Sequence[str], *args: object, **kwargs: object) -> object:
            # `run_interactive(..., sudo=True)` prepends sudo itself.
            program = "sudo" if kwargs.get("sudo") else argv[0]
            if program in _HARMLESS or shutil.which(program) is None:
                return real(argv, *args, **kwargs)
            msg = f"test ran a real command without mocking it: {argv!r}"
            raise AssertionError(msg)

        return run

    for module in _purger_modules():
        for name in _SPAWNERS:
            if hasattr(module, name):
                monkeypatch.setattr(module, name, guard(getattr(module, name)))


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
