"""Plugin for Homebrew downloads and old versions."""

import re
from pathlib import Path

from nclutils.sh import run_command

from purger.plugin import Action, Plugin, Tier
from purger.sizing import SIZE_CMD_TIMEOUT_SECONDS, disk_usage, parse_human_size

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


def _autoremove_names(output: str) -> list[str]:
    """Read the formula names a `brew autoremove` dry run lists.

    Tap formulae print as `user/tap/name`, but the Cellar keys them by the last part.

    Args:
        output: Standard output of `brew autoremove --dry-run`.

    Returns:
        list[str]: Cellar directory names of the formulae that would be removed.
    """
    return [
        line.strip().rsplit("/", maxsplit=1)[-1]
        for line in output.splitlines()
        if line.strip() and not line.startswith("==>")
    ]


def _autoremove_size() -> int:
    """Measure the Cellar directories of the formulae `brew autoremove` would remove.

    Returns:
        int: Allocated bytes of those formulae, across every installed version.
    """
    cellar = Path(
        run_command(["brew", "--cellar"], timeout=SIZE_CMD_TIMEOUT_SECONDS).stdout.strip()
    )
    output = run_command(
        ["brew", "autoremove", "--dry-run"], timeout=SIZE_CMD_TIMEOUT_SECONDS
    ).stdout
    return disk_usage(cellar / name for name in _autoremove_names(output))


PLUGIN = Plugin(
    name="brew",
    description="Homebrew downloads, outdated versions, and unneeded dependencies",
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
        Action(
            name="autoremove",
            tier=Tier.CAUTION,
            help=(
                "Uninstall formulae that were installed only as dependencies "
                "and that nothing installed needs anymore."
            ),
            command=("brew", "autoremove"),
            size=_autoremove_size,
        ),
    ),
)
