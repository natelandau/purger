"""Plugin for the Swift Package Manager shared cache."""

import sys

from purger.plugin import (
    Action,
    DeletePaths,
    Plugin,
    RiskLevel,
    RunCommand,
    SizeOfPaths,
    directory_from_environment,
)

# The real folder, since purger skips paths reached through ~/.swiftpm/cache, a link to it.
SWIFTPM_CACHE_DIRECTORY = (
    "~/Library/Caches/org.swift.swiftpm"
    if sys.platform == "darwin"
    else f"{directory_from_environment('XDG_CACHE_HOME', default='~/.cache')}/org.swift.swiftpm"
)

PLUGIN = Plugin(
    name="swiftpm",
    description="Swift Package Manager package and binary artifact cache",
    requires_on_path="swift",
    actions=(
        Action(
            name="purge-cache",
            risk_level=RiskLevel.CAUTION,
            description=(
                "Remove cached package clones, registry downloads, and compiled manifests. "
                "Packages clone again on next resolve."
            ),
            cleanup=RunCommand("swift", "package", "purge-cache"),
            size=SizeOfPaths(
                f"{SWIFTPM_CACHE_DIRECTORY}/repositories",
                f"{SWIFTPM_CACHE_DIRECTORY}/registry/downloads",
                f"{SWIFTPM_CACHE_DIRECTORY}/manifests",
            ),
        ),
        Action(
            name="artifacts",
            risk_level=RiskLevel.CAUTION,
            description="Remove downloaded binary frameworks. They download again on next resolve.",
            cleanup=DeletePaths(f"{SWIFTPM_CACHE_DIRECTORY}/artifacts/*"),
        ),
    ),
)
