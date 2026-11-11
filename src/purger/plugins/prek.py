"""Plugin for the prek hook cache."""

from purger.plugin import (
    Action,
    Plugin,
    RiskLevel,
    RunCommand,
    SizeReportedByCommand,
    SizeUnknown,
)

PREK_CACHE_SIZE_COMMAND = ("prek", "cache", "size", "--output-format", "machine")

PLUGIN = Plugin(
    name="prek",
    description="prek hook environments and repos",
    requires_on_path="prek",
    total_size=SizeReportedByCommand(PREK_CACHE_SIZE_COMMAND, read_size=int),
    actions=(
        Action(
            name="gc",
            risk_level=RiskLevel.SAFE,
            description="Remove hook environments and repos no longer referenced by any config.",
            cleanup=RunCommand("prek", "cache", "gc"),
            size=SizeUnknown(reason="prek cannot preview what cache gc removes"),
        ),
        Action(
            name="clean",
            risk_level=RiskLevel.DANGEROUS,
            description="Wipe the entire prek cache. Hook environments are reinstalled on next run.",
            cleanup=RunCommand("prek", "cache", "clean"),
            size=SizeReportedByCommand(PREK_CACHE_SIZE_COMMAND, read_size=int),
        ),
    ),
)
