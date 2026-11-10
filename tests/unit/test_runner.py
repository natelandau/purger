"""Tests for the action runner."""

from pathlib import Path

import pytest
from nclutils.sh import (
    CompletedCommand,
    ShellCommandFailedError,
    ShellCommandNotFoundError,
    ShellCommandTimeoutError,
)
from pytest_mock import MockerFixture

from purger.reports import ActionReport, Status
from purger.runner import _describe_failure, run_action, sudo_ready
from tests.conftest import make_action


def _report(resolved: list[Path] | None = None, **action_overrides: object) -> ActionReport:
    return ActionReport(
        action=make_action(**action_overrides), size_bytes=None, resolved_paths=resolved or []
    )


def test_run_action_command_runs_tool(mocker: MockerFixture) -> None:
    """Verify a command action runs the tool command and is marked done."""
    # Given
    run_command = mocker.patch("purger.runner.run_command", autospec=True)
    report = _report(command=("uv", "cache", "prune"))

    # When
    run_action(report, home=Path("/unused"), verbose=False, interactive=True)

    # Then
    run_command.assert_called_once_with(["uv", "cache", "prune"], stream=False)
    assert report.status == Status.DONE
    assert report.error is None


def test_run_action_sudo_uses_run_interactive(mocker: MockerFixture) -> None:
    """Verify a sudo action runs interactively with sudo and never via run_command."""
    # Given
    run_command = mocker.patch("purger.runner.run_command", autospec=True)
    run_interactive = mocker.patch("purger.runner.run_interactive", autospec=True)
    report = _report(command=("apt", "clean"), needs_sudo=True)

    # When
    run_action(report, home=Path("/unused"), verbose=False, interactive=True)

    # Then
    run_interactive.assert_called_once_with(["apt", "clean"], sudo=True, check=True)
    run_command.assert_not_called()
    assert report.status == Status.DONE


def test_run_action_sudo_non_interactive_never_prompts(mocker: MockerFixture) -> None:
    """Verify a non-interactive sudo action captures output and tells sudo never to prompt."""
    # Given
    run_command = mocker.patch("purger.runner.run_command", autospec=True)
    run_interactive = mocker.patch("purger.runner.run_interactive", autospec=True)
    report = _report(command=("apt", "clean"), needs_sudo=True)

    # When
    run_action(report, home=Path("/unused"), verbose=True, interactive=False)

    # Then
    run_command.assert_called_once_with(["sudo", "-n", "apt", "clean"], stream=False)
    run_interactive.assert_not_called()
    assert report.status == Status.DONE


def test_run_action_command_failure_is_reported(mocker: MockerFixture) -> None:
    """Verify a failing command marks the action failed without raising."""
    # Given
    mocker.patch(
        "purger.runner.run_command", autospec=True, side_effect=ShellCommandFailedError(msg="boom")
    )
    report = _report(command=("false",))

    # When
    run_action(report, home=Path("/unused"), verbose=False, interactive=True)

    # Then
    assert report.status == Status.FAILED
    assert report.error is not None
    assert "boom" in report.error


def test_run_action_keyboard_interrupt_propagates(mocker: MockerFixture) -> None:
    """Verify KeyboardInterrupt is not swallowed by the runner."""
    # Given
    mocker.patch("purger.runner.run_command", autospec=True, side_effect=KeyboardInterrupt)
    report = _report(command=("sleep", "9"))

    # When / Then
    with pytest.raises(KeyboardInterrupt):
        run_action(report, home=Path("/unused"), verbose=False, interactive=True)


def test_run_action_path_deletes_matches(tmp_path: Path) -> None:
    """Verify a path action deletes every resolved path."""
    # Given
    home = tmp_path / "Users" / "me"
    paths = []
    for name in ("a", "b"):
        target = home / "Library" / "Caches" / name
        target.mkdir(parents=True)
        (target / "file").write_text("x")
        paths.append(target)
    report = _report(paths, command=None, paths=("~/Library/Caches/*",))

    # When
    run_action(report, home=home, verbose=False, interactive=True)

    # Then
    assert report.status == Status.DONE
    assert not any(p.exists() for p in paths)


def test_run_action_path_guard_violation_fails_action(tmp_path: Path) -> None:
    """Verify a guard violation fails the action and stops before later paths."""
    # Given
    home = tmp_path / "Users" / "me"
    bad = home / "Documents"
    bad.mkdir(parents=True)
    (bad / "file").write_text("x")
    good = home / "Library" / "Caches" / "x"
    good.mkdir(parents=True)
    report = _report([bad, good], command=None, paths=("~/Documents",))

    # When
    run_action(report, home=home, verbose=False, interactive=True)

    # Then
    assert report.status == Status.FAILED
    assert report.error is not None
    assert str(bad) in report.error
    assert (bad / "file").exists()
    assert good.exists()


def test_run_action_path_os_error_fails_action(mocker: MockerFixture, tmp_path: Path) -> None:
    """Verify a filesystem error fails the action and names the path."""
    # Given
    home = tmp_path / "Users" / "me"
    target = home / "Library" / "Caches" / "x"
    mocker.patch("purger.runner.delete_path", autospec=True, side_effect=PermissionError("denied"))
    report = _report([target], command=None, paths=("~/Library/Caches/x",))

    # When
    run_action(report, home=home, verbose=False, interactive=True)

    # Then
    assert report.status == Status.FAILED
    assert report.error is not None
    assert str(target) in report.error
    assert "denied" in report.error


def test_sudo_ready_true_when_credentials_cached(mocker: MockerFixture) -> None:
    """Verify sudo_ready is True when the non-interactive sudo probe succeeds."""
    # Given
    run_command = mocker.patch("purger.runner.run_command", autospec=True)
    run_command.return_value.ok = True

    # When
    ready = sudo_ready()

    # Then
    assert ready is True
    run_command.assert_called_once_with(["sudo", "-n", "true"], check=False, timeout=10)


def test_sudo_ready_false_when_sudo_missing(mocker: MockerFixture) -> None:
    """Verify sudo_ready is False when sudo is not installed."""
    # Given
    mocker.patch(
        "purger.runner.run_command",
        autospec=True,
        side_effect=ShellCommandNotFoundError(argv=["sudo"]),
    )

    # When / Then
    assert sudo_ready() is False


def test_sudo_ready_false_when_credentials_not_cached(mocker: MockerFixture) -> None:
    """Verify sudo_ready is False when the probe exits non-zero."""
    # Given
    run_command = mocker.patch("purger.runner.run_command", autospec=True)
    run_command.return_value.ok = False

    # When / Then
    assert sudo_ready() is False


def _completed(*, stderr: str = "", returncode: int = 1) -> CompletedCommand:
    return CompletedCommand(
        argv=("tool",), returncode=returncode, stdout="", stderr=stderr, duration=0.0, cwd=None
    )


def test_run_action_undecodable_output_fails_action(mocker: MockerFixture) -> None:
    """Verify tool output that is not UTF-8 fails the action instead of escaping."""
    # Given
    mocker.patch(
        "purger.runner.run_command",
        autospec=True,
        side_effect=UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte"),
    )
    report = _report(command=("tool",))

    # When
    run_action(report, home=Path("/unused"), verbose=False, interactive=True)

    # Then
    assert report.status == Status.FAILED
    assert report.error is not None
    assert "UTF-8" in report.error


def test_run_action_symlinked_parent_fails_action(tmp_path: Path) -> None:
    """Verify a match reached through a symlinked directory fails and deletes nothing."""
    # Given a cache directory that is a symlink to the user's documents
    home = tmp_path / "Users" / "me"
    documents = home / "Documents" / "Project"
    documents.mkdir(parents=True)
    (home / "Library").mkdir()
    (home / "Library" / "Caches").symlink_to(home / "Documents")
    match = home / "Library" / "Caches" / "Project"
    report = _report([match], command=None, paths=("~/Library/Caches/*",))

    # When
    run_action(report, home=home, verbose=False, interactive=True)

    # Then
    assert report.status == Status.FAILED
    assert report.error is not None
    assert "symlink" in report.error
    assert documents.exists()


def test_run_action_verbose_streams(mocker: MockerFixture) -> None:
    """Verify verbose mode streams a plain command's output."""
    # Given
    run_command = mocker.patch("purger.runner.run_command", autospec=True)
    report = _report(command=("uv", "cache", "prune"))

    # When
    run_action(report, home=Path("/unused"), verbose=True, interactive=True)

    # Then
    run_command.assert_called_once_with(["uv", "cache", "prune"], stream=True)


def test_run_action_interactive_sudo_failure_is_reported(mocker: MockerFixture) -> None:
    """Verify a failing interactive sudo command marks the action failed."""
    # Given
    mocker.patch(
        "purger.runner.run_interactive",
        autospec=True,
        side_effect=ShellCommandFailedError(msg="sudo: 3 incorrect password attempts"),
    )
    report = _report(command=("apt-get", "clean"), needs_sudo=True)

    # When
    run_action(report, home=Path("/unused"), verbose=False, interactive=True)

    # Then
    assert report.status == Status.FAILED
    assert report.error is not None
    assert "incorrect password" in report.error


@pytest.mark.parametrize(
    ("stderr", "expected"),
    [
        ("  disk full \n", "boom: disk full"),
        ("   ", "boom"),
        ("", "boom"),
    ],
)
def test_describe_appends_trimmed_stderr(stderr: str, expected: str) -> None:
    """Verify a failure message carries the trimmed stderr only when there is some."""
    # Given
    exc = ShellCommandFailedError(result=_completed(stderr=stderr), msg="boom")

    # When
    message = _describe_failure(exc)

    # Then
    assert message == expected


def test_describe_does_not_repeat_stderr_in_built_message() -> None:
    """Verify stderr appears once when the error message was built from the result."""
    # Given an error whose message nclutils built from the result
    exc = ShellCommandFailedError(result=_completed(stderr="disk full"))

    # When
    message = _describe_failure(exc)

    # Then
    assert message.count("disk full") == 1


def test_describe_leads_with_explain_hint() -> None:
    """Verify an action's hint leads the message and the tool output still follows."""
    # Given
    exc = ShellCommandFailedError(result=_completed(stderr="  ENOTEMPTY \n"))
    seen: list[str] = []

    def explain(stderr: str) -> str:
        seen.append(stderr)
        return "a hint"

    # When
    message = _describe_failure(exc, explain=explain)

    # Then
    assert seen == ["ENOTEMPTY"]
    assert message.startswith("a hint\n")
    assert message.count("ENOTEMPTY") == 1


@pytest.mark.parametrize("stderr", ["", "   "])
def test_describe_skips_explain_without_stderr(stderr: str) -> None:
    """Verify the hint hook is not called when the command printed no stderr."""
    # Given
    exc = ShellCommandFailedError(result=_completed(stderr=stderr), msg="boom")

    def explain(_stderr: str) -> str:
        pytest.fail("explain must not run without stderr")

    # When
    message = _describe_failure(exc, explain=explain)

    # Then
    assert message == "boom"


def test_describe_keeps_message_when_explain_returns_none() -> None:
    """Verify a hook with nothing to add leaves the message unchanged."""
    # Given
    exc = ShellCommandFailedError(result=_completed(stderr="disk full"), msg="boom")

    # When
    message = _describe_failure(exc, explain=lambda _stderr: None)

    # Then
    assert message == "boom: disk full"


def test_run_action_failure_uses_action_explain_hook(mocker: MockerFixture) -> None:
    """Verify a failed command's report carries the hint from the action's hook."""
    # Given
    mocker.patch(
        "purger.runner.run_command",
        autospec=True,
        side_effect=ShellCommandFailedError(result=_completed(stderr="ENOTEMPTY")),
    )
    report = _report(command=("tool",), explain_failure=lambda _stderr: "a hint")

    # When
    run_action(report, home=Path("/unused"), verbose=False, interactive=True)

    # Then
    assert report.status == Status.FAILED
    assert report.error is not None
    assert report.error.startswith("a hint\n")


def test_sudo_ready_false_on_timeout(mocker: MockerFixture) -> None:
    """Verify sudo_ready is False when the probe times out."""
    # Given
    mocker.patch(
        "purger.runner.run_command",
        autospec=True,
        side_effect=ShellCommandTimeoutError(result=_completed(returncode=-9), timeout=10.0),
    )

    # When / Then
    assert sudo_ready() is False
