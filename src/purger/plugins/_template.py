"""Template for a new purger plugin, with every field set and explained.

This file is a complete plugin for an imaginary program named `example-tool`.
purger does not load a module whose name starts with an underscore, so this
plugin never runs. Copy the file to start a plugin of your own.

How to start a plugin:

1. Pick the plugin name. Use lowercase words joined by hyphens, such as
   `example-tool`.
2. Copy this file to `src/purger/plugins/<module>.py`. The module name is the
   plugin name with underscores in place of hyphens, such as `example_tool.py`.
3. In the copy, delete each field, action, and function that your tool does
   not need. Each comment below says what happens when a field is left out.
   A field whose comment does not say this is required. If you leave out a
   required field, Python raises TypeError when purger loads the plugin.
4. Delete the comments, and replace this docstring with one line that names
   the tool.
5. Add the plugin name to the set in `test_discover_plugins_finds_builtin_set`,
   and add each action to `_EXPECTED_ACTIONS`. Both are in
   `tests/plugins/test_definitions.py`.
6. Write the tests for the plugin in `tests/plugins/test_<module>.py`.

Terms used in this file:

- A plugin is the set of cleanups that purger offers for one tool. The
  `PLUGIN` variable at the end of this file holds it.
- An action is one cleanup that a person can approve or decline, such as
  "remove the package cache". A plugin has one or more actions.
- The cleanup of an action is how the action removes data. purger runs a
  command of the tool, or deletes files and folders itself.
- The size of an action is how much disk space the action frees. purger
  measures it before it asks the person to approve the action.
- The total size of a plugin is how much disk space the data of the tool uses
  in all. The `info` command shows it, and the JSON output reports it as
  `footprint_bytes`.
- The risk level of an action tells how costly it is to get the data back.
  It decides how purger asks for approval.
- A setting is a value that a person can change in the purger config file or
  with an environment variable.
- When purger skips a plugin, it leaves the plugin out of the run and reports
  the reason.

How to choose a cleanup:

- Use `RunCommand` when the tool has its own cleanup command, such as
  `uv cache prune`. The tool knows which of its files are in use, so its own
  command is the safer choice.
- Use `DeletePaths` when the tool has no cleanup command and nothing else
  keeps a record of the files. purger deletes the files and folders itself.

How to choose a size, for an action that uses `RunCommand` or for the total
size of a plugin. Use the first choice in this list that fits the tool:

- `SizeOfDirectoryPrintedBy`: the tool has a command that prints the folder
  where it keeps its data, such as `uv cache dir`.
- `SizeOfPaths`: the data is in files and folders that you can name with glob
  patterns, or find with a function.
- `SizeReportedByCommand`: the tool has a command that prints how much space
  a cleanup frees, such as `docker system df`.
- `SizeComputedBy`: no path or command gives the size, so a function that you
  write calculates it.
- `SizeUnknown`: purger cannot know the size before the cleanup runs.

An action that uses `DeletePaths` takes no size object. Its size is the disk
space of the paths that it deletes.

If a field has a value that cannot work, the plugin raises ValueError when
purger imports it, and the message tells you how to correct the field.
"""

import json
import time
from pathlib import Path

from purger.plugin import (
    Action,
    DeletePaths,
    OperatingSystem,
    PathSizes,
    Plugin,
    PluginSettings,
    RiskLevel,
    RunCommand,
    Setting,
    SettingValue,
    SizeBreakdownRow,
    SizeComputedBy,
    SizeOfDirectoryPrintedBy,
    SizeOfPaths,
    SizeReportedByCommand,
    SizeUnknown,
    at_least,
    directory_from_environment,
    folder_from_environment,
    once_per_run,
    program_matching,
    user_cache_folders,
)
from purger.runner import command_succeeds
from purger.safety import invoking_home

SECONDS_PER_DAY = 24 * 60 * 60

# `directory_from_environment` returns the value of an environment variable
# that moves the data folder of a tool, or the default when the variable is
# not set. A relative value counts as not set. Use it for every tool that
# reads such a variable, such as `CARGO_HOME` or `XDG_CACHE_HOME`. If you write
# a fixed folder in its place, purger looks in the wrong folder for each
# person who moved the data. The function escapes glob characters in the
# value, so that the result is safe to use in a glob pattern.
#
# `folder_from_environment` reads the variable the same way, but returns the
# folder as a literal path, with no escape. Use it when the code opens a file
# or lists a folder itself, and does not build a glob pattern. An escaped
# folder name does not match the real folder when you use it as a path.
EXAMPLE_TOOL_HOME = directory_from_environment("EXAMPLE_TOOL_HOME", default="~/.example-tool")

# `user_cache_folders` lists the usual places where a tool keeps a per-user
# cache: `~/Library/Caches/<name>` on macOS, and `~/.cache/<name>` and the
# folder in `XDG_CACHE_HOME` on Linux. The result has each folder one time,
# and is safe to use in a glob pattern. Use it for a tool that follows these
# conventions. Give `macos_folder_name` when the macOS folder has a different
# name, such as `Yarn` for `yarn`. If you leave it out, the macOS folder has
# the same name.
EXAMPLE_TOOL_CACHE_FOLDERS = user_cache_folders("example-tool", macos_folder_name="ExampleTool")

# This command prints the folder that holds all the data of example-tool.
# Keep a command in a constant when two or more size objects use it.
EXAMPLE_TOOL_HOME_COMMAND = ("example-tool", "home")


def expand_home(entry: str) -> Path:
    """Expand a leading `~` to the home folder of the person who runs purger.

    Use `invoking_home` in place of `Path.expanduser`, because purger can run
    with sudo, and `Path.expanduser` then gives the home folder of root.

    Args:
        entry: A folder path, which can start with `~`.

    Returns:
        Path: The folder, with `~` expanded.
    """
    if entry == "~":
        return invoking_home()
    if entry.startswith("~/"):
        return invoking_home() / entry.removeprefix("~/")
    return Path(entry)


def reject_relative_paths(value: SettingValue) -> None:
    """Reject a search folder that is not an absolute path, `~`, or a path that starts with `~/`.

    A relative path points to a different folder in each working folder, so
    the setting does not accept it.

    Args:
        value: The value of the `search_paths` setting.

    Raises:
        ValueError: If an entry is a relative path.
    """
    entries = value if isinstance(value, tuple) else ()
    for entry in entries:
        if entry != "~" and not entry.startswith(("/", "~/")):
            msg = f"entry {entry!r} must be an absolute path, ~, or start with ~/"
            raise ValueError(msg)


def search_folders(settings: PluginSettings) -> list[Path]:
    """Return the folders in the `search_paths` setting, with `~` expanded.

    Args:
        settings: The settings of the plugin for this run.

    Returns:
        list[Path]: The folders to search.
    """
    return [expand_home(entry) for entry in settings.get_string_list("search_paths")]


def example_tool_is_usable() -> bool:
    """Report whether example-tool can do a cleanup now.

    Returns:
        bool: True when `example-tool status` succeeds in 10 seconds or less.
    """
    return command_succeeds(["example-tool", "status"], timeout=10)


def has_existing_search_folder(settings: PluginSettings) -> bool:
    """Report whether at least one search folder exists on disk.

    Args:
        settings: The settings of the plugin for this run.

    Returns:
        bool: False when no search folder exists, so purger skips the plugin.
    """
    return any(folder.is_dir() for folder in search_folders(settings))


@once_per_run
def example_tool_scan(settings: PluginSettings) -> dict[str, list[Path]]:
    """Find the old logs and the crash reports in the search folders.

    `once_per_run` runs this function one time in each purger command, and
    gives the same result to every caller in that command. Two actions read
    this scan, so the scan runs one time for both. After a cleanup,
    purger measures the plugin again, and that measurement gets the same
    result too. Thus purger compares the same paths before and after, which
    gives a correct value for the freed space.

    `once_per_run` keeps a separate result for each plugin and for each set
    of setting values. If the function raises an exception, `once_per_run`
    remembers the exception and raises it again to each caller in that
    command, so a failed scan does not run again.

    Use `once_per_run` for a function that receives the settings and scans
    the disk. Without it, the scan runs again for each caller.

    Args:
        settings: The settings of the plugin for this run.

    Returns:
        dict[str, list[Path]]: The paths found for each kind of file. Each
            path is absolute, because purger does not accept a relative path.
    """
    oldest = time.time() - settings.get_integer("max_age_days") * SECONDS_PER_DAY
    found: dict[str, list[Path]] = {"old_logs": [], "crash_reports": []}
    for folder in search_folders(settings):
        found["old_logs"].extend(
            path for path in (folder / "logs").glob("*.log") if path.stat().st_mtime < oldest
        )
        found["crash_reports"].extend((folder / "crash-reports").glob("*"))
    return found


def old_logs(settings: PluginSettings) -> list[Path]:
    """Return the log files that are older than the `max_age_days` setting."""
    return example_tool_scan(settings)["old_logs"]


def crash_reports(settings: PluginSettings) -> list[Path]:
    """Return the crash reports in the search folders."""
    return example_tool_scan(settings)["crash_reports"]


def size_recorded_in_trash_index() -> int | None:
    """Add up the sizes that example-tool records for the items in its trash.

    This function reads a file, so it gets the data folder with
    `folder_from_environment` and not with `directory_from_environment`.

    Returns:
        int | None: The size in bytes, or None when the index file does not exist.
    """
    folder = folder_from_environment("EXAMPLE_TOOL_HOME", default="~/.example-tool")
    index = expand_home(folder) / "trash" / "index.json"
    if not index.is_file():
        return None
    return sum(entry["size_bytes"] for entry in json.loads(index.read_text()))


def explain_example_tool_failure(stderr: str) -> str | None:
    """Give the probable cause of a failure that the error text of example-tool does not show.

    Args:
        stderr: The error output of the command that failed.

    Returns:
        str | None: A hint for the person, or None when there is no hint to give.
    """
    if "EBUSY" not in stderr:
        return None
    return "Another example-tool process is using the cache. Stop it, then run this action again."


def one_row_per_search_folder(sizes: PathSizes, settings: PluginSettings) -> list[SizeBreakdownRow]:
    """Show the size of the found files for each search folder.

    Args:
        sizes: The size in bytes of each path that the action found.
        settings: The settings of the plugin for this run.

    Returns:
        list[SizeBreakdownRow]: One row for each search folder that holds a found path.
    """
    rows: list[SizeBreakdownRow] = []
    for folder in search_folders(settings):
        paths = tuple(sorted(path for path in sizes if folder in path.parents))
        if not paths:
            continue
        rows.append(
            SizeBreakdownRow(
                # The text that names the row.
                label=folder.name,
                # The size in bytes of the row, or None when it is unknown.
                size_bytes=sum(sizes[path] for path in paths),
                # Short names of the items in the row, shown next to its size.
                # If you leave this out, the row names no items.
                item_names=tuple(path.name for path in paths),
                # The full paths in the row, shown with `-vv` and in the JSON
                # output. If you leave this out, the row shows no paths.
                paths=paths,
                # The plugin summary merges the rows of all its actions into
                # one list. Rows with the same `merge_key` become one row. If
                # you leave this out, it is None, and rows with the same label
                # merge. Here, the label is only the folder name, so the full
                # path keeps two folders with the same name apart.
                merge_key=str(folder),
            )
        )
    return rows


PLUGIN = Plugin(
    # ----- Identity -----
    #
    # The name that people type to select the plugin, such as `purger clean
    # example-tool`. Use lowercase words joined by hyphens. The name must be
    # the module name with hyphens in place of underscores.
    name="example-tool",
    # One line that tells what data the plugin removes. The `info` table and
    # the help text of `purger info` and `purger clean` show it.
    description="Example tool package cache, downloads, and logs",
    #
    # ----- When the plugin runs -----
    #
    # purger does these checks in this order before it measures a plugin. The
    # first check that fails skips the plugin, and purger reports the reason.
    #
    # The operating systems where the tool exists. On any other system, purger
    # skips the plugin. If you leave this out, the plugin runs on macOS and on
    # Linux.
    operating_systems=(OperatingSystem.MACOS, OperatingSystem.LINUX),
    # A program that must be on PATH. If it is not on PATH, purger skips the
    # plugin and reports the program as missing. Use it for each tool that
    # has a program. If you leave this out, purger does not look for a program.
    requires_on_path="example-tool",
    # A function that returns True when the tool is ready for a cleanup. Use it
    # for a tool that can be installed but not usable, such as a program whose
    # background service is stopped. If the function returns False or raises
    # an exception, purger skips the plugin as unavailable. The function runs
    # on each `info` and `clean` command, so make it fast. If you leave this
    # out, purger does not do this check.
    skip_unless=example_tool_is_usable,
    # A function that receives the command line of each running process, as a
    # list of words, and returns True for a process of the tool. While such a
    # process runs, purger skips the plugin as unavailable, because the
    # process can be using the files that a cleanup removes. The processes
    # that purger starts itself never count. If the function raises an
    # exception, purger skips the plugin as unavailable. If you leave this
    # out, purger does not look at running processes.
    #
    # `program_matching` makes this function for you. Its first argument is a
    # regular expression that must match the full file name of the program.
    # It also finds the program when an interpreter starts it, such as
    # `node /usr/local/bin/npm`. Put the subcommands that do not touch the
    # data of the tool in `ignoring_subcommands`, so that a long-running one
    # does not block the cleanup.
    skip_while_running=program_matching(r"example-tool", ignoring_subcommands={"shell"}),
    # A function that receives the settings and returns False when the plugin
    # is not configured correctly. purger then skips the plugin as not
    # configured. Use it for a problem that `required=True` on a setting
    # cannot find, such as a setting whose folders do not exist. The check
    # runs after the check for required settings. If the function raises an
    # exception, purger logs a warning and skips the plugin as not
    # configured. If you leave this out, purger does not do this check.
    skip_unless_configured=has_existing_search_folder,
    #
    # ----- Settings -----
    #
    # The values that a person can change. If you leave this out, the plugin
    # has no settings.
    #
    # A person sets a value in the `[plugins.example-tool]` table of the purger
    # config file, or with an environment variable. The name of the variable
    # is `PURGER_`, the plugin name in capitals with underscores in place of
    # hyphens, two underscores, and the setting name in capitals. An example
    # is `PURGER_EXAMPLE_TOOL__MAX_AGE_DAYS`. A list in an environment variable
    # has commas between the items.
    #
    # purger reads each value and makes sure that it has the correct type
    # before the plugin runs. Functions in this file that receive a
    # `PluginSettings` read the values with `get_string`, `get_integer`,
    # `get_boolean`, or `get_string_list`.
    settings=(
        Setting(
            # The key in the config file. Use lowercase words joined by
            # underscores. Each setting in a plugin must have a different name.
            name="max_age_days",
            # The type of the value: `str`, `int`, `bool`, or `list[str]`.
            value_type=int,
            # The value when the person sets none. It must have the type in
            # `value_type`. Write a `list[str]` default as a list.
            default=30,
            # One or more sentences that tell a reader of the plugin what the
            # value does. This field is required and must not be blank.
            description="Remove logs that are older than this number of days.",
            # A function that raises ValueError for a value that the type
            # alone does not reject. purger then stops with an error that
            # names the setting and the value. The function also runs on the
            # default when purger imports the plugin. `at_least(1)` rejects an
            # integer less than 1. If you leave this out, purger accepts each
            # value of the correct type.
            validate=at_least(1),
        ),
        Setting(
            name="search_paths",
            value_type=list[str],
            default=[],
            description="Folders that hold example-tool projects. Each must be an absolute path, ~, or start with ~/.",
            validate=reject_relative_paths,
            # Set `required=True` when the plugin cannot run until the person
            # sets a value. While the value is empty or only whitespace, purger skips the plugin
            # as not configured. Only a `str` or `list[str]` setting can be
            # required, because only those types have an empty value. If you
            # leave this out, the setting is not required.
            required=True,
        ),
    ),
    #
    # ----- Total size -----
    #
    # How much disk space the data of the tool uses in all. Use one of the
    # size objects that the module docstring lists. Set it when the tool
    # can report the size of all its data, or when two actions can remove the
    # same data. If you leave this out, the total size is the sum of the
    # sizes of the actions. That sum is correct only when no two actions
    # remove the same data and each action knows its size. A test makes
    # sure that a plugin with two or more `RunCommand` actions, and a
    # `SizeUnknown` among them, sets the total size.
    #
    # `SizeOfDirectoryPrintedBy` runs a command that prints the full path of
    # one folder, and measures that folder. Use it when the tool can tell
    # where it keeps its data, because the tool knows each place the folder
    # can be. When the plugin and its actions use the same command, purger
    # runs it one time for each measurement.
    total_size=SizeOfDirectoryPrintedBy(EXAMPLE_TOOL_HOME_COMMAND),
    #
    # ----- Actions -----
    #
    # The cleanups that the plugin offers. A plugin must have one or more
    # actions. The first action explains the fields that each action has.
    # Each action after it shows a different cleanup or size object.
    actions=(
        Action(
            # The name that people type to select the action. Use lowercase
            # words joined by hyphens. Each action in a plugin must have a
            # different name.
            name="prune",
            # The risk level tells how costly it is to get the data back:
            #
            # SAFE: The tool makes the data again by itself, and the cost is
            #   at most a slower next run. purger asks one question for all
            #   SAFE actions, and the `--yes` flag answers it.
            # CAUTION: The data is slow to get back, or the person must do
            #   something, such as install packages again. purger asks about
            #   each CAUTION action separately.
            # DANGEROUS: The data can be lost permanently. purger offers a
            #   DANGEROUS action only when the person adds the `--dangerous`
            #   flag, and then asks about each one separately.
            #
            # The `--yes` flag, the `--json` flag, and a run with no terminal
            # never approve a CAUTION or DANGEROUS action.
            risk_level=RiskLevel.SAFE,
            # The text that tells what the action removes. purger shows it in
            # the separate prompt for each CAUTION and DANGEROUS action. The
            # single prompt for SAFE actions names only the plugins and a
            # total size, so it does not show this text. The JSON output
            # reports it under the key `help`. Start with a verb such as
            # "Remove". Then tell what it costs to get the data back, so that
            # the person can decide.
            description="Remove packages that no project uses. They download again when needed.",
            # How the action removes the data. This field is required.
            #
            # `RunCommand` runs the cleanup command of the tool. Give the
            # program and each argument as a separate string. purger does not
            # use a shell, so the arguments need no quotes.
            cleanup=RunCommand("example-tool", "cache", "prune"),
            # How much disk space the action frees. An action with
            # `RunCommand` must have a size. If purger cannot know the size,
            # write `SizeUnknown`, which the "system" action below shows. An
            # action with `DeletePaths` must not have a size.
            #
            # `subpaths` holds glob patterns relative to the printed folder.
            # purger measures only the paths that they match. If you leave
            # `subpaths` out, purger measures the whole folder.
            size=SizeOfDirectoryPrintedBy(EXAMPLE_TOOL_HOME_COMMAND, subpaths=("packages/*",)),
            # A function that receives the error output of a command that
            # failed, and returns a hint or None. purger shows the hint above
            # the error text of the tool. Use it when the error text of the
            # tool does not tell the person how to correct the problem. Only
            # an action with `RunCommand` can have it. If you leave this out,
            # purger shows only the error text of the tool.
            explain_failure=explain_example_tool_failure,
        ),
        Action(
            name="index",
            risk_level=RiskLevel.SAFE,
            description="Remove the search index. example-tool builds it again on its next run.",
            cleanup=RunCommand("example-tool", "index", "clear"),
            # `SizeOfPaths` measures the files and folders that glob patterns
            # match. A `~` at the start of a pattern is the home folder of the
            # person who runs purger. Use it when you know where the data is.
            # Give glob patterns, or give `found_by=` a function that returns
            # paths, as the "old-logs" action shows. Give one of the two, not
            # both.
            #
            # This action uses `RunCommand` and not `DeletePaths`, because
            # example-tool keeps a record of its index files. A deletion by
            # purger leaves that record wrong.
            size=SizeOfPaths(f"{EXAMPLE_TOOL_HOME}/index/*"),
        ),
        Action(
            name="downloads",
            risk_level=RiskLevel.CAUTION,
            description="Remove downloaded toolchains. Large toolchains must download again before their next use.",
            cleanup=RunCommand("example-tool", "downloads", "clear"),
            # `SizeReportedByCommand` runs a command and gives its standard
            # output to `read_size`, which returns a number of bytes. Use it
            # when the tool can tell how much space a cleanup frees.
            # `read_size` is required. Write `read_size=int` when the command
            # prints only a number of bytes. If the command prints a size
            # such as "1.5 GB", use `parse_human_size` from `purger.sizing`.
            # If the command fails or `read_size` raises an exception, purger
            # shows the size as unknown.
            size=SizeReportedByCommand(
                ("example-tool", "downloads", "size", "--bytes"), read_size=int
            ),
        ),
        Action(
            name="trash",
            risk_level=RiskLevel.CAUTION,
            description="Remove the items in the example-tool trash. They cannot be restored after this.",
            cleanup=RunCommand("example-tool", "trash", "empty"),
            # `SizeComputedBy` calls a function that you write. The function
            # takes no arguments, and returns a number of bytes, or None when
            # the size is unknown. Use it when no path or command gives the
            # size. If the function raises an exception, purger shows the
            # size as unknown.
            size=SizeComputedBy(size_recorded_in_trash_index),
        ),
        Action(
            name="system",
            risk_level=RiskLevel.DANGEROUS,
            description="Remove the system-wide data of example-tool. Each project must be set up again.",
            # Set `with_sudo=True` when the command must run as root. At a
            # terminal, sudo can ask for a password. With no terminal, purger
            # never waits for a password. It first runs `sudo -n true` to see
            # if sudo can run without one. If sudo needs a password, purger
            # skips the action. If that check passes but the command then
            # fails under sudo, the action fails. If you leave this out, the
            # command runs as the person who runs purger.
            cleanup=RunCommand("example-tool", "system", "clean", with_sudo=True),
            # `SizeUnknown` tells purger and the reader that the size cannot
            # be known before the cleanup runs. `reason` must not be empty.
            # It is for people who read the code, and purger never shows it.
            size=SizeUnknown(reason="example-tool cannot tell what system clean removes"),
        ),
        Action(
            name="temporary-files",
            risk_level=RiskLevel.SAFE,
            description="Remove temporary files that example-tool left behind. The tool does not use them again.",
            # `DeletePaths` makes purger delete the files and folders itself.
            # Its size is the disk space of those paths. Give it glob
            # patterns when the paths are at fixed places, with a `~` at the
            # start for the home folder. purger deletes only paths in the
            # home folder of the person who runs purger. It refuses a direct
            # child of the home folder, such as `~/.example-tool` itself, so
            # match the items inside such a folder. It deletes a symbolic link
            # and not the data that the link points to. When purger measures,
            # it drops each path that it reaches through a folder that is a
            # symbolic link. A pattern that matches nothing leaves the action
            # with nothing to clean.
            cleanup=DeletePaths(
                f"{EXAMPLE_TOOL_HOME}/scratch/*",
                *(f"{folder}/leftovers/*" for folder in EXAMPLE_TOOL_CACHE_FOLDERS),
            ),
        ),
        Action(
            name="old-logs",
            risk_level=RiskLevel.CAUTION,
            description="Remove example-tool logs older than the max_age_days setting. You cannot examine those past runs after this.",
            # Give `found_by=` a function when the paths depend on settings
            # or on what is on disk. The function receives the settings and
            # returns full paths. purger uses these paths exactly as given,
            # so a glob character in a name needs no escape. purger calls the
            # function only when it measures, so the disk is not scanned when
            # purger imports the plugin. If the function raises an exception,
            # purger logs a warning and the action offers nothing.
            cleanup=DeletePaths(found_by=old_logs),
            # Set `adds_to_plugin_total_size=True` when the total size of the
            # plugin does not measure the data of this action. purger then
            # adds the size of this action to the total size. The logs are in
            # the search folders and not in the data folder of example-tool,
            # so the total size does not include them. Only a plugin that sets
            # `total_size` can use this field. If you leave this out, purger
            # does not add the size of the action to the total.
            adds_to_plugin_total_size=True,
            # A function that divides the size of the action into labeled
            # rows. The rows show with `-v`, and in the `details` of the JSON
            # output. The function receives the size of each path that the
            # action found, and the settings. It returns `SizeBreakdownRow`
            # objects, such as one row for each project. If you leave this out,
            # an action with paths shows one row for each path, and an action
            # with no paths shows no rows. If the function raises an exception,
            # purger shows one row for each path.
            size_breakdown=one_row_per_search_folder,
        ),
        Action(
            name="crash-reports",
            risk_level=RiskLevel.CAUTION,
            description="Remove example-tool crash reports. You cannot examine those past crashes after this.",
            cleanup=DeletePaths(found_by=crash_reports),
            adds_to_plugin_total_size=True,
            size_breakdown=one_row_per_search_folder,
        ),
    ),
)
