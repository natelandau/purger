"""Data model for purger plugins and their actions."""

import re
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum

_KEBAB_CASE = re.compile(r"[a-z0-9]+(-[a-z0-9]+)*")
_PLATFORMS = frozenset({"darwin", "linux"})


class Tier(StrEnum):
    """Risk level of an action, which decides how much confirmation it needs."""

    SAFE = "safe"
    CAUTION = "caution"
    DANGEROUS = "dangerous"


@dataclass(frozen=True, kw_only=True)
class Action:
    """One cleanup operation, run as a command or as a set of paths to delete.

    Declaring actions as data lets the CLI size, confirm, and run every
    plugin the same way. When both `command` and `paths` are set, only the
    command runs and the paths are used to size and list what it removes.
    Set `paths` to a function when the globs depend on what is on disk, so
    they are worked out each time the action is measured rather than at import.
    Set `outside_footprint` when the plugin's own footprint does not cover this
    action's data, so its size is added to the footprint whenever the action is
    measured. Set `explain_failure` to turn a failed command's stderr into a
    hint about the likely cause; it returns None when it has nothing to add.

    Raises:
        ValueError: If the name is not kebab-case, neither command nor paths
            is set, needs_sudo is set without a command, or help is empty.
    """

    name: str
    tier: Tier
    help: str
    command: tuple[str, ...] | None = None
    paths: tuple[str, ...] | Callable[[], tuple[str, ...]] = ()
    size_cmd: tuple[str, ...] | None = None
    parse_size: Callable[[str], int] | None = None
    size: Callable[[], int | None] | None = None
    needs_sudo: bool = False
    outside_footprint: bool = False
    explain_failure: Callable[[str], str | None] | None = None

    def __post_init__(self) -> None:
        """Validate the action's invariants."""
        if not _KEBAB_CASE.fullmatch(self.name):
            msg = f"action name {self.name!r} must be kebab-case"
            raise ValueError(msg)
        if self.command is None and not self.paths:
            msg = f"action {self.name!r} needs a command or paths"
            raise ValueError(msg)
        if self.needs_sudo and self.command is None:
            msg = f"action {self.name!r} sets needs_sudo without a command"
            raise ValueError(msg)
        if not self.help.strip():
            msg = f"action {self.name!r} needs help text, which confirmation prompts show"
            raise ValueError(msg)


@dataclass(frozen=True, kw_only=True)
class Plugin:
    """A named group of cleanup actions for one tool or ecosystem.

    Grouping actions under a plugin lets users target a whole tool at once
    and lets purger skip plugins that do not apply to the host. A plugin
    sizes itself with the first of `size`, `size_cmd`, or `paths` that is
    set; with none, its footprint is the sum of its actions' sizes.

    Raises:
        ValueError: If the name is not kebab-case, there are no actions,
            action names repeat, or a platform is not darwin or linux.
    """

    name: str
    description: str
    actions: tuple[Action, ...]
    platforms: frozenset[str] = frozenset({"darwin", "linux"})
    requires: str | None = None
    paths: tuple[str, ...] = ()
    size_cmd: tuple[str, ...] | None = None
    parse_size: Callable[[str], int] | None = None
    size: Callable[[], int | None] | None = None
    is_available: Callable[[], bool] | None = None

    def __post_init__(self) -> None:
        """Validate the plugin's invariants."""
        if not _KEBAB_CASE.fullmatch(self.name):
            msg = f"plugin name {self.name!r} must be kebab-case"
            raise ValueError(msg)
        if not self.actions:
            msg = f"plugin {self.name!r} needs at least one action"
            raise ValueError(msg)
        names = [action.name for action in self.actions]
        if len(set(names)) != len(names):
            msg = f"plugin {self.name!r} has duplicate action names"
            raise ValueError(msg)
        unknown = self.platforms - _PLATFORMS
        if unknown:
            msg = f"plugin {self.name!r} has unknown platform(s): {sorted(unknown)}"
            raise ValueError(msg)

    @property
    def has_own_footprint(self) -> bool:
        """Report whether the plugin sizes itself rather than summing its actions."""
        return self.size is not None or self.size_cmd is not None or bool(self.paths)
