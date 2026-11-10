"""Plugin for the Go build cache and module download cache."""

from functools import partial

from purger.plugin import Action, Plugin, Tier
from purger.sizing import printed_dir_usage

PLUGIN = Plugin(
    name="go",
    description="Go build cache and module download cache",
    requires="go",
    actions=(
        Action(
            name="build-cache",
            tier=Tier.SAFE,
            help="Remove compiled build outputs and test results. Builds run slower once, then recover.",
            command=("go", "clean", "-cache"),
            size_cmd=("go", "env", "GOCACHE"),
            # `go clean -cache` removes only the hashed subdirectories and keeps the fuzz corpus.
            parse_size=partial(printed_dir_usage, patterns=("[0-9a-f][0-9a-f]",)),
        ),
        Action(
            name="modcache",
            tier=Tier.CAUTION,
            help=(
                "Remove downloaded module sources. Builds download them again "
                "and fail offline until they do."
            ),
            command=("go", "clean", "-modcache"),
            size_cmd=("go", "env", "GOMODCACHE"),
            parse_size=printed_dir_usage,
        ),
    ),
)
