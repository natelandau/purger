"""Tests for the helpers plugins share."""

import pytest

from purger.plugins._shared import env_dir


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, "~/.cargo"),
        ("", "~/.cargo"),
        ("/opt/data/", "/opt/data"),
        ("/Users/me/odd[dir]", "/Users/me/odd[[]dir]"),
        ("relative/data", "~/.cargo"),
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
