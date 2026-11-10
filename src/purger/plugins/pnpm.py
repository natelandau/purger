"""Plugin for the pnpm package store."""

from purger.plugin import Action, Plugin, Tier
from purger.sizing import printed_dir_usage

PLUGIN = Plugin(
    name="pnpm",
    description="pnpm content-addressable package store",
    requires="pnpm",
    size_cmd=("pnpm", "store", "path"),
    parse_size=printed_dir_usage,
    actions=(
        Action(
            name="prune",
            tier=Tier.SAFE,
            help="Remove packages that no project on this machine references.",
            command=("pnpm", "store", "prune"),
        ),
    ),
)
