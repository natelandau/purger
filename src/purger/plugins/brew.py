"""Plugin for Homebrew downloads and old versions."""

import re

from purger.plugin import Action, Plugin, Tier
from purger.sizing import parse_human_size

_FREED = re.compile(r"free approximately (.+?) of disk space")
_DECIMAL_LOOKING_UNIT = re.compile(r"([KMGT])B\s*$", re.IGNORECASE)


def _parse_dry_run(output: str) -> int:
    """Read the bytes a `brew cleanup` dry run would free.

    Brew prints no summary line when nothing would be freed, so a missing
    match means zero. Brew labels sizes "KB", "MB", and "GB" but computes them
    in powers of 1024, so each unit is read as its binary equivalent.

    Args:
        output: Standard output of `brew cleanup --dry-run`.

    Returns:
        int: Bytes that would be freed.
    """
    match = _FREED.search(output)
    if match is None:
        return 0
    return parse_human_size(_DECIMAL_LOOKING_UNIT.sub(r"\1iB", match.group(1)))


PLUGIN = Plugin(
    name="brew",
    description="Homebrew downloads and outdated versions",
    requires="brew",
    actions=(
        Action(
            name="cleanup",
            tier=Tier.SAFE,
            help="Remove old formula versions and the entire download cache.",
            command=("brew", "cleanup", "--prune=all"),
            size_cmd=("brew", "cleanup", "--prune=all", "--dry-run"),
            parse_size=_parse_dry_run,
        ),
    ),
)
