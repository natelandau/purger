"""Data model for purger plugins and their actions."""

import functools
import glob
import os
import re
import traceback
import types
from collections.abc import Callable, Hashable, Iterable, Iterator, Mapping, Sequence
from collections.abc import Set as AbstractSet
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import TypeIs, cast

_KEBAB_CASE = re.compile(r"[a-z0-9]+(-[a-z0-9]+)*")
_SNAKE_CASE = re.compile(r"[a-z][a-z0-9]*(_[a-z0-9]+)*")


class RiskLevel(StrEnum):
    """Risk level of an action, which decides how much confirmation it needs."""

    SAFE = "safe"
    CAUTION = "caution"
    DANGEROUS = "dangerous"


class OperatingSystem(StrEnum):
    """An operating system a plugin supports, named as `sys.platform` names it."""

    MACOS = "darwin"
    LINUX = "linux"


SettingValue = str | int | bool | tuple[str, ...]
type ValueType = type[str | int | bool] | types.GenericAlias


def _is_string(value: object) -> TypeIs[str]:
    return isinstance(value, str)


def _is_integer(value: object) -> TypeIs[int]:
    # bool subclasses int, but a setting declared `int` must not accept True.
    return isinstance(value, int) and not isinstance(value, bool)


def _is_boolean(value: object) -> TypeIs[bool]:
    return isinstance(value, bool)


def _is_string_tuple(value: object) -> TypeIs[tuple[str, ...]]:
    return isinstance(value, tuple) and all(isinstance(item, str) for item in value)


@dataclass(frozen=True)
class _ValueKind[T: SettingValue]:
    """How to recognize the stored value of one setting `value_type`, and how to name it."""

    label: str
    matches: Callable[[object], TypeIs[T]]


_STRING = _ValueKind(label="a string", matches=_is_string)
_INTEGER = _ValueKind(label="an integer", matches=_is_integer)
_BOOLEAN = _ValueKind(label="a boolean", matches=_is_boolean)
_STRING_LIST = _ValueKind(label="a list of strings", matches=_is_string_tuple)

type _AnyValueKind = (
    _ValueKind[str] | _ValueKind[int] | _ValueKind[bool] | _ValueKind[tuple[str, ...]]
)

# Keyed by equality, because `list[str]` builds a new alias on each use.
_VALUE_KINDS: Mapping[object, _AnyValueKind] = {
    str: _STRING,
    int: _INTEGER,
    bool: _BOOLEAN,
    list[str]: _STRING_LIST,
}


def _value_kind(value_type: ValueType) -> _AnyValueKind:
    """Return the value kind for a setting's `value_type`.

    Args:
        value_type: One of `str`, `int`, `bool`, or `list[str]`.

    Raises:
        ValueError: If the type is not one of those four.
    """
    kind = _VALUE_KINDS.get(value_type) if isinstance(value_type, Hashable) else None
    if kind is None:
        msg = f"setting value_type must be str, int, bool, or list[str], got {value_type!r}"
        raise ValueError(msg)
    return kind


@dataclass(frozen=True, kw_only=True)
class Setting:
    """A user-configurable option that a plugin declares.

    Declaring settings as data lets the config loader validate the config file
    and environment variables against the declared `value_type` and `validate`.
    Set `validate` to a function that raises ValueError to reject a value the
    type alone cannot rule out, such as a negative depth. Set `required` on a
    `str` or `list[str]` setting whose empty value means the plugin is not
    configured, so purger skips the plugin. A list default is stored as a tuple.

    Raises:
        ValueError: If the name is not snake_case, the default does not match
            the type, the description is blank, the default fails `validate`,
            or `required` is set on an `int` or `bool`.
    """

    name: str
    value_type: ValueType
    default: SettingValue | list[str]
    description: str
    validate: Callable[[SettingValue], None] | None = None
    required: bool = False

    def __post_init__(self) -> None:
        """Validate the setting's invariants."""
        if not _SNAKE_CASE.fullmatch(self.name):
            msg = f"setting name {self.name!r} must be snake_case"
            raise ValueError(msg)
        kind = _value_kind(self.value_type)
        if isinstance(self.default, list):
            object.__setattr__(self, "default", tuple(self.default))
        if not kind.matches(self.default):
            msg = (
                f"setting {self.name!r} default {self.default!r} does not match "
                f"value_type {self.value_type!r}"
            )
            raise ValueError(msg)
        if self.required and kind in {_INTEGER, _BOOLEAN}:
            msg = (
                f"setting {self.name!r} cannot be required: `required` applies only to "
                "str and list[str] settings, which can be empty"
            )
            raise ValueError(msg)
        if self.validate is not None:
            try:
                self.validate(cast("SettingValue", self.default))
            except ValueError as error:
                msg = (
                    f"setting {self.name!r} default {self.default!r} "
                    f"fails its own validate: {error}"
                )
                raise ValueError(msg) from error
        if not self.description.strip():
            msg = f"setting {self.name!r} needs a description"
            raise ValueError(msg)

    def accepts(self, value: object) -> bool:
        """Report whether a value has this setting's type, treating bool as not an int."""
        return _value_kind(self.value_type).matches(value)

    @property
    def expected_type_description(self) -> str:
        """Describe the type this setting accepts in words, for error messages."""
        return _value_kind(self.value_type).label


@dataclass(frozen=True, kw_only=True)
class PluginSettings:
    """One plugin's resolved settings for a run, with declared defaults filled in.

    Handing a plugin its settings, rather than letting it read shared state,
    keeps each run's values in one place and lets tests build them directly.
    """

    plugin: str
    values: Mapping[str, SettingValue]

    def value(self, name: str) -> SettingValue:
        """Return a setting's configured value, or its declared default.

        Args:
            name: The setting's name.

        Raises:
            KeyError: If the plugin does not declare the setting.
        """
        if name not in self.values:
            msg = f"plugin {self.plugin!r} declares no setting {name!r}"
            raise KeyError(msg)
        return self.values[name]

    def _value_of_kind[T: SettingValue](self, name: str, kind: _ValueKind[T]) -> T:
        """Return a setting's value after confirming it has the kind the caller expects.

        Raises:
            TypeError: If the setting's value is not of that kind.
        """
        value = self.value(name)
        if not kind.matches(value):
            msg = f"setting {name!r} of plugin {self.plugin!r} is not {kind.label}"
            raise TypeError(msg)
        return value

    def get_string(self, name: str) -> str:
        """Return a `str` setting's value.

        Raises:
            TypeError: If the setting is not a `str`.
        """
        return self._value_of_kind(name, _STRING)

    def get_integer(self, name: str) -> int:
        """Return an `int` setting's value.

        Raises:
            TypeError: If the setting is not an `int`.
        """
        return self._value_of_kind(name, _INTEGER)

    def get_boolean(self, name: str) -> bool:
        """Return a `bool` setting's value.

        Raises:
            TypeError: If the setting is not a `bool`.
        """
        return self._value_of_kind(name, _BOOLEAN)

    def get_string_list(self, name: str) -> tuple[str, ...]:
        """Return a `list[str]` setting's value as a tuple of strings.

        Raises:
            TypeError: If the setting is not a `list[str]`.
        """
        return self._value_of_kind(name, _STRING_LIST)


type _RunCacheKey = tuple[Callable[..., object], str, tuple[tuple[str, SettingValue], ...]]

_run_cache: dict[_RunCacheKey, object] = {}


@dataclass(frozen=True)
class _RunFailure:
    """The exception a `once_per_run` function raised, kept to raise again."""

    error: Exception
    traceback: types.TracebackType | None


def once_per_run[T](function: Callable[[PluginSettings], T]) -> Callable[[PluginSettings], T]:
    """Run a function that takes a plugin's settings once per purger command.

    Use it for a scan that several actions share, such as finding project
    folders: every `found_by` that calls the decorated function gets the same
    result, and so does the re-measure after a cleanup, so freed space compares
    the same paths. Results are kept apart per plugin and per settings value.

    Args:
        function: Receives the plugin's settings and returns the shared result,
            which must be a collection, such as a list or dict, and not a
            generator, since every caller reads it.

    Returns:
        Callable[[PluginSettings], T]: The function, returning its first result
            for the same settings until `reset_run_caches` runs. A function
            that raised raises the same exception again.
    """

    @functools.wraps(function)
    def run_once(settings: PluginSettings) -> T:
        key = (function, settings.plugin, tuple(sorted(settings.values.items())))
        if key not in _run_cache:
            try:
                result = function(settings)
                if isinstance(result, Iterator):
                    # The first caller would drain it and every later caller would find nothing.
                    msg = (
                        f"{run_once.__qualname__} must return a collection, not a one-pass iterator"
                    )
                    raise TypeError(msg)  # noqa: TRY301 - cached like any other failure
                _run_cache[key] = result
            except Exception as error:
                # Remembered too, so a failing scan is not repeated for every caller.
                # Clearing frame locals keeps the stack lines without pinning the scan's data.
                traceback.clear_frames(error.__traceback__)
                _run_cache[key] = _RunFailure(error=error, traceback=error.__traceback__)
                raise
        result = _run_cache[key]
        if isinstance(result, _RunFailure):
            # Reset to the stored traceback so each raise does not add frames to it.
            raise result.error.with_traceback(result.traceback)
        return cast("T", result)

    return run_once


# Holds the exceptions themselves so an id cannot be reused by a later exception.
_reported_errors: dict[int, Exception] = {}


def first_report_of(error: Exception) -> bool:
    """Return True the first time this exception is reported in a purger command.

    A cached `once_per_run` failure is raised again for every action that
    reads it, so callers use this to log it once.
    """
    if id(error) in _reported_errors:
        return False
    _reported_errors[id(error)] = error
    return True


def reset_run_caches() -> None:
    """Forget every `once_per_run` result, so the next purger command scans again."""
    _run_cache.clear()
    _reported_errors.clear()


@dataclass(frozen=True, kw_only=True)
class SizeBreakdownRow:
    """One labeled, sized row that breaks an action's size down for a person to inspect.

    Rows merge on `merge_key`, or on `label` when `merge_key` is None, so a
    plugin can show a short label while keeping rows with the same label apart.

    Raises:
        ValueError: If the label is blank.
    """

    label: str
    size_bytes: int | None
    item_names: tuple[str, ...] = ()
    paths: tuple[Path, ...] = ()
    merge_key: str | None = None

    def __post_init__(self) -> None:
        """Validate the row's invariants."""
        if not self.label.strip():
            msg = "size breakdown row needs a label"
            raise ValueError(msg)


def _require_strings(owner: str, field: str, values: tuple[object, ...]) -> None:
    """Reject a field that is not a tuple of strings, such as a tuple passed without `*`.

    Raises:
        ValueError: If the field is not a tuple or holds anything but strings.
    """
    if not isinstance(values, tuple) or not all(isinstance(value, str) for value in values):
        msg = f"{owner} {field} must be strings, got {values!r}"
        raise ValueError(msg)


type FoundBy = Callable[[PluginSettings], Iterable[Path]]


@dataclass(frozen=True, init=False)
class RunCommand:
    """Run the tool's own cleanup command, such as `uv cache prune`.

    Use it when the tool knows how to clean itself safely, so purger does not
    delete files the tool tracks.

    Attributes:
        argv: The command and its arguments.
        with_sudo: Whether the command needs root, so purger asks for it.
    """

    argv: tuple[str, ...]
    with_sudo: bool

    def __init__(self, *argv: str, with_sudo: bool = False) -> None:
        """Store the command, rejecting an empty one.

        Args:
            *argv: The command and its arguments.
            with_sudo: Whether the command needs root.
        """
        if not argv:
            msg = "RunCommand needs at least one argv word"
            raise ValueError(msg)
        _require_strings("RunCommand", "argv words", argv)
        object.__setattr__(self, "argv", argv)
        object.__setattr__(self, "with_sudo", with_sudo)


@dataclass(frozen=True, init=False)
class _PathSelection:
    """Files and folders named by glob patterns or found by a function, never both.

    Attributes:
        globs: Glob patterns, with `~` expanded.
        found_by: A function that receives the plugin's settings and returns paths.
    """

    globs: tuple[str, ...]
    found_by: FoundBy | None

    def __init__(self, *globs: str, found_by: FoundBy | None = None) -> None:
        """Store the selection, rejecting one that sets both or neither source.

        Args:
            *globs: Glob patterns, with `~` expanded.
            found_by: A function that receives the plugin's settings and returns paths.
        """
        if bool(globs) == (found_by is not None):
            msg = (
                f"{type(self).__name__} needs exactly one of globs or found_by, "
                f"got globs={globs!r} and found_by={found_by!r}"
            )
            raise ValueError(msg)
        _require_strings(type(self).__name__, "globs", globs)
        object.__setattr__(self, "globs", globs)
        object.__setattr__(self, "found_by", found_by)


@dataclass(frozen=True, init=False)
class DeletePaths(_PathSelection):
    """Delete files and folders, through purger's guarded deletion.

    Use positional glob patterns for fixed locations. Use `found_by` when the
    paths depend on settings or on what is on disk; it returns exact paths, so
    no escaping is needed.

    Attributes:
        globs: Glob patterns, with `~` expanded.
        found_by: A function that receives the plugin's settings and returns paths.
    """


@dataclass(frozen=True, init=False)
class SizeOfPaths(_PathSelection):
    """Measure the disk usage of files and folders.

    Use it when the data an action removes lives at known paths. The selection
    rules match `DeletePaths`.

    Attributes:
        globs: Glob patterns, with `~` expanded.
        found_by: A function that receives the plugin's settings and returns paths.
    """


@dataclass(frozen=True)
class SizeOfDirectoryPrintedBy:
    """Measure the folder that a command prints, such as `uv cache dir`.

    Use it when the tool, not purger, knows where its data lives.

    Attributes:
        command: The command that prints one absolute folder path.
        subpaths: Globs relative to that folder. Empty measures the whole folder.
    """

    command: tuple[str, ...]
    subpaths: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        """Reject an empty command."""
        if not self.command:
            msg = "SizeOfDirectoryPrintedBy needs a command"
            raise ValueError(msg)
        _require_strings("SizeOfDirectoryPrintedBy", "command", self.command)
        _require_strings("SizeOfDirectoryPrintedBy", "subpaths", self.subpaths)


@dataclass(frozen=True)
class SizeReportedByCommand:
    """Measure by reading a number out of a command's output.

    Use it when a tool reports its own reclaimable size, such as `docker system df`.

    Attributes:
        command: The command to run.
        read_size: Turns the command's stdout into bytes. Write `int` when the
            command prints a plain number.
    """

    command: tuple[str, ...]
    read_size: Callable[[str], int]

    def __post_init__(self) -> None:
        """Reject an empty command."""
        if not self.command:
            msg = "SizeReportedByCommand needs a command"
            raise ValueError(msg)
        _require_strings("SizeReportedByCommand", "command", self.command)


@dataclass(frozen=True)
class SizeComputedBy:
    """Measure with a function the plugin supplies.

    Use it when no path or command gives the size directly.

    Attributes:
        function: Takes no arguments and returns bytes, or None when unknown.
    """

    function: Callable[[], int | None]


@dataclass(frozen=True)
class SizeUnknown:
    """Declare that the size cannot be known before the cleanup runs.

    Use it so that a missing size is a stated decision rather than an omission.

    Attributes:
        reason: Why the size is unknowable, for readers of the code. It is not shown in output.
    """

    reason: str

    def __post_init__(self) -> None:
        """Reject a blank reason."""
        if not self.reason.strip():
            msg = "SizeUnknown needs a reason, got a blank one"
            raise ValueError(msg)


type SizeSource = (
    SizeOfPaths | SizeOfDirectoryPrintedBy | SizeReportedByCommand | SizeComputedBy | SizeUnknown
)
type Cleanup = RunCommand | DeletePaths


def at_least(minimum: int) -> Callable[[SettingValue], None]:
    """Build a setting `validate` function that rejects integers below a minimum.

    Args:
        minimum: The smallest accepted value.

    Returns:
        Callable[[SettingValue], None]: A function that raises ValueError for a non-int or smaller value.
    """

    def validate(value: SettingValue) -> None:
        """Raise ValueError unless the value is an int of at least the minimum."""
        if not isinstance(value, int) or isinstance(value, bool) or value < minimum:
            msg = f"must be at least {minimum}, got {value!r}"
            raise ValueError(msg)

    return validate


def _folder_in_environment(variable: str) -> str | None:
    """Return an environment variable's folder without a trailing slash, or None when unusable.

    A relative value counts as unset, since it would resolve against the working directory.
    """
    value = os.environ.get(variable, "").rstrip("/")
    return value if value.startswith(("/", "~")) else None


def folder_from_environment(variable: str, *, default: str) -> str:
    """Return a tool's folder from an environment variable, as a literal path.

    Use it when the code opens or lists the folder itself, such as reading an
    index file inside it. Use `directory_from_environment` instead to build a
    glob pattern.

    Args:
        variable: The environment variable that overrides the folder.
        default: The folder when the variable is unset, empty, or relative.

    Returns:
        str: The variable's absolute or `~` value without a trailing slash, or the default.
    """
    folder = _folder_in_environment(variable)
    return default if folder is None else folder


def directory_from_environment(variable: str, *, default: str) -> str:
    """Return a tool's folder from an environment variable, as a glob-safe pattern.

    Use it to build globs for tools that let users move their data folder,
    such as `CARGO_HOME` or `XDG_CACHE_HOME`.

    Args:
        variable: The environment variable that overrides the folder.
        default: The folder when the variable is unset, empty, or relative, such as `~/.cargo`.

    Returns:
        str: The folder without a trailing slash, with glob characters in the
            variable's value escaped so they match literally.
    """
    folder = _folder_in_environment(variable)
    return default if folder is None else glob.escape(folder)


def user_cache_folders(
    folder_name: str, *, macos_folder_name: str | None = None
) -> tuple[str, ...]:
    """List the folders where a tool can keep its per-user cache, as glob-safe patterns.

    Use it for a tool that follows the usual cache locations: `~/Library/Caches`
    on macOS, and `XDG_CACHE_HOME` or `~/.cache` on Linux. Every location is
    listed on both systems, so a cache written before a move still gets found.

    Args:
        folder_name: The tool's cache folder name, such as `pip`.
        macos_folder_name: The name inside `~/Library/Caches` when it differs,
            such as `Yarn`. Leave it out when the names match.

    Returns:
        tuple[str, ...]: The macOS folder, `~/.cache/<folder_name>`, and the
            `XDG_CACHE_HOME` folder, without duplicates.
    """
    xdg_cache = directory_from_environment("XDG_CACHE_HOME", default="~/.cache")
    macos_name = folder_name if macos_folder_name is None else macos_folder_name
    return tuple(
        dict.fromkeys(
            (
                f"~/Library/Caches/{macos_name}",
                f"~/.cache/{folder_name}",
                f"{xdg_cache}/{folder_name}",
            )
        )
    )


def program_matching(
    pattern: str, *, ignoring_subcommands: AbstractSet[str] = frozenset()
) -> Callable[[list[str]], bool]:
    """Build a `skip_while_running` check that matches processes running a named program.

    Only the executable and the first non-option argument after it are checked,
    since an interpreter launch (`node /usr/local/bin/npm`, `python3 -m pip`,
    `python3 -s /usr/bin/ansible-playbook`) puts the program after the
    interpreter and its flags.

    Args:
        pattern: A regex that must match the whole file name of the program.
        ignoring_subcommands: Subcommands that leave the tool's files alone, such
            as `exec` for npm, so a long-running one does not block cleanup.

    Returns:
        Callable[[list[str]], bool]: A check that receives a process argv.
    """
    program = re.compile(pattern)

    def runs_program(cmdline: list[str]) -> bool:
        """Report whether the process argv runs the program and not an ignored subcommand."""
        words = _argv_words(cmdline)
        if not words:
            return False
        script_at = next((i for i, arg in enumerate(words) if i and not arg.startswith("-")), None)
        candidates = [0] if script_at is None else [0, script_at]
        for index in candidates:
            if program.fullmatch(Path(words[index]).name):
                subcommand = next((a for a in words[index + 1 :] if not a.startswith("-")), None)
                return subcommand not in ignoring_subcommands
        return False

    return runs_program


def _argv_words(cmdline: list[str]) -> list[str]:
    """Return a process argv, splitting one that a rewritten process title joined.

    A program that sets its title, as npm does, leaves the whole command line
    in the first argument, followed only by empty ones.
    """
    if cmdline and not any(cmdline[1:]):
        return cmdline[0].split()
    return cmdline


PathSizes = Mapping[Path, int]
type SizeBreakdownFunction = Callable[[PathSizes, PluginSettings], Sequence[SizeBreakdownRow]]


@dataclass(frozen=True, kw_only=True)
class Action:
    """One cleanup operation that a plugin offers.

    Declaring actions as data lets the CLI size, confirm, and run every plugin
    the same way. `cleanup` says how the data is removed and `size` how much
    space it holds. A `RunCommand` cleanup needs a `size`, which may be
    `SizeUnknown`. A `DeletePaths` cleanup takes its size from its paths, so
    it has no `size`. Set `adds_to_plugin_total_size` when the plugin's
    `total_size` does not measure this action's data. Set `explain_failure` to
    turn a failed command's stderr into a hint, and `size_breakdown` to split
    the size into rows for `-v` and JSON.

    Raises:
        ValueError: If the name is not kebab-case, the description is blank,
            the size does not suit the cleanup, or `explain_failure` is set on
            a `DeletePaths` cleanup.
    """

    name: str
    risk_level: RiskLevel
    description: str
    cleanup: Cleanup
    size: SizeSource | None = None
    adds_to_plugin_total_size: bool = False
    explain_failure: Callable[[str], str | None] | None = None
    size_breakdown: SizeBreakdownFunction | None = None

    def __post_init__(self) -> None:
        """Reject a bad name, a blank description, and combinations that cannot work, naming the fix."""
        label = f"action {self.name!r}"
        if not _KEBAB_CASE.fullmatch(self.name):
            msg = f"{label} name must be kebab-case"
            raise ValueError(msg)
        if not self.description.strip():
            msg = f"{label} needs a description=; caution and dangerous prompts and the JSON output (key `help`) show it"
            raise ValueError(msg)
        if isinstance(self.cleanup, RunCommand) and self.size is None:
            msg = (
                f"{label} runs a command, so it needs size=; write "
                "size=SizeUnknown(reason=...) when the size cannot be known before the cleanup runs"
            )
            raise ValueError(msg)
        if isinstance(self.cleanup, DeletePaths):
            if self.size is not None:
                msg = f"{label} deletes paths, which are its size; remove size="
                raise ValueError(msg)
            if self.explain_failure is not None:
                msg = f"{label} deletes paths, and explain_failure= applies only to RunCommand; remove it"
                raise ValueError(msg)

    @property
    def cleanup_command(self) -> RunCommand | None:
        """Return the command the cleanup runs, or None when purger deletes paths itself."""
        return self.cleanup if isinstance(self.cleanup, RunCommand) else None

    @property
    def runs_with_sudo(self) -> bool:
        """Report whether the cleanup runs a command as root."""
        command = self.cleanup_command
        return command is not None and command.with_sudo


@dataclass(frozen=True, kw_only=True)
class Plugin:
    """A named group of cleanup actions for one tool or ecosystem.

    Grouping actions under a plugin lets users target a whole tool at once and
    lets purger skip plugins that do not apply to the host.

    Set `total_size` to how much disk space the tool's data uses. Without it,
    the plugin's size is the sum of its actions' sizes, which is right only
    when no two actions remove the same data.

    Limit where the plugin runs with `operating_systems`, `requires_on_path`
    (a program that must be installed), and `skip_unless` (a fast check that
    the tool is usable). Set `skip_while_running` to a check on a process argv
    that matches the tool; the plugin is skipped while one runs, since a live
    run may be using the files a cleanup would remove. purger's own
    subprocesses never count.

    Set `settings` to the options users may configure. Set
    `skip_unless_configured` to a check on those settings that returns False
    while the plugin is not set up, which skips it as not configured. A
    `required` setting that is empty skips it too.

    Raises:
        ValueError: If the name is not kebab-case, there are no actions,
            action or setting names repeat, `operating_systems` is empty or
            repeats, or an action adds to a total size the plugin does not set.
    """

    name: str
    description: str
    actions: tuple[Action, ...]
    operating_systems: tuple[OperatingSystem, ...] = (OperatingSystem.MACOS, OperatingSystem.LINUX)
    requires_on_path: str | None = None
    skip_unless: Callable[[], bool] | None = None
    skip_while_running: Callable[[list[str]], bool] | None = None
    skip_unless_configured: Callable[[PluginSettings], bool] | None = None
    settings: tuple[Setting, ...] = ()
    total_size: SizeSource | None = None

    def __post_init__(self) -> None:
        """Validate the plugin's invariants."""
        if not _KEBAB_CASE.fullmatch(self.name):
            msg = f"plugin name {self.name!r} must be kebab-case"
            raise ValueError(msg)
        if not self.description.strip():
            msg = f"plugin {self.name!r} needs a description=, which the info table shows"
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
        if not self.operating_systems or len(set(self.operating_systems)) != len(
            self.operating_systems
        ):
            msg = f"plugin {self.name!r} operating_systems must be non-empty with no repeats"
            raise ValueError(msg)
        for action in self.actions:
            if action.adds_to_plugin_total_size and self.total_size is None:
                msg = (
                    f"action {action.name!r} sets adds_to_plugin_total_size=True, so plugin "
                    f"{self.name!r} needs total_size=; or remove adds_to_plugin_total_size="
                )
                raise ValueError(msg)
