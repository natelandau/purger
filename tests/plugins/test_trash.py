"""Tests for the trash plugin."""

from pathlib import Path

import pytest
from pytest_mock import MockerFixture

from purger.plugins import trash


def test_trash_available_when_readable(tmp_path: Path, mocker: MockerFixture) -> None:
    """Verify the trash plugin applies when the Trash folder can be listed."""
    # Given
    (tmp_path / ".Trash").mkdir()
    mocker.patch("purger.plugins.trash.invoking_home", return_value=tmp_path)

    # When / Then
    assert trash._is_available() is True


def test_trash_available_when_missing(tmp_path: Path, mocker: MockerFixture) -> None:
    """Verify a home without a Trash folder counts as available with nothing to clean."""
    # Given
    mocker.patch("purger.plugins.trash.invoking_home", return_value=tmp_path)

    # When / Then
    assert trash._is_available() is True


def test_trash_unavailable_without_permission(tmp_path: Path, mocker: MockerFixture) -> None:
    """Verify a Trash that macOS refuses to list reports unavailable instead of empty."""
    # Given
    mocker.patch("purger.plugins.trash.invoking_home", return_value=tmp_path)
    mocker.patch.object(Path, "iterdir", side_effect=PermissionError)

    # When / Then
    assert trash._is_available() is False


@pytest.mark.parametrize(
    ("xdg_data_home", "expected"),
    [
        (
            None,
            (
                "~/.local/share/Trash/files/*",
                "~/.local/share/Trash/info/*",
                "~/.local/share/Trash/directorysizes",
            ),
        ),
        (
            "/Users/me/data",
            (
                "/Users/me/data/Trash/files/*",
                "/Users/me/data/Trash/info/*",
                "/Users/me/data/Trash/directorysizes",
            ),
        ),
    ],
)
def test_linux_trash_empties_contents_not_folders(
    xdg_data_home: str | None, expected: tuple[str, ...], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify the Linux Trash keeps its files and info folders and follows XDG_DATA_HOME."""
    # Given
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    if xdg_data_home is not None:
        monkeypatch.setenv("XDG_DATA_HOME", xdg_data_home)

    # When / Then
    assert trash._trash_paths("linux") == expected


def test_macos_trash_empties_home_trash() -> None:
    """Verify the macOS Trash is the home .Trash folder."""
    # Given / When / Then
    assert trash._trash_paths("darwin") == ("~/.Trash/*",)
