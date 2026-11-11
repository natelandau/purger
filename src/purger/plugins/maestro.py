"""Plugin for the per-run test output Maestro keeps for every `maestro test`."""

from purger.plugin import (
    Action,
    DeletePaths,
    Plugin,
    RiskLevel,
    directory_from_environment,
)
from purger.sizing import any_path_exists

# The JVM main class the maestro launcher runs, for test runs, studio, and mcp alike.
MAESTRO_MAIN_CLASS = "maestro.cli.AppKt"


def maestro_test_output_globs() -> tuple[str, ...]:
    """List the folders Maestro writes run output to, as glob-safe patterns.

    Maestro documents `~/.maestro/tests`, but writes under the XDG state folder
    instead, so both are cleaned.

    Returns:
        tuple[str, ...]: Glob patterns matching each run's output folder.
    """
    state = directory_from_environment("XDG_STATE_HOME", default="~/.local/state")
    return ("~/.maestro/tests/*", f"{state}/maestro/tests/*")


MAESTRO_TEST_OUTPUT_GLOBS = maestro_test_output_globs()


def command_runs_maestro(cmdline: list[str]) -> bool:
    """Report whether a process argv runs the Maestro CLI.

    Args:
        cmdline: The process argv.

    Returns:
        bool: True when the argv names Maestro's main class.
    """
    return MAESTRO_MAIN_CLASS in cmdline


def maestro_has_test_output() -> bool:
    """Report whether there is run output to clean.

    Returns:
        bool: True when an output folder exists.
    """
    return any_path_exists(MAESTRO_TEST_OUTPUT_GLOBS)


PLUGIN = Plugin(
    name="maestro",
    description="Maestro test run logs, screenshots, and local recordings",
    skip_unless=maestro_has_test_output,
    skip_while_running=command_runs_maestro,
    actions=(
        Action(
            name="test-output",
            risk_level=RiskLevel.CAUTION,
            description=(
                "Remove the logs, screenshots, and local recordings of past Maestro runs. "
                "They cannot be recovered."
            ),
            cleanup=DeletePaths(*MAESTRO_TEST_OUTPUT_GLOBS),
        ),
    ),
)
