"""Tests for the flatpak plugin."""

from purger.plugin import OperatingSystem, RunCommand, SizeUnknown
from purger.plugins import flatpak


def _action(name: str) -> RunCommand:
    action = next(a for a in flatpak.PLUGIN.actions if a.name == name)
    assert isinstance(action.cleanup, RunCommand)
    return action.cleanup


def test_flatpak_is_linux_only_with_unknown_sizes() -> None:
    """Verify flatpak is limited to Linux and reports no sizes, since it cannot preview removals."""
    # Given / When / Then
    assert flatpak.PLUGIN.operating_systems == (OperatingSystem.LINUX,)
    assert isinstance(flatpak.PLUGIN.total_size, SizeUnknown)
    assert all(isinstance(action.size, SizeUnknown) for action in flatpak.PLUGIN.actions)


def test_flatpak_user_cleanup_runs_without_sudo() -> None:
    """Verify the user installation is cleaned as the current user."""
    # Given
    cleanup = _action("unused-user")

    # When / Then
    assert cleanup.with_sudo is False
    assert cleanup.argv[-1] == "--user"
    assert cleanup.argv[:3] == ("flatpak", "uninstall", "--unused")


def test_flatpak_system_cleanup_runs_with_sudo() -> None:
    """Verify the system installation is cleaned as root."""
    # Given
    cleanup = _action("unused-system")

    # When / Then
    assert cleanup.with_sudo is True
    assert cleanup.argv[-1] == "--system"
    assert "--noninteractive" in cleanup.argv
