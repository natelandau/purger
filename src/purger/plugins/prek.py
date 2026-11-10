"""Plugin for the prek hook cache."""

from purger.plugin import Action, Plugin, Tier

_SIZE_CMD = ("prek", "cache", "size", "--output-format", "machine")

PLUGIN = Plugin(
    name="prek",
    description="prek hook environments and repos",
    requires="prek",
    size_cmd=_SIZE_CMD,
    actions=(
        Action(
            name="gc",
            tier=Tier.SAFE,
            help="Remove hook environments and repos no longer referenced by any config.",
            command=("prek", "cache", "gc"),
        ),
        Action(
            name="clean",
            tier=Tier.DANGEROUS,
            help="Wipe the entire prek cache. Hook environments are reinstalled on next run.",
            command=("prek", "cache", "clean"),
            size_cmd=_SIZE_CMD,
        ),
    ),
)
