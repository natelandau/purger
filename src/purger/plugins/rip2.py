"""Plugin for the rip2 graveyard, where `rip` keeps the files it deletes."""

from nclutils.sh import run_command

from purger.plugin import Action, Plugin, Tier
from purger.sizing import printed_dir_usage

_GRAVEYARD_CMD = ("rip", "graveyard")

# Only rip2 has the `graveyard` subcommand. The original rip installs the same
# binary and would bury a file named `graveyard` instead of printing a path.
_RIP2_MARKER = "Print the graveyard path"


def _is_available() -> bool:
    """Report whether `rip` on PATH is rip2, so its subcommands mean what this plugin expects.

    Returns:
        bool: True when `rip --help` succeeds and lists rip2's graveyard subcommand.
    """
    result = run_command(["rip", "--help"], check=False, timeout=10)
    return result.ok and _RIP2_MARKER in result.stdout


PLUGIN = Plugin(
    name="rip2",
    description="rip2 graveyard of deleted files",
    requires="rip",
    is_available=_is_available,
    size_cmd=_GRAVEYARD_CMD,
    parse_size=printed_dir_usage,
    actions=(
        Action(
            name="decompose",
            tier=Tier.CAUTION,
            help=(
                "Remove everything in the rip2 graveyard. "
                "Buried files can no longer be restored with `rip -u`."
            ),
            command=("rip", "--decompose", "--force"),
            size_cmd=_GRAVEYARD_CMD,
            parse_size=printed_dir_usage,
        ),
    ),
)
