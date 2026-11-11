"""Plugin for the freedesktop thumbnail cache that Linux desktops share."""

from purger.plugin import (
    Action,
    DeletePaths,
    OperatingSystem,
    Plugin,
    RiskLevel,
    directory_from_environment,
)

THUMBNAIL_GLOBS = (
    f"{directory_from_environment('XDG_CACHE_HOME', default='~/.cache')}/thumbnails/*",
)

PLUGIN = Plugin(
    name="thumbnails",
    description="Desktop file thumbnail cache",
    operating_systems=(OperatingSystem.LINUX,),
    actions=(
        Action(
            name="cache",
            risk_level=RiskLevel.SAFE,
            description="Remove cached file thumbnails. File managers draw them again when needed.",
            cleanup=DeletePaths(*THUMBNAIL_GLOBS),
        ),
    ),
)
