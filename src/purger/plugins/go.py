"""Plugin for the Go build cache and module download cache."""

from purger.plugin import (
    Action,
    Plugin,
    RiskLevel,
    RunCommand,
    SizeOfDirectoryPrintedBy,
)

PLUGIN = Plugin(
    name="go",
    description="Go build cache and module download cache",
    requires_on_path="go",
    actions=(
        Action(
            name="build-cache",
            risk_level=RiskLevel.SAFE,
            description="Remove compiled build outputs and test results. Builds run slower once, then recover.",
            cleanup=RunCommand("go", "clean", "-cache"),
            # `go clean -cache` removes only the hashed subdirectories and keeps the fuzz corpus.
            size=SizeOfDirectoryPrintedBy(("go", "env", "GOCACHE"), subpaths=("[0-9a-f][0-9a-f]",)),
        ),
        Action(
            name="modcache",
            risk_level=RiskLevel.CAUTION,
            description=(
                "Remove downloaded module sources. Builds download them again "
                "and fail offline until they do."
            ),
            cleanup=RunCommand("go", "clean", "-modcache"),
            size=SizeOfDirectoryPrintedBy(("go", "env", "GOMODCACHE")),
        ),
    ),
)
