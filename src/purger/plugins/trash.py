"""Plugin for the Trash in the user's home folder on macOS and Linux."""

import sys

from purger.plugin import Action, Plugin, Tier
from purger.plugins._shared import env_dir
from purger.safety import invoking_home


def _trash_paths(platform: str) -> tuple[str, ...]:
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
    trash = f"{env_dir('XDG_DATA_HOME', '~/.local/share')}/Trash"
    return (f"{trash}/files/*", f"{trash}/info/*", f"{trash}/directorysizes")


def _is_available() -> bool:
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


_ON_MACOS = sys.platform == "darwin"

PLUGIN = Plugin(
    name="trash",
    description=(
        "macOS Trash in your home folder (the terminal needs Full Disk Access)"
        if _ON_MACOS
        else "Trash in your home folder"
    ),
    platforms=frozenset({"darwin", "linux"}),
    is_available=_is_available if _ON_MACOS else None,
    actions=(
        Action(
            name="empty",
            tier=Tier.CAUTION,
            help=(
                "Remove everything in the Trash in your home folder. "
                "Removed items cannot be recovered."
            ),
            paths=_trash_paths(sys.platform),
        ),
    ),
)
