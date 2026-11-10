"""Plugin for the Swift Package Manager shared cache."""

import sys

from purger.plugin import Action, Plugin, Tier
from purger.plugins._shared import env_dir

# The real folder, since purger skips paths reached through ~/.swiftpm/cache, a link to it.
_CACHE = (
    "~/Library/Caches/org.swift.swiftpm"
    if sys.platform == "darwin"
    else f"{env_dir('XDG_CACHE_HOME', '~/.cache')}/org.swift.swiftpm"
)

PLUGIN = Plugin(
    name="swiftpm",
    description="Swift Package Manager package and binary artifact cache",
    requires="swift",
    actions=(
        Action(
            name="purge-cache",
            tier=Tier.CAUTION,
            help=(
                "Remove cached package clones, registry downloads, and compiled manifests. "
                "Packages clone again on next resolve."
            ),
            command=("swift", "package", "purge-cache"),
            paths=(f"{_CACHE}/repositories", f"{_CACHE}/registry/downloads", f"{_CACHE}/manifests"),
        ),
        Action(
            name="artifacts",
            tier=Tier.CAUTION,
            help="Remove downloaded binary frameworks. They download again on next resolve.",
            paths=(f"{_CACHE}/artifacts/*",),
        ),
    ),
)
