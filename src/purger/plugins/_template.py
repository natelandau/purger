"""Template for a new plugin. Copy it to `<plugin_name>.py` and edit the copy.

purger never registers a module whose name starts with an underscore, so this
file is reference only. It sets or names every `Plugin` and `Action` field,
and a test fails when a field is added without documenting it here. In the
copy, delete the fields and actions the tool does not need, and replace this
docstring with one line that names the tool.
"""

from functools import partial
from pathlib import Path

from purger.config import PluginSettings
from purger.plugin import Action, Plugin, Setting, SettingValue, Tier
from purger.plugins._shared import program_matcher
from purger.runner import command_succeeds
from purger.sizing import printed_dir_usage

_CACHE_DIR_CMD = ("example-tool", "cache", "dir")


def _is_available() -> bool:
    """Report whether the tool can run cleanups now, such as a daemon that answers.

    Returns:
        bool: True when the tool is usable.
    """
    return command_succeeds(["example-tool", "status"], timeout=10)


def _prepare(settings: PluginSettings) -> dict[str, tuple[Path, ...]] | None:
    """Scan once so every `prepared` action shares the result.

    Args:
        settings: The plugin's settings for this run.

    Returns:
        dict[str, tuple[Path, ...]] | None: The paths of each prepared action
            by action name, or None while a required setting is missing.
    """
    if not settings.str_list("search_paths"):
        return None
    return {}


def _check_positive(value: SettingValue) -> None:
    """Reject a setting value that is not a positive number.

    Args:
        value: The configured value.

    Raises:
        ValueError: If the value is not above zero.
    """
    if not isinstance(value, int) or value < 1:
        msg = "must be at least 1"
        raise ValueError(msg)


def _explain_failure(stderr: str) -> str | None:
    """Name the likely cause of a failure that the tool's own error hides.

    Args:
        stderr: The failed command's stderr.

    Returns:
        str | None: A hint, or None when there is nothing to add.
    """
    if "EBUSY" not in stderr:
        return None
    return "Another example-tool process is using the cache. Stop it, then run this action again."


PLUGIN = Plugin(
    # Kebab-case. The file name is this name with underscores for hyphens.
    name="example-tool",
    # One line, shown in the `info` table and the help page.
    description="Example tool package cache and logs",
    # Leave out to run on both darwin and linux.
    platforms=frozenset({"darwin", "linux"}),
    # A binary that must be on PATH. Without it, the plugin is skipped.
    requires="example-tool",
    # A check that runs after `requires`, for a tool that is installed but not
    # usable, such as a stopped daemon. It runs on every `info` and `clean`, so
    # keep it fast. A hook that raises counts as unavailable.
    is_available=_is_available,
    # Receives each running process's argv and returns True for the tool. While
    # a match runs, the plugin is skipped, since that run may be using the files
    # a cleanup would remove. purger's own subprocesses never count.
    # `program_matcher` matches the program's file name, also when an
    # interpreter such as node or python launches it.
    busy_process=program_matcher(r"example-tool"),
    # The footprint is the size in the `info` table. purger uses the first of
    # `size`, `size_cmd`, or `paths` that is set. With none, the footprint is
    # the sum of the action sizes, which is right only when no two actions
    # remove the same data.
    size_cmd=_CACHE_DIR_CMD,
    # Turns the `size_cmd` output into bytes. When the tool prints its cache
    # directory, `printed_dir_usage` measures the directory it really uses.
    parse_size=printed_dir_usage,
    # Options users set in `[plugins.example-tool]` of the config file, or with
    # `PURGER_EXAMPLE_TOOL__<NAME>`. Kinds: "str", "int", "bool", "str_list".
    # The default must match the kind. `check` may raise ValueError to reject a
    # value. Hooks that take settings read them from the `PluginSettings`
    # they receive, such as `settings.integer("name")`.
    settings=(
        Setting(
            # Snake_case and unique within the plugin.
            name="max_age_days",
            kind="int",
            default=30,
            help="Only remove entries older than this many days.",
            check=_check_positive,
        ),
        Setting(
            name="search_paths",
            kind="str_list",
            default=(),
            help="Folders to search. The plugin is skipped until one is set.",
        ),
    ),
    # Receives the plugin's settings for the run and returns the literal paths
    # of each `prepared` action, keyed by action name. Use it to do one scan
    # that several actions share. It runs once per run, after the availability
    # and busy checks pass. Return None while a required setting is missing,
    # such as search paths, and the plugin is skipped as `not_configured`.
    # Re-measuring after a cleanup does not run it again but re-measures the
    # paths it returned. A hook that raises is logged and the plugin shows
    # with no prepared paths.
    prepare=_prepare,
    # Alternatives to `size_cmd`:
    # paths=("~/.cache/example-tool",),  # Globs, measured on disk.
    # size=_measure,  # Returns bytes, or None when the size is unknown.
    actions=(
        Action(
            # Kebab-case and unique within the plugin.
            name="prune",
            # SAFE: the tool rebuilds the data cheaply and automatically.
            # CAUTION: a rebuild is slow or needs the user to act.
            # DANGEROUS: the data may be lost for good. Runs only with --dangerous.
            tier=Tier.SAFE,
            # Shown in prompts and the JSON output. Say what a rebuild costs.
            help="Remove packages no longer in use. They download again when needed.",
            # The tool's own cleanup command. Prefer it to deleting paths.
            command=("example-tool", "cache", "prune"),
            # Sizes the action the same way as the plugin: `size` first, then
            # `size_cmd`, then `paths`. With none, the size shows as unknown.
            # A command shared with the plugin runs once per measurement.
            size_cmd=_CACHE_DIR_CMD,
            parse_size=partial(printed_dir_usage, patterns=("packages/*",)),
            # Turns a failed command's stderr into a hint that leads the error.
            explain_failure=_explain_failure,
        ),
        Action(
            name="logs",
            tier=Tier.CAUTION,
            help="Remove the tool's logs. Past runs can no longer be debugged.",
            # Without a command, purger deletes the matches itself, and only
            # below the home directory. With a command, the paths only size and
            # list what the command removes. When the globs depend on what is
            # on disk, pass a function that returns them; it runs each time the
            # action is measured. Never scan the disk at import.
            paths=("~/.local/state/example-tool/logs/*",),
            # Instead of `paths`, set prepared=True to take this action's paths
            # from what the plugin's `prepare` hook returns under its name.
            # Those paths are literal, not globs.
            # The plugin's footprint does not cover this data, so this action's
            # size is added to it.
            outside_footprint=True,
        ),
        Action(
            name="system",
            tier=Tier.DANGEROUS,
            help="Remove the tool's system-wide data. Every project must set up again.",
            command=("example-tool", "system", "clean"),
            # Runs the command with sudo. purger lets sudo ask for a password
            # at a terminal and fails the action instead of prompting without one.
            needs_sudo=True,
            # Returns bytes, or None when the size is unknown. A hook that
            # raises counts as unknown.
            # size=_measure_system,
        ),
    ),
)
