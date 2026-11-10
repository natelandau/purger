"""Plugin for the package managers that corepack downloads."""

from purger.plugin import Action, Plugin, Tier
from purger.plugins._shared import env_dir


def _home() -> str:
    """Return the corepack home, which uses ~/.cache on macOS too, not ~/Library/Caches.

    Returns:
        str: The folder as a glob-safe pattern.
    """
    return env_dir("COREPACK_HOME", f"{env_dir('XDG_CACHE_HOME', '~/.cache')}/node/corepack")


_INSTALLS = (f"{_home()}/v1",)

PLUGIN = Plugin(
    name="corepack",
    description="Package manager versions downloaded by corepack",
    requires="corepack",
    actions=(
        Action(
            name="cache",
            tier=Tier.CAUTION,
            help=(
                "Remove every yarn, pnpm, and npm version corepack downloaded. "
                "Each one downloads again the next time a project uses it."
            ),
            command=("corepack", "cache", "clean"),
            paths=_INSTALLS,
        ),
    ),
)
