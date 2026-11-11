"""Tests for the macports plugin."""

from purger.plugin import OperatingSystem, RiskLevel, RunCommand, SizeOfPaths, SizeUnknown
from purger.plugins import macports


def test_macports_is_macos_only_and_sizes_the_prefix_folders() -> None:
    """Verify the plugin is limited to macOS and totals build, distfiles, and logs."""
    # Given
    total = macports.PLUGIN.total_size

    # When / Then
    assert macports.PLUGIN.operating_systems == (OperatingSystem.MACOS,)
    assert isinstance(total, SizeOfPaths)
    prefix = macports.MACPORTS_PREFIX
    assert total.globs == (
        f"{prefix}/var/macports/build",
        f"{prefix}/var/macports/distfiles",
        f"{prefix}/var/macports/logs",
    )


def test_macports_cleanups_use_the_absolute_port_path_with_sudo() -> None:
    """Verify both cleanups run the absolute `port` path as root and report an unknown size."""
    # Given
    port = str(macports.PORT_EXECUTABLE)

    # When / Then
    expected = {
        "clean": ((port, "-N", "clean", "--all", "installed"), RiskLevel.SAFE),
        "reclaim": ((port, "-N", "reclaim"), RiskLevel.CAUTION),
    }
    assert {a.name for a in macports.PLUGIN.actions} == set(expected)
    for action in macports.PLUGIN.actions:
        argv, risk = expected[action.name]
        assert isinstance(action.cleanup, RunCommand)
        assert action.cleanup.argv == argv
        assert action.cleanup.with_sudo is True
        assert action.risk_level is risk
        assert isinstance(action.size, SizeUnknown)


def test_macports_prefix_is_two_levels_above_the_port_executable() -> None:
    """Verify the prefix is derived from the port executable, such as /opt/local/bin/port."""
    # Given / When / Then
    assert macports.PORT_EXECUTABLE.parent.parent == macports.MACPORTS_PREFIX
