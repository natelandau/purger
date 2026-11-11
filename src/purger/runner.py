"""Execute approved cleanup actions."""

import logging
from collections.abc import Callable, Sequence
from pathlib import Path

from nclutils.sh import (
    ShellCommandError,
    ShellCommandFailedError,
    run_command,
    run_interactive,
)

from purger.reports import ActionReport, Status
from purger.safety import UnsafePathError, delete_path

logger = logging.getLogger(__name__)

_SUDO_PROBE_TIMEOUT_SECONDS = 10


def _explain(explain: Callable[[str], str | None], *, stderr: str) -> str | None:
    """Run an action's failure hook so a broken hook drops its hint instead of the run."""
    try:
        return explain(stderr)
    except Exception as exc:  # noqa: BLE001 - plugin-supplied code can raise anything
        logger.debug("cannot explain failure: %s", exc)
        return None


def _describe_failure(exc: Exception, *, explain: Callable[[str], str | None] | None = None) -> str:
    """Build an error message for a failed action, adding trimmed stderr from a failed command.

    Args:
        exc: The exception that failed the action.
        explain: The action's hook that turns a failed command's stderr into a
            hint, which leads the message so it is not lost below the tool output.

    Returns:
        str: The message to record on the action report.
    """
    if isinstance(exc, UnicodeDecodeError):
        return f"tool output is not UTF-8: {exc}"
    message = str(exc)
    if isinstance(exc, ShellCommandFailedError) and exc.result is not None:
        stderr = exc.result.stderr.strip()
        # A message nclutils builds from the result already carries stderr.
        if stderr and stderr not in message:
            message = f"{message}: {stderr}"
        hint = _explain(explain, stderr=stderr) if explain is not None and stderr else None
        if hint:
            message = f"{hint}\n{message}"
    return message


class _PathDeletionError(Exception):
    """A resolved path could not be deleted; the message names the path."""


def _run_command_action(
    command: tuple[str, ...], *, needs_sudo: bool, verbose: bool, interactive: bool
) -> None:
    """Run the tool's own cleanup command."""
    if needs_sudo and interactive:
        run_interactive(list(command), sudo=True, check=True)
    elif needs_sudo:
        # `-n` makes sudo fail instead of waiting for a password no one can type.
        run_command(["sudo", "-n", *command], stream=False)
    else:
        run_command(list(command), stream=verbose)


def _delete_paths(report: ActionReport, *, home: Path) -> None:
    """Delete resolved paths, continuing past filesystem errors.

    A guard violation stops at once, since it means the path list itself is
    suspect. A filesystem error, such as one unremovable file, fails only its
    own path, so the rest of the action's paths are still removed.

    Raises:
        _PathDeletionError: If any path could not be deleted. The message names
            every failed path.
    """
    failures: list[str] = []
    for path in report.resolved_paths:
        try:
            delete_path(path=path, home=home)
        except UnsafePathError as exc:
            failures.append(f"{path}: {exc}")
            break
        except OSError as exc:
            failures.append(f"{path}: {exc}")
    if failures:
        raise _PathDeletionError("\n".join(failures))


def run_action(report: ActionReport, *, home: Path, verbose: bool, interactive: bool) -> None:
    """Execute one approved action and record DONE or FAILED on its report.

    Failures are recorded rather than raised so one bad action never stops the run.
    KeyboardInterrupt still propagates so the CLI can handle it.

    Args:
        report: The action report to run and update.
        home: Home directory that bounds what path deletion may touch.
        verbose: Stream the tool command's output when True.
        interactive: Whether a person is at the terminal. Sudo commands then run
            attached to it so sudo can ask for a password; otherwise their output
            is captured and sudo must not prompt.
    """
    try:
        command = report.action.command
        if command is not None:
            _run_command_action(
                command,
                needs_sudo=report.action.needs_sudo,
                verbose=verbose,
                interactive=interactive,
            )
        else:
            _delete_paths(report, home=home)
    except (ShellCommandError, UnicodeDecodeError, _PathDeletionError, OSError) as exc:
        logger.debug("Action %s failed", report.action.name, exc_info=True)
        report.status = Status.FAILED
        report.error = _describe_failure(exc, explain=report.action.explain_failure)
    else:
        report.status = Status.DONE
        report.error = None


def command_succeeds(argv: Sequence[str], *, timeout: float) -> bool:
    """Run a probe command and report whether it exited zero.

    Probes answer yes or no, so a command that cannot run counts as a no.

    Args:
        argv: The command and its arguments.
        timeout: Seconds to wait before giving up.

    Returns:
        bool: True when the command ran and exited zero.
    """
    try:
        return run_command(list(argv), check=False, timeout=timeout).ok
    except ShellCommandError as exc:
        logger.debug("probe %s failed: %s", argv[0], exc)
        return False


def sudo_ready() -> bool:
    """Check whether sudo can run without prompting, so a run can fail early.

    Returns:
        True when sudo credentials are cached or passwordless sudo is configured.
    """
    return command_succeeds(["sudo", "-n", "true"], timeout=_SUDO_PROBE_TIMEOUT_SECONDS)
