"""Plugin for conda's package tarballs, unused packages, index cache, and logs."""

import json

from purger.plugin import Action, Plugin, Tier

_DRY_RUN = ("conda", "clean", "--all", "--dry-run", "--json")


def _reclaimable_bytes(output: str) -> int:
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
    requires="conda",
    size_cmd=_DRY_RUN,
    parse_size=_reclaimable_bytes,
    actions=(
        Action(
            name="clean",
            tier=Tier.SAFE,
            help=(
                "Remove downloaded tarballs, packages no environment links to, the index "
                "cache, and logs. Installs download them again."
            ),
            command=("conda", "clean", "--all", "--yes"),
            size_cmd=_DRY_RUN,
            parse_size=_reclaimable_bytes,
        ),
    ),
)
