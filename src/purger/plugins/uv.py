"""Plugin for the uv package cache."""

from purger.plugin import (
    Action,
    Plugin,
    RiskLevel,
    RunCommand,
    SizeReportedByCommand,
    SizeUnknown,
)

UV_CACHE_SIZE_COMMAND = (
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
    requires_on_path="uv",
    total_size=SizeReportedByCommand(UV_CACHE_SIZE_COMMAND, read_size=int),
    actions=(
        Action(
            name="prune",
            risk_level=RiskLevel.SAFE,
            description="Remove unused cache entries, keeping everything current projects use.",
            cleanup=RunCommand("uv", "cache", "prune"),
            size=SizeUnknown(reason="uv cannot report what prune would remove"),
        ),
        Action(
            name="clean",
            risk_level=RiskLevel.DANGEROUS,
            description="Wipe the entire uv cache. Every package is re-downloaded and rebuilt on next use.",
            cleanup=RunCommand("uv", "cache", "clean", "--force"),
            size=SizeReportedByCommand(UV_CACHE_SIZE_COMMAND, read_size=int),
        ),
    ),
)
