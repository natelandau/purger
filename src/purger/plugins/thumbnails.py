"""Plugin for the freedesktop thumbnail cache that Linux desktops share."""

from purger.plugin import Action, Plugin, Tier
from purger.plugins._shared import env_dir

_THUMBNAIL_PATHS = (f"{env_dir('XDG_CACHE_HOME', '~/.cache')}/thumbnails/*",)

PLUGIN = Plugin(
    name="thumbnails",
    description="Desktop file thumbnail cache",
    platforms=frozenset({"linux"}),
    actions=(
        Action(
            name="cache",
            tier=Tier.SAFE,
            help="Remove cached file thumbnails. File managers draw them again when needed.",
            paths=_THUMBNAIL_PATHS,
        ),
    ),
)
