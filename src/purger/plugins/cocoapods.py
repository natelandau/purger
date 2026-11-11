"""Plugin for the CocoaPods download cache."""

from purger.plugin import (
    Action,
    OperatingSystem,
    Plugin,
    RiskLevel,
    RunCommand,
    SizeOfPaths,
    directory_from_environment,
)


def cocoapods_cache_directory() -> str:
    """Return the CocoaPods cache folder, which `CP_CACHE_DIR` overrides before `CP_HOME_DIR`.

    Returns:
        str: The cache folder as a glob-safe pattern.
    """
    home = directory_from_environment("CP_HOME_DIR", default="")
    return directory_from_environment(
        "CP_CACHE_DIR", default=f"{home}/cache" if home else "~/Library/Caches/CocoaPods"
    )


COCOAPODS_CACHE_FILES = f"{cocoapods_cache_directory()}/*"

PLUGIN = Plugin(
    name="cocoapods",
    description="CocoaPods download cache",
    operating_systems=(OperatingSystem.MACOS,),
    requires_on_path="pod",
    actions=(
        Action(
            name="cache",
            risk_level=RiskLevel.CAUTION,
            description="Wipe the pod download cache. Pods download again on next `pod install`.",
            cleanup=RunCommand("pod", "cache", "clean", "--all"),
            size=SizeOfPaths(COCOAPODS_CACHE_FILES),
        ),
    ),
)
