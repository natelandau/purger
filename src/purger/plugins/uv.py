"""Plugin for the uv package cache."""

from purger.plugin import Action, Plugin, Tier

_SIZE_CMD = (
    "uv",
    "cache",
    "size",
    "--output-format",
    "machine",
    "--preview-features",
    "cache-size",
)

PLUGIN = Plugin(
    name="uv",
    description="uv package cache",
    requires="uv",
    size_cmd=_SIZE_CMD,
    actions=(
        Action(
            name="prune",
            tier=Tier.SAFE,
            help="Remove unused cache entries, keeping everything current projects use.",
            command=("uv", "cache", "prune"),
        ),
        Action(
            name="clean",
            tier=Tier.DANGEROUS,
            help="Wipe the entire uv cache. Every package is re-downloaded and rebuilt on next use.",
            command=("uv", "cache", "clean", "--force"),
            size_cmd=_SIZE_CMD,
        ),
    ),
)
