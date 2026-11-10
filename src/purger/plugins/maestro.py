"""Plugin for the per-run test output Maestro keeps for every `maestro test`."""

from purger.plugin import Action, Plugin, Tier
from purger.plugins._shared import env_dir, process_running
from purger.sizing import expand_paths

# The JVM main class the maestro launcher runs, for test runs, studio, and mcp alike.
_MAESTRO_MAIN_CLASS = "maestro.cli.AppKt"


def _test_output_patterns() -> tuple[str, ...]:
    """List the folders Maestro writes run output to, as glob-safe patterns.

    Maestro documents `~/.maestro/tests`, but writes under the XDG state folder
    instead, so both are cleaned.

    Returns:
        tuple[str, ...]: Glob patterns matching each run's output folder.
    """
    state = env_dir("XDG_STATE_HOME", "~/.local/state")
    return ("~/.maestro/tests/*", f"{state}/maestro/tests/*")


_TEST_OUTPUT_PATHS = _test_output_patterns()


def _is_maestro_command(cmdline: list[str]) -> bool:
    """Report whether a process argv runs the Maestro CLI.

    Args:
        cmdline: The process argv.

    Returns:
        bool: True when the argv names Maestro's main class.
    """
    return _MAESTRO_MAIN_CLASS in cmdline


def _is_available() -> bool:
    """Report whether there is run output to clean and no Maestro run that could be writing it.

    Returns:
        bool: True when an output folder exists and Maestro is not running.
    """
    return bool(expand_paths(_TEST_OUTPUT_PATHS)) and not process_running(_is_maestro_command)


PLUGIN = Plugin(
    name="maestro",
    description="Maestro test run logs, screenshots, and local recordings",
    is_available=_is_available,
    actions=(
        Action(
            name="test-output",
            tier=Tier.CAUTION,
            help=(
                "Remove the logs, screenshots, and local recordings of past Maestro runs. "
                "They cannot be recovered."
            ),
            paths=_TEST_OUTPUT_PATHS,
        ),
    ),
)
