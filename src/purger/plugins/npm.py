"""Plugin for the npm package cache and npx cache."""

from functools import partial

from purger.plugin import Action, Plugin, Tier
from purger.sizing import printed_dir_usage

_CACHE_DIR_CMD = ("npm", "config", "get", "cache")

PLUGIN = Plugin(
    name="npm",
    description="npm package cache and npx cache",
    requires="npm",
    size_cmd=_CACHE_DIR_CMD,
    # Only the parts purger can clean, so the two cleanups account for the whole footprint.
    parse_size=partial(printed_dir_usage, patterns=("_cacache", "_npx")),
    actions=(
        Action(
            name="verify",
            tier=Tier.SAFE,
            help="Garbage-collect cache data that is no longer needed and verify the rest.",
            command=("npm", "cache", "verify"),
        ),
        Action(
            name="npx",
            tier=Tier.SAFE,
            help="Remove packages cached by npx. They download again on the next npx run.",
            command=("npm", "cache", "npx", "rm", "--force"),
            size_cmd=_CACHE_DIR_CMD,
            parse_size=partial(printed_dir_usage, patterns=("_npx",)),
        ),
        Action(
            name="clean",
            tier=Tier.DANGEROUS,
            help="Wipe the entire package cache. Every package is downloaded again on next install.",
            command=("npm", "cache", "clean", "--force"),
            size_cmd=_CACHE_DIR_CMD,
            parse_size=partial(printed_dir_usage, patterns=("_cacache",)),
        ),
    ),
)
