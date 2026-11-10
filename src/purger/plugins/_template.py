"""Template for a new plugin. Copy it to `<plugin_name>.py` and edit the copy.

purger never registers a module whose name starts with an underscore, so this
file is reference only. It sets or names every `Plugin` and `Action` field,
and a test fails when a field is added without documenting it here. In the
copy, delete the fields and actions the tool does not need, and replace this
docstring with one line that names the tool.
"""

from functools import partial

from purger.plugin import Action, Plugin, Tier
from purger.runner import command_succeeds
from purger.sizing import printed_dir_usage

_CACHE_DIR_CMD = ("example-tool", "cache", "dir")


def _is_available() -> bool:
    """Report whether the tool can run cleanups now, such as a daemon that answers.

    Returns:
        bool: True when the tool is usable.
    """
    return command_succeeds(["example-tool", "status"], timeout=10)


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
    # The footprint is the size in the `info` table. purger uses the first of
    # `size`, `size_cmd`, or `paths` that is set. With none, the footprint is
    # the sum of the action sizes, which is right only when no two actions
    # remove the same data.
    size_cmd=_CACHE_DIR_CMD,
    # Turns the `size_cmd` output into bytes. When the tool prints its cache
    # directory, `printed_dir_usage` measures the directory it really uses.
    parse_size=printed_dir_usage,
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
