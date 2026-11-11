"""Plugin for the npm package cache and npx cache."""

from functools import partial

from purger.plugin import Action, Plugin, Tier
from purger.plugins._shared import program_matcher
from purger.sizing import printed_dir_usage

_CACHE_DIR_CMD = ("npm", "config", "get", "cache")


def _explain_npx_failure(stderr: str) -> str | None:
    """Name the usual cause when npm cannot remove the npx cache directory.

    A running npx command (an MCP server or a status line, for example) can
    reinstall a package while npm removes the cache, so the final rmdir finds
    the directory not empty.

    Args:
        stderr: The failed npm command's stderr.

    Returns:
        str | None: A hint for ENOTEMPTY, or None for any other failure.
    """
    if "ENOTEMPTY" not in stderr:
        return None
    return (
        "An npx command wrote to the npx cache while it was being removed. "
        "Most of the cache is likely gone. Stop programs that run through npx, "
        "such as MCP servers or a status line, then run this action again."
    )


PLUGIN = Plugin(
    name="npm",
    description="npm package cache and npx cache",
    requires="npm",
    size_cmd=_CACHE_DIR_CMD,
    # Only the parts purger can clean, so the two cleanups account for the whole footprint.
    parse_size=partial(printed_dir_usage, patterns=("_cacache", "_npx")),
    # npx, `npm exec`, and package scripts such as `npm run dev` often run for
    # hours as MCP servers, a status line, or a dev server, and leave the
    # package cache alone, so only other npm commands count.
    busy_process=program_matcher(
        r"npm(-cli\.js)?",
        ignore_subcommands=frozenset(
            {
                "exec",
                "x",
                "run",
                "run-script",
                "rum",
                "urn",
                "start",
                "stop",
                "restart",
                "test",
                "t",
                "tst",
            }
        ),
    ),
    actions=(
        Action(
            name="verify",
            tier=Tier.SAFE,
            help="Garbage-collect cache data that is no longer needed and verify the rest.",
            command=("npm", "cache", "verify"),
        ),
        Action(
            name="npx",
            tier=Tier.SAFE,
            help="Remove packages cached by npx. They download again on the next npx run.",
            command=("npm", "cache", "npx", "rm", "--force"),
            size_cmd=_CACHE_DIR_CMD,
            parse_size=partial(printed_dir_usage, patterns=("_npx",)),
            explain_failure=_explain_npx_failure,
        ),
        Action(
            name="clean",
            tier=Tier.DANGEROUS,
            help="Wipe the entire package cache. Every package is downloaded again on next install.",
            command=("npm", "cache", "clean", "--force"),
            size_cmd=_CACHE_DIR_CMD,
            parse_size=partial(printed_dir_usage, patterns=("_cacache",)),
        ),
    ),
)
