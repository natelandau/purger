"""Plugin for the rip2 graveyard, where `rip` keeps the files it deletes."""

from nclutils.sh import run_command

from purger.plugin import (
    Action,
    Plugin,
    RiskLevel,
    RunCommand,
    SizeOfDirectoryPrintedBy,
)

RIP_GRAVEYARD_COMMAND = ("rip", "graveyard")

# Only rip2 has the `graveyard` subcommand. The original rip installs the same
# binary and would bury a file named `graveyard` instead of printing a path.
_RIP2_MARKER = "Print the graveyard path"


def rip_is_rip2() -> bool:
    """Report whether `rip` on PATH is rip2, so its subcommands mean what this plugin expects.

    Returns:
        bool: True when `rip --help` succeeds and lists rip2's graveyard subcommand.
    """
    result = run_command(["rip", "--help"], check=False, timeout=10)
    return result.ok and _RIP2_MARKER in result.stdout


PLUGIN = Plugin(
    name="rip2",
    description="rip2 graveyard of deleted files",
    requires_on_path="rip",
    skip_unless=rip_is_rip2,
    total_size=SizeOfDirectoryPrintedBy(RIP_GRAVEYARD_COMMAND),
    actions=(
        Action(
            name="decompose",
            risk_level=RiskLevel.CAUTION,
            description=(
                "Remove everything in the rip2 graveyard. "
                "Buried files can no longer be restored with `rip -u`."
            ),
            cleanup=RunCommand("rip", "--decompose", "--force"),
            size=SizeOfDirectoryPrintedBy(RIP_GRAVEYARD_COMMAND),
        ),
    ),
)
