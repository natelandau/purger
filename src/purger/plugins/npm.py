"""Plugin for the npm package cache and npx cache."""

from purger.plugin import (
    Action,
    Plugin,
    RiskLevel,
    RunCommand,
    SizeOfDirectoryPrintedBy,
    SizeUnknown,
    program_matching,
)

NPM_CACHE_DIRECTORY_COMMAND = ("npm", "config", "get", "cache")


def explain_npx_failure(stderr: str) -> str | None:
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
    requires_on_path="npm",
    # npx, `npm exec`, and package scripts such as `npm run dev` often run for
    # hours as MCP servers, a status line, or a dev server, and leave the
    # package cache alone, so only other npm commands count.
    skip_while_running=program_matching(
        r"npm(-cli\.js)?",
        ignoring_subcommands=frozenset(
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
    # Only the parts purger can clean, so the two cleanups account for everything `total_size` measures.
    total_size=SizeOfDirectoryPrintedBy(NPM_CACHE_DIRECTORY_COMMAND, subpaths=("_cacache", "_npx")),
    actions=(
        Action(
            name="verify",
            risk_level=RiskLevel.SAFE,
            description="Garbage-collect cache data that is no longer needed and verify the rest.",
            cleanup=RunCommand("npm", "cache", "verify"),
            size=SizeUnknown(reason="npm cannot preview what cache verify removes"),
        ),
        Action(
            name="npx",
            risk_level=RiskLevel.SAFE,
            description="Remove packages cached by npx. They download again on the next npx run.",
            cleanup=RunCommand("npm", "cache", "npx", "rm", "--force"),
            size=SizeOfDirectoryPrintedBy(NPM_CACHE_DIRECTORY_COMMAND, subpaths=("_npx",)),
            explain_failure=explain_npx_failure,
        ),
        Action(
            name="clean",
            risk_level=RiskLevel.DANGEROUS,
            description="Wipe the entire package cache. Every package is downloaded again on next install.",
            cleanup=RunCommand("npm", "cache", "clean", "--force"),
            size=SizeOfDirectoryPrintedBy(NPM_CACHE_DIRECTORY_COMMAND, subpaths=("_cacache",)),
        ),
    ),
)
