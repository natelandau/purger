"""Plugin for the package managers that corepack downloads."""

from purger.plugin import (
    Action,
    Plugin,
    RiskLevel,
    RunCommand,
    SizeOfPaths,
    directory_from_environment,
)


def corepack_home_directory() -> str:
    """Return the corepack home, which uses ~/.cache on macOS too, not ~/Library/Caches.

    Returns:
        str: The folder as a glob-safe pattern.
    """
    cache_home = directory_from_environment("XDG_CACHE_HOME", default="~/.cache")
    return directory_from_environment("COREPACK_HOME", default=f"{cache_home}/node/corepack")


COREPACK_INSTALLS = f"{corepack_home_directory()}/v1"

PLUGIN = Plugin(
    name="corepack",
    description="Package manager versions downloaded by corepack",
    requires_on_path="corepack",
    actions=(
        Action(
            name="cache",
            risk_level=RiskLevel.CAUTION,
            description=(
                "Remove every yarn, pnpm, and npm version corepack downloaded. "
                "Each one downloads again the next time a project uses it."
            ),
            cleanup=RunCommand("corepack", "cache", "clean"),
            size=SizeOfPaths(COREPACK_INSTALLS),
        ),
    ),
)
