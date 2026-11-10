"""Plugin for the CocoaPods download cache."""

from purger.plugin import Action, Plugin, Tier
from purger.plugins._shared import env_dir


def _cache_dir() -> str:
    """Return the CocoaPods cache folder, which `CP_CACHE_DIR` overrides before `CP_HOME_DIR`.

    Returns:
        str: The cache folder as a glob-safe pattern.
    """
    home = env_dir("CP_HOME_DIR", "")
    return env_dir("CP_CACHE_DIR", f"{home}/cache" if home else "~/Library/Caches/CocoaPods")


_CACHE = (f"{_cache_dir()}/*",)

PLUGIN = Plugin(
    name="cocoapods",
    description="CocoaPods download cache",
    platforms=frozenset({"darwin"}),
    requires="pod",
    actions=(
        Action(
            name="cache",
            tier=Tier.CAUTION,
            help="Wipe the pod download cache. Pods download again on next `pod install`.",
            command=("pod", "cache", "clean", "--all"),
            paths=_CACHE,
        ),
    ),
)
