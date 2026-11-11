"""Plugin for the bun package cache."""

import logging
import re

from nclutils.sh import ShellCommandError, run_command

from purger.plugin import (
    Action,
    Plugin,
    RiskLevel,
    RunCommand,
    SizeOfDirectoryPrintedBy,
    SizeUnknown,
)

BUN_CACHE_DIRECTORY_COMMAND = ("bun", "pm", "cache")
# The first release with `bun pm cache prune`. Older bun prints the cache path for
# any unknown cache action and exits zero, so a prune would report success and do nothing.
_MIN_VERSION = (1, 4, 4)
_VERSION = re.compile(r"^(\d+)\.(\d+)\.(\d+)")

logger = logging.getLogger(__name__)


def bun_supports_cache_prune() -> bool:
    """Report whether the installed bun has `bun pm cache prune`.

    Returns:
        bool: True when `bun --version` is 1.4.4 or later.
    """
    try:
        output = run_command(["bun", "--version"], check=False, timeout=10).stdout.strip()
    except ShellCommandError as exc:
        logger.debug("cannot read the bun version: %s", exc)
        return False
    match = _VERSION.match(output)
    if match is None:
        logger.debug("cannot parse the bun version: %r", output)
        return False
    version = tuple(int(part) for part in match.groups())
    if version < _MIN_VERSION:
        logger.debug("skipping bun %s: cache prune needs 1.4.4 or later", output)
        return False
    return True


PLUGIN = Plugin(
    name="bun",
    description="bun package cache",
    requires_on_path="bun",
    skip_unless=bun_supports_cache_prune,
    total_size=SizeOfDirectoryPrintedBy(BUN_CACHE_DIRECTORY_COMMAND),
    actions=(
        Action(
            name="prune",
            risk_level=RiskLevel.SAFE,
            description=(
                "Remove packages bun has not downloaded or linked in 30 days. "
                "They download again when a project needs them."
            ),
            cleanup=RunCommand("bun", "pm", "cache", "prune", "--max-age", "30"),
            size=SizeUnknown(reason="bun cannot preview which packages cache prune removes"),
        ),
        Action(
            name="clean",
            risk_level=RiskLevel.DANGEROUS,
            description=(
                "Wipe the entire bun package cache. Every package downloads again, "
                "and projects that use bun's global store need `bun install` first."
            ),
            cleanup=RunCommand("bun", "pm", "cache", "rm"),
            size=SizeOfDirectoryPrintedBy(BUN_CACHE_DIRECTORY_COMMAND),
        ),
    ),
)
