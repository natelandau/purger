"""Tests for the helpers plugins share."""

import pytest
from pytest_mock import MockerFixture

from purger.plugins._shared import env_dir, process_running


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, "~/.cargo"),
        ("", "~/.cargo"),
        ("/opt/data/", "/opt/data"),
        ("/Users/me/odd[dir]", "/Users/me/odd[[]dir]"),
    ],
)
def test_env_dir_prefers_the_variable(
    value: str | None, expected: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify the variable wins when set, with glob characters escaped, and the default fills in."""
    # Given
    monkeypatch.delenv("TOOL_HOME", raising=False)
    if value is not None:
        monkeypatch.setenv("TOOL_HOME", value)

    # When / Then
    assert env_dir("TOOL_HOME", "~/.cargo") == expected


@pytest.mark.parametrize(
    ("cmdlines", "expected"),
    [
        ([["/bin/zsh"], ["tool", "run"]], True),
        ([["/bin/zsh"], None, []], False),
    ],
    ids=["match", "no-match-or-unreadable"],
)
def test_process_running_checks_readable_argv(
    cmdlines: list[list[str] | None], expected: bool, mocker: MockerFixture
) -> None:
    """Verify a matching argv counts as running, and unreadable or empty argv is skipped."""
    # Given
    processes = [mocker.Mock(info={"cmdline": cmdline}) for cmdline in cmdlines]
    mocker.patch("purger.plugins._shared.psutil.process_iter", return_value=processes)

    # When / Then
    assert process_running(lambda argv: argv[0] == "tool") is expected
