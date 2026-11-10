"""Help text for the purger CLI, including the plugin catalog generated from the registry."""

from collections.abc import Iterable

from purger.plugin import Plugin
from purger.registry import skip_reason
from purger.reports import Status

APP_HELP = """\
Find rebuildable caches and free disk space.

purger measures the caches that developer tools leave behind (package manager
downloads, container images, build products) and removes them after you confirm.
It prefers each tool's own cleanup command. It deletes a cache directory by hand
only when the tool has no command, and only inside your home directory. Without
`--dangerous` and your explicit confirmation, it never deletes your files, your
projects, data that cannot be rebuilt, or anything outside the declared cache paths.

**Risk tiers.** Every cleanup action has one tier:

- `safe`: rebuilt cheaply and automatically. All safe actions run after one confirmation.
- `caution`: rebuildable, but slow or a large download. Each one asks for its own confirmation.
- `dangerous`: can destroy data that cannot be recreated, or wipes a cache completely.
  Hidden unless you pass `--dangerous`, and each one asks for its own confirmation.

**For agents and scripts.** Pass `--json` to any command to get exactly one JSON
document on stdout. `purger clean --json --yes` cleans `safe` caches only.
`caution` and `dangerous` actions never run without a person answering a prompt,
so a non-interactive run reports them as `declined`. Do not run purger as root:
it refuses, and it asks for sudo only for the actions that need it.

Start with `purger info` to see what can be cleaned, then run `purger clean`.
"""

INFO_HELP = """\
Show the caches on this machine and how much space each uses.

Read-only: info never deletes anything. It measures every plugin that applies
to this machine and prints a table with one row per tool: its cache size and
description, largest first, and a total at the bottom. A check under `Partial
purge` means a clean frees only part of that size, because the tool decides at
run time what it still needs. Sizes that cannot be measured show `?` and are
left out of the total. `dangerous` actions are left out unless you pass
`--dangerous`, which can turn a partial purge into a full one. Plugins that do
not apply here (wrong platform, tool not installed, or tool not running) are
not shown, unless you name one.
"""

CLEAN_HELP = """\
Remove caches after confirmation, with stricter confirmation for riskier actions.

clean first measures and prints the same table as `purger info`. Actions with
nothing to clean are skipped. Then it asks:

1. One confirmation for all `safe` actions. `--yes` skips only this prompt.
2. One confirmation for each `caution` action, showing what it costs.
3. With `--dangerous` only, one confirmation for each `dangerous` action,
   warning that data may be lost for good.

Then it runs the approved actions. A failed action is reported and the run
continues with the next one. At the end it measures again and prints a table
of each tool's cache size before and after, and the space freed. Actions that
need sudo say so before they run.

Without an interactive terminal (or with `--json`), purger cannot prompt:
`safe` actions run only with `--yes`, `caution` and `dangerous` actions are
declined with a warning, and sudo actions run only when sudo needs no password.

`--dry-run` prints each command or path that would be cleaned, asks nothing,
and changes nothing.
"""

_INFO_EXIT_CODES = """\
**Exit codes**

- `0`: success.
- `2`: usage error, such as an unknown plugin name or running as root.
- `130`: interrupted with Ctrl-C.
"""

_CLEAN_EXIT_CODES = """\
**Exit codes**

- `0`: success, including when every action was declined.
- `1`: at least one confirmed action failed.
- `2`: usage error, such as an unknown plugin name or running as root.
- `130`: interrupted with Ctrl-C.
"""

_JSON_ERRORS = """\
Usage errors print `{"schema_version": 1, "error": {"code": ..., "message": ...}}`
with code `usage` (an unknown option or a bad value), `unknown_plugin`,
`running_as_root`, or `interrupted` (Ctrl-C while measuring).
"""

INFO_EPILOGUE = f"""\
**Examples**

- `purger info`: show every cache on this machine.
- `purger info uv brew`: show only the uv and brew caches.
- `purger info --json`: the same report as one JSON document.
- `purger info --dangerous`: also show `dangerous` actions.

{_INFO_EXIT_CODES}
**JSON output** (`--json`)

Top-level keys: `schema_version` (always 1), `platform` (`darwin` or `linux`),
`total_bytes`, `plugins`, and `skipped`. Each plugin has `name`, `description`,
`footprint_bytes`, and `actions`. Each action has `name`, `tier`, `help`,
`size_bytes`, `command` (argv list or null), `paths` (resolved paths), and
`needs_sudo`. Each `skipped` entry has `name` and `reason` (`platform`,
`missing_binary`, or `unavailable`). Sizes are integer bytes, or null when unknown.
{_JSON_ERRORS}"""

_STATUS_VALUES = ", ".join(f"`{status.value}`" for status in Status)

CLEAN_EPILOGUE = f"""\
**Examples**

- `purger clean`: measure, confirm, and clean every cache.
- `purger clean uv xcode --dry-run`: show what would be cleaned for uv and xcode.
- `purger clean --json --yes`: clean `safe` caches without prompts, as JSON.

{_CLEAN_EXIT_CODES}
**JSON output** (`--json`)

The same document as `purger info --json`, plus `total_freed_bytes` at the top
level, `freed_bytes` on each plugin, and `status` and `error` on each action.
`status` is one of: {_STATUS_VALUES}. `declined` means no one approved it.
`skipped` means it never started (for example sudo needs a password, or Ctrl-C
stopped the run first). `failed` includes an action stopped by Ctrl-C, with
`error` set to `interrupted`. `planned` appears only with `--dry-run`.
{_JSON_ERRORS}"""


def _platforms(plugin: Plugin) -> str:
    return ", ".join(sorted(plugin.platforms))


def plugin_catalog(plugins: Iterable[Plugin]) -> str:
    """Describe every plugin in one line each as markdown for the help pages.

    Generating the list from the registry keeps help in step with the plugin files.
    Availability is judged by platform and required binary only, so rendering help
    never runs a plugin's own availability check.

    Args:
        plugins: Plugins to describe.

    Returns:
        str: A markdown section listing each plugin with its platforms.
    """
    lines = ["**Available plugins**", ""]
    for plugin in plugins:
        line = f"- **{plugin.name}**: {plugin.description} ({_platforms(plugin)})"
        if skip_reason(plugin, check_available=False) is not None:
            line += ", *not available on this machine*"
        lines.append(line)
    return "\n".join(lines) + "\n"


def build_epilogue(static: str, plugins: Iterable[Plugin]) -> str:
    """Join a command's fixed epilogue with the generated plugin catalog.

    Args:
        static: Examples, exit codes, and other fixed text. May be empty.
        plugins: Plugins to list.

    Returns:
        str: The full markdown epilogue.
    """
    catalog = plugin_catalog(plugins)
    return f"{static}\n{catalog}" if static else catalog
