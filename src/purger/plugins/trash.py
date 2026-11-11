"""Plugin for the Trash in the user's home folder on macOS and Linux."""

import sys

from purger.plugin import (
    Action,
    DeletePaths,
    OperatingSystem,
    Plugin,
    RiskLevel,
    directory_from_environment,
)
from purger.safety import invoking_home


def trash_globs(platform: str) -> tuple[str, ...]:
    """List what emptying the home Trash removes on a platform.

    On Linux the freedesktop Trash keeps its `files` and `info` folders, so only
    their contents go, along with the `directorysizes` cache that would go stale.

    Args:
        platform: A `sys.platform` value.

    Returns:
        tuple[str, ...]: Glob patterns of the items to delete.
    """
    if platform == "darwin":
        return ("~/.Trash/*",)
    trash = f"{directory_from_environment('XDG_DATA_HOME', default='~/.local/share')}/Trash"
    return (f"{trash}/files/*", f"{trash}/info/*", f"{trash}/directorysizes")


def trash_is_readable() -> bool:
    """Report whether the macOS Trash can be listed, which needs Full Disk Access.

    Without this check, an unreadable Trash would measure as empty.

    Returns:
        bool: True when the Trash is readable or does not exist.
    """
    try:
        next((invoking_home() / ".Trash").iterdir(), None)
    except FileNotFoundError:
        return True
    except PermissionError:
        return False
    return True


ON_MACOS = sys.platform == "darwin"

PLUGIN = Plugin(
    name="trash",
    description=(
        "macOS Trash in your home folder (the terminal needs Full Disk Access)"
        if ON_MACOS
        else "Trash in your home folder"
    ),
    operating_systems=(OperatingSystem.MACOS, OperatingSystem.LINUX),
    skip_unless=trash_is_readable if ON_MACOS else None,
    actions=(
        Action(
            name="empty",
            risk_level=RiskLevel.CAUTION,
            description=(
                "Remove everything in the Trash in your home folder. "
                "Removed items cannot be recovered."
            ),
            cleanup=DeletePaths(*trash_globs(sys.platform)),
        ),
    ),
)
