"""Plugin for conda's package tarballs, unused packages, index cache, and logs."""

import json

from purger.plugin import Action, Plugin, RiskLevel, RunCommand, SizeReportedByCommand

CONDA_CLEAN_DRY_RUN_COMMAND = ("conda", "clean", "--all", "--dry-run", "--json")


def reclaimable_bytes(output: str) -> int:
    """Sum the tarball and unused package sizes a `conda clean` dry run reports.

    The index cache, temp files, and logs carry no size in the report and are small,
    so they are left out.

    Args:
        output: JSON from `conda clean --all --dry-run --json`.

    Returns:
        int: Bytes the tarballs and unused packages take.
    """
    report = json.loads(output)
    return sum(report.get(key, {}).get("total_size", 0) for key in ("tarballs", "packages"))


PLUGIN = Plugin(
    name="conda",
    description="conda package tarballs, unused packages, and index cache",
    requires_on_path="conda",
    total_size=SizeReportedByCommand(CONDA_CLEAN_DRY_RUN_COMMAND, read_size=reclaimable_bytes),
    actions=(
        Action(
            name="clean",
            risk_level=RiskLevel.SAFE,
            description=(
                "Remove downloaded tarballs, packages no environment links to, the index "
                "cache, and logs. Installs download them again."
            ),
            cleanup=RunCommand("conda", "clean", "--all", "--yes"),
            size=SizeReportedByCommand(CONDA_CLEAN_DRY_RUN_COMMAND, read_size=reclaimable_bytes),
        ),
    ),
)
