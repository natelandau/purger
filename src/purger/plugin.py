"""Data model for purger plugins and their actions."""

import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from pathlib import Path

    from purger.config import PluginSettings

_KEBAB_CASE = re.compile(r"[a-z0-9]+(-[a-z0-9]+)*")
_SNAKE_CASE = re.compile(r"[a-z][a-z0-9]*(_[a-z0-9]+)*")
_PLATFORMS = frozenset({"darwin", "linux"})


class Tier(StrEnum):
    """Risk level of an action, which decides how much confirmation it needs."""

    SAFE = "safe"
    CAUTION = "caution"
    DANGEROUS = "dangerous"


SettingKind = Literal["str", "int", "bool", "str_list"]
SettingValue = str | int | bool | tuple[str, ...]


def matches_kind(kind: SettingKind, value: object) -> bool:
    """Report whether a value has the type a setting kind names, treating bool as not an int.

    Args:
        kind: The setting kind to test against.
        value: The candidate value, where a `str_list` is a tuple of strings.
    """
    match kind:
        case "str":
            return isinstance(value, str)
        case "int":
            return isinstance(value, int) and not isinstance(value, bool)
        case "bool":
            return isinstance(value, bool)
        case "str_list":
            return isinstance(value, tuple) and all(isinstance(item, str) for item in value)


@dataclass(frozen=True, kw_only=True)
class Setting:
    """A user-configurable option that a plugin declares.

    Declaring settings as data lets the config loader validate the config file
    and environment variables against the declared kind and check. Set
    `check` to a function that raises ValueError to reject a value the kind
    alone cannot rule out, such as a negative depth.

    Raises:
        ValueError: If the name is not snake_case, the default does not match
            the kind, help is empty, or the default fails `check`.
    """

    name: str
    kind: SettingKind
    default: SettingValue
    help: str
    check: Callable[[SettingValue], None] | None = None

    def __post_init__(self) -> None:
        """Validate the setting's invariants."""
        if not _SNAKE_CASE.fullmatch(self.name):
            msg = f"setting name {self.name!r} must be snake_case"
            raise ValueError(msg)
        if not matches_kind(self.kind, self.default):
            msg = (
                f"setting {self.name!r} default {self.default!r} does not match kind {self.kind!r}"
            )
            raise ValueError(msg)
        if self.check is not None:
            try:
                self.check(self.default)
            except ValueError as error:
                msg = f"setting {self.name!r} default {self.default!r} fails its own check: {error}"
                raise ValueError(msg) from error
        if not self.help.strip():
            msg = f"setting {self.name!r} needs help text"
            raise ValueError(msg)


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
    Set `prepared` when the action's paths come from the plugin's `prepare`
    hook, which returns the literal paths for each prepared action by name.

    Raises:
        ValueError: If the name is not kebab-case, none of command, paths, or
            prepared is set, both paths and prepared are set, needs_sudo is set
            without a command, or help is empty.
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
    prepared: bool = False

    def __post_init__(self) -> None:
        """Validate the action's invariants."""
        if not _KEBAB_CASE.fullmatch(self.name):
            msg = f"action name {self.name!r} must be kebab-case"
            raise ValueError(msg)
        if self.command is None and not self.paths and not self.prepared:
            msg = f"action {self.name!r} needs a command, paths, or prepared"
            raise ValueError(msg)
        if self.prepared and self.paths:
            msg = f"action {self.name!r} sets both paths and prepared"
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
    Set `busy_process` to a check that matches a process argv running the
    tool; the plugin is skipped while one runs, since a live run may be using
    the files a cleanup would remove. purger's own subprocesses never count.
    Set `settings` to the options users may configure for this plugin.
    Set `prepare` to a hook that receives the plugin's settings for the run
    and returns the paths of each `prepared` action by action name, or None
    while a required setting is missing, which skips the plugin. It runs once
    before the plugin is measured, so actions can share one scan, and
    re-measuring after a cleanup reuses what it returned.

    Raises:
        ValueError: If the name is not kebab-case, there are no actions,
            action or setting names repeat, a platform is not darwin or linux,
            or an action is prepared but the plugin has no prepare hook.
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
    busy_process: Callable[[list[str]], bool] | None = None
    settings: tuple[Setting, ...] = ()
    prepare: "Callable[[PluginSettings], Mapping[str, tuple[Path, ...]] | None] | None" = None

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
        setting_names = [item.name for item in self.settings]
        if len(set(setting_names)) != len(setting_names):
            msg = f"plugin {self.name!r} has duplicate setting names"
            raise ValueError(msg)
        unknown = self.platforms - _PLATFORMS
        if unknown:
            msg = f"plugin {self.name!r} has unknown platform(s): {sorted(unknown)}"
            raise ValueError(msg)
        if self.prepare is None and any(action.prepared for action in self.actions):
            msg = f"plugin {self.name!r} has prepared actions but no prepare hook"
            raise ValueError(msg)

    @property
    def has_own_footprint(self) -> bool:
        """Report whether the plugin sizes itself rather than summing its actions."""
        return self.size is not None or self.size_cmd is not None or bool(self.paths)
